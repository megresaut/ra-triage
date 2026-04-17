"""Dev-server subprocess spawn + readiness + shutdown.

The RA AVM backend's ``dev_command`` is ``cd api && PORT={port} go run
./cmd/server`` — a shell string with ``cd``, ``&&``, and a template
substitution. We intentionally execute it through ``bash -c`` rather than
trying to parse it into an exec-list, because the shell semantics (the
``cd`` + ``&&`` chain) are load-bearing and re-implementing them in Python
would be a bug farm.

Each spawned dev server streams stdout+stderr (merged) into two destinations:

- A persistent log file at ``~/ra-triage/logs/<bug_id>/test_instance.<repo_name>.log``.
- The event bus (``test-instance:logs`` topic), so the frontend WS panel
  tails it live.

Readiness
---------

RA AVM has no ``/health`` endpoints (``health_url = None`` in the registry).
We therefore poll for readiness by:

1. Opening a TCP connection to ``127.0.0.1:<port>`` — confirms the bind is up.
2. Performing one HTTP GET on ``http://127.0.0.1:<port>/`` and checking the
   status code is ``<500`` — confirms the server is actually responding,
   not just listening.

Polling interval is 500ms; timeout defaults to 30s per server.

Shutdown
--------

Graceful shutdown is always ``SIGTERM`` → wait ``term_wait`` (default 5s) →
``SIGKILL`` → ``pkill -P <pid>`` to reap shell-spawned children. Because we
run dev commands under ``bash -c``, the bash shell itself is the direct child
and the real Go/Vite server is its grandchild; ``pkill -P`` is what actually
tears the grandchild down if bash has already exited. ``stop_process`` is
best-effort — we never raise if the process is already dead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import Request, urlopen

from ra_triage.config.registry import RepoConfig
from ra_triage.settings import get_settings
from ra_triage.utils.events import publish_test_instance_log

logger = logging.getLogger(__name__)


class ReadinessTimeout(RuntimeError):
    """Raised by :func:`wait_for_readiness` when the dev server never came up."""


# Only bind on localhost — this is a non-negotiable per the Agent 7 brief. We
# set it as module-level state so every readiness probe + dev-command
# templating lines up.
_LOCALHOST: str = "127.0.0.1"


@dataclass
class SpawnedProcess:
    """One running dev server.

    ``log_task`` is the asyncio task draining stdout to the log file + event
    bus. ``stop_process`` cancels it during shutdown so the loop doesn't
    reference a closed process.
    """

    repo_name: str
    pid: int
    port: int
    proc: asyncio.subprocess.Process
    log_path: Path
    # Populated after spawn so stop_process can cancel cleanly.
    log_task: asyncio.Task[None] | None = field(default=None)


async def spawn_dev(
    repo: RepoConfig,
    worktree: Path,
    port: int,
    extra_env: dict[str, str],
    bug_id: int,
) -> SpawnedProcess:
    """Spawn ``repo.dev_command`` in ``worktree`` with ``{port}`` substituted.

    Runs through ``bash -c`` so shell-isms in the registry's dev_command
    (``cd api && PORT={port} go run ...``) work as-written. The subprocess
    inherits the current env, overlaid with ``extra_env`` (which is also
    overlaid with ``PORT=<port>`` just in case the shell command doesn't
    export it explicitly — double-source is fine, the server reads whichever
    value it actually queries).

    Returns once the process is spawned. The caller should then run
    :func:`wait_for_readiness` to confirm it actually accepted a connection.
    """
    rendered = repo.dev_command.format(port=port)

    env = dict(os.environ)
    env.update(extra_env)
    # Belt-and-braces — the dev_command may or may not set PORT inline.
    env.setdefault("PORT", str(port))

    settings = get_settings()
    log_dir = settings.logs_dir / str(bug_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"test_instance.{repo.name}.log"

    logger.info(
        "Spawning dev for bug=%d repo=%s at %s port=%d cmd=%r",
        bug_id,
        repo.name,
        worktree,
        port,
        rendered,
    )

    proc = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        rendered,
        cwd=str(worktree),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        # New session so SIGTERM to this pid delivers to the shell only —
        # then our pkill -P cleans up descendants. This also detaches the
        # subprocess from our terminal's tty, which prevents Ctrl-C on the
        # triage backend from killing the dev server before we can run
        # graceful shutdown.
        start_new_session=True,
    )

    spawned = SpawnedProcess(
        repo_name=repo.name,
        pid=proc.pid,
        port=port,
        proc=proc,
        log_path=log_path,
    )
    spawned.log_task = asyncio.create_task(
        _pump_output(spawned, bug_id=bug_id),
        name=f"test-instance-log-{repo.name}-{bug_id}",
    )
    return spawned


async def _pump_output(sp: SpawnedProcess, *, bug_id: int) -> None:
    """Drain ``sp.proc.stdout`` line-by-line to the log file + event bus.

    Runs for the lifetime of the dev server. We open the log file with
    line-buffering so external tailers see output promptly. Any exception is
    logged but swallowed — pumping must not be allowed to crash the launch.
    """
    if sp.proc.stdout is None:
        return
    # Line-buffered append. `buffering=1` has a text-mode constraint; we open
    # in text mode and flush each line explicitly.
    try:
        with sp.log_path.open("a", encoding="utf-8", buffering=1) as log_fp:
            while True:
                raw = await sp.proc.stdout.readline()
                if not raw:
                    # EOF — the child closed its stdout. Happens on graceful
                    # shutdown after SIGTERM.
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                # Local file first (never blocking on the bus), then fan out.
                try:
                    log_fp.write(line + "\n")
                except OSError:
                    logger.warning(
                        "Failed to write log line for bug=%d repo=%s",
                        bug_id,
                        sp.repo_name,
                        exc_info=True,
                    )
                publish_test_instance_log(
                    bug_id=bug_id, repo=sp.repo_name, text=line
                )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "log pump crashed for bug=%d repo=%s", bug_id, sp.repo_name
        )


async def wait_for_readiness(port: int, timeout_s: float = 30.0) -> None:
    """Poll ``127.0.0.1:<port>`` until it accepts + answers an HTTP GET.

    Readiness means BOTH conditions hold:

    - TCP: ``socket.create_connection((127.0.0.1, port), timeout=1)`` succeeds.
    - HTTP: ``GET http://127.0.0.1:<port>/`` returns a status code ``< 500``.

    The HTTP probe tolerates 4xx — RA AVM's backend responds 404 to ``/``
    even when fully up, and that still means "alive".

    Raises :class:`ReadinessTimeout` with the last-observed failure message
    if we don't see readiness within ``timeout_s`` seconds.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    last_error: str = "never checked"

    while loop.time() < deadline:
        # Phase 1: TCP. A fast path — if the bind hasn't happened yet, we
        # short-circuit straight to the next poll interval.
        tcp_ok, tcp_err = await asyncio.to_thread(_tcp_probe, port)
        if not tcp_ok:
            last_error = f"TCP: {tcp_err}"
            await asyncio.sleep(0.5)
            continue

        # Phase 2: HTTP GET. Runs in a thread because urllib is sync.
        http_ok, http_err = await asyncio.to_thread(_http_probe, port)
        if http_ok:
            return
        last_error = f"HTTP: {http_err}"
        await asyncio.sleep(0.5)

    raise ReadinessTimeout(
        f"port {port} never became ready within {timeout_s:.0f}s "
        f"(last error: {last_error})"
    )


def _tcp_probe(port: int) -> tuple[bool, str]:
    """Try to open a TCP connection to ``127.0.0.1:<port>``. Return (ok, reason)."""
    try:
        with socket.create_connection((_LOCALHOST, port), timeout=1.0):
            return True, ""
    except (OSError, ConnectionError) as exc:
        return False, str(exc)


def _http_probe(port: int) -> tuple[bool, str]:
    """HTTP GET ``/`` on localhost:port. Return (ok, reason).

    ``ok`` iff status < 500 AND the connection completed. 4xx counts as OK
    because RA AVM responds 404 to ``/`` but is otherwise serving.
    """
    url = f"http://{_LOCALHOST}:{port}/"
    req = Request(url, method="GET")
    try:
        with urlopen(req, timeout=2.0) as resp:  # noqa: S310 - local only, no redirects
            status = int(getattr(resp, "status", 0)) or int(
                resp.getcode() or 0
            )
            if status and status < 500:
                return True, ""
            return False, f"status={status}"
    except Exception as exc:  # urllib raises many shapes — HTTPError, URLError, …
        # HTTPError subclasses URLError and carries a .code; a 404 counts as
        # ready just like in the success branch above.
        code = getattr(exc, "code", None)
        if isinstance(code, int) and code < 500:
            return True, ""
        return False, str(exc)


async def stop_process(sp: SpawnedProcess, term_wait: float = 5.0) -> None:
    """Graceful shutdown: SIGTERM → wait → SIGKILL → pkill -P to mop up.

    Best-effort. Already-exited processes are fine — we swallow the
    ``ProcessLookupError`` and proceed to ``pkill -P`` so any lingering
    grandchildren still get reaped. Called from the
    :class:`TestInstanceManager` on explicit Stop, on Launch-another-bug,
    and on crash reap.
    """
    proc = sp.proc
    pid = sp.pid

    # Phase 1: SIGTERM.
    if proc.returncode is None:
        try:
            # We spawned under a new session; signal the whole process group
            # so the Go/npm/vite grandchildren receive it too.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            logger.warning(
                "SIGTERM to bug=%s repo=%s pid=%d failed",
                sp.repo_name,
                sp.repo_name,
                pid,
                exc_info=True,
            )

        # Phase 2: wait up to term_wait seconds for graceful exit.
        try:
            await asyncio.wait_for(proc.wait(), timeout=term_wait)
        except TimeoutError:
            logger.warning(
                "SIGTERM grace period elapsed for %s; escalating to SIGKILL",
                sp.repo_name,
            )
            # Phase 3: SIGKILL.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2.0)

    # Phase 4: final cleanup — pkill -P to kill any stragglers that survived
    # the process-group signals. ``bash -c`` semantics mean descendants can
    # reattach if a server forks; pkill -P catches those.
    try:
        subprocess.run(
            ["pkill", "-P", str(pid)],
            check=False,
            capture_output=True,
            timeout=5.0,
        )
    except (subprocess.SubprocessError, OSError):
        # pkill isn't installed, or the pid is gone — don't care, we tried.
        pass

    # Stop the log pump so the next subscribe doesn't see a zombie task.
    task = sp.log_task
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


__all__ = [
    "ReadinessTimeout",
    "SpawnedProcess",
    "spawn_dev",
    "stop_process",
    "wait_for_readiness",
]
