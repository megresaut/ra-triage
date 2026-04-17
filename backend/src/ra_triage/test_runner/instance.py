"""Global test-instance manager — at most one live per backend process.

Launch-for-Testing spins up RA AVM's dev servers from a specific bug's
worktree, on dedicated test ports (18080 backend / 15173 frontend). A single
asyncio lock in this module guarantees at most one instance is alive at a
time across the whole backend — switching bugs means the old instance is
stopped before the new one spawns.

Lifecycle
---------

``status`` values on ``test_instances``:

    starting  — row inserted; procs being spawned + readiness polled.
    running   — all procs passed readiness; pid/port maps are live.
    stopping  — shutdown requested; procs receiving SIGTERM (transient).
    stopped   — all procs exited; row is final.
    crashed   — the reaper observed a proc exit mid-running; the lock has
                been released so another launch is allowed. Row is final.

npm_install cache
-----------------

Frontend repos (``role="frontend"``) need ``node_modules/`` under the
worktree before we spawn vite. We key the cache on a hash of
``package.json`` + ``package-lock.json`` under the worktree: if a marker
file at ``<triage_home>/npm-cache/<repo_name>/<hash>`` exists, we skip ``npm
ci``; otherwise we run it once and drop the marker. Backend (Go) repos skip
this entirely.

Browser open
------------

On successful launch we ``webbrowser.open("http://127.0.0.1:<frontend-port>")``
— the frontend's ``test_launch_port`` is the landing surface. If the project
has no frontend repo (future projects), we open the first repo's port
instead so something happens.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import webbrowser
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ra_triage.config.registry import ProjectConfig, RepoConfig, get_project
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.db.models import Bug, TestInstance
from ra_triage.settings import get_settings
from ra_triage.test_runner import db_snapshot, processes
from ra_triage.test_runner.processes import (
    ReadinessTimeout,
    SpawnedProcess,
)
from ra_triage.utils.events import publish_test_instance_status
from ra_triage.workspaces import worktree as wt_module

logger = logging.getLogger(__name__)


# Readiness timeout per dev server. 30s is comfortable for Vite's cold
# start; Go is much faster. Tuned per Agent 7 brief.
_READINESS_TIMEOUT_S: float = 30.0


class LaunchFailed(RuntimeError):
    """Raised by :meth:`TestInstanceManager.launch` when spawn or readiness fails.

    Carries any partially-spawned procs that were stopped inside ``.extra``
    for test assertions / debugging.
    """

    def __init__(self, message: str, *, extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.extra = extra or {}


@dataclass
class RunningInstance:
    """Snapshot of the active instance.

    Lives only in memory on the manager; the durable record is the
    ``test_instances`` row referenced by ``db_instance_row_id``.
    """

    bug_id: int
    procs: dict[str, SpawnedProcess] = field(default_factory=dict)
    snapshot_hash: str = ""
    db_instance_row_id: int = 0
    launched_at: datetime = field(default_factory=lambda: _utcnow())
    # The port the browser was opened on — kept here so the /current endpoint
    # can return it without re-computing from the registry.
    browser_port: int | None = None


class TestInstanceManager:
    """Single-instance orchestrator for Launch-for-Testing."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._current: RunningInstance | None = None
        # Background reaper task handle — set by main.py lifespan.
        self._reaper_task: asyncio.Task[None] | None = None
        self._reaper_stop = asyncio.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def launch(self, bug_id: int) -> RunningInstance:
        """Launch a test instance for ``bug_id``. Stops a previous one first.

        Steps, all under the manager lock:

          1. If an instance is currently running/starting, stop it (publish
             ``stopping`` → ``stopped``).
          2. Load the bug + resolve the project. Fail if the bug doesn't
             exist, is terminal, or is in a state that can't be tested.
          3. Restore the test DB from a ``pg_dump`` snapshot.
          4. Ensure ``npm ci`` has been run in each frontend worktree (cache
             keyed by ``package.json`` + ``package-lock.json`` hash). Skips
             backend (Go) repos.
          5. Spawn each repo's dev command on its ``test_launch_port``.
          6. Poll readiness in parallel — TCP + HTTP GET < 500 — with a 30s
             ceiling per process.
          7. Update the ``test_instances`` row to ``running`` with real
             pid/port maps.
          8. Open the browser to the frontend's test_launch_port.
          9. Broadcast ``test_instance_status`` on the event bus.

        On any failure, any partially-spawned procs are stopped, the DB row
        is flipped to ``crashed``, the lock is released, and
        :class:`LaunchFailed` is raised with context.
        """
        async with self._lock:
            await self._stop_current_locked()

            bug = await asyncio.to_thread(self._load_bug_sync, bug_id)
            if bug is None:
                raise LaunchFailed(f"bug_id={bug_id} not found")
            project = get_project(bug["project_id"])

            # Insert the tracker row up-front so the UI sees ``starting``
            # immediately (and a crash during spawn still leaves an audit
            # trail).
            row_id = await asyncio.to_thread(
                self._insert_starting_row, bug_id
            )
            await self._broadcast_status(
                bug_id=bug_id,
                status="starting",
                row_id=row_id,
                ports={},
                pids={},
            )

            try:
                snapshot_hash = await self._restore_db(project)
                await self._ensure_npm_install(bug_id, project)
                spawned = await self._spawn_all(
                    bug_id=bug_id, project=project
                )
                await self._wait_readiness(spawned)
            except (ReadinessTimeout, db_snapshot.SnapshotError) as exc:
                await self._cleanup_partial_spawn(bug_id, row_id, locals().get("spawned") or {})
                raise LaunchFailed(
                    f"Launch for bug {bug_id} failed: {exc}",
                ) from exc
            except Exception as exc:
                await self._cleanup_partial_spawn(bug_id, row_id, locals().get("spawned") or {})
                raise LaunchFailed(
                    f"Launch for bug {bug_id} crashed: {exc}",
                ) from exc

            ports = {name: sp.port for name, sp in spawned.items()}
            pids = {name: sp.pid for name, sp in spawned.items()}
            await asyncio.to_thread(
                self._finalize_running_row,
                row_id,
                pids,
                ports,
                snapshot_hash,
            )

            browser_port = _pick_browser_port(project, ports)
            instance = RunningInstance(
                bug_id=bug_id,
                procs=spawned,
                snapshot_hash=snapshot_hash,
                db_instance_row_id=row_id,
                launched_at=_utcnow(),
                browser_port=browser_port,
            )
            self._current = instance

            await self._broadcast_status(
                bug_id=bug_id,
                status="running",
                row_id=row_id,
                ports=ports,
                pids=pids,
                db_snapshot_hash=snapshot_hash,
                browser_port=browser_port,
            )

            if browser_port is not None:
                try:
                    webbrowser.open(f"http://127.0.0.1:{browser_port}")
                except Exception:
                    # Headless envs (CI, tests) may have no browser. Log and
                    # proceed — the launch itself is still successful.
                    logger.warning(
                        "webbrowser.open failed; instance is up at port %d",
                        browser_port,
                        exc_info=True,
                    )

            return instance

    async def stop(self) -> None:
        """Graceful shutdown of the current instance, if any. No-op if none."""
        async with self._lock:
            await self._stop_current_locked()

    async def current(self) -> dict[str, Any] | None:
        """Serializable view of the live instance, or None."""
        inst = self._current
        if inst is None:
            return None
        return {
            "bug_id": inst.bug_id,
            "status": "running",
            "ports": {name: sp.port for name, sp in inst.procs.items()},
            "pids": {name: sp.pid for name, sp in inst.procs.items()},
            "db_snapshot_hash": inst.snapshot_hash,
            "launched_at": inst.launched_at.isoformat(),
            "browser_port": inst.browser_port,
        }

    # ------------------------------------------------------------------
    # Reaper
    # ------------------------------------------------------------------

    async def reap(self) -> bool:
        """One-shot reap check. Return True if a crash was detected.

        If any proc in the current instance has exited unexpectedly while
        status is still ``running``, mark the row ``crashed``, release the
        lock by clearing ``_current``, and broadcast. Runs outside the
        manager lock so a hung shutdown can't stall the reaper.
        """
        inst = self._current
        if inst is None:
            return False
        dead = [name for name, sp in inst.procs.items() if sp.proc.returncode is not None]
        if not dead:
            return False

        logger.warning(
            "Test instance for bug %d crashed — dead procs: %s",
            inst.bug_id,
            dead,
        )
        # Stop whatever is still alive so we don't leak file descriptors.
        for sp in inst.procs.values():
            if sp.proc.returncode is None:
                try:
                    await processes.stop_process(sp)
                except Exception:
                    logger.exception("cleanup of live proc during crash reap")

        await asyncio.to_thread(
            self._mark_row_crashed, inst.db_instance_row_id
        )
        await self._broadcast_status(
            bug_id=inst.bug_id,
            status="crashed",
            row_id=inst.db_instance_row_id,
            ports={name: sp.port for name, sp in inst.procs.items()},
            pids={name: sp.pid for name, sp in inst.procs.items()},
            dead_procs=dead,
        )
        self._current = None
        return True

    async def reap_forever(self, interval_s: float = 1.0) -> None:
        """Forever poll ``reap()`` at ``interval_s`` cadence.

        Scheduled by ``main.py`` during lifespan. Exits cleanly on
        ``_reaper_stop`` (set during lifespan shutdown).
        """
        try:
            while not self._reaper_stop.is_set():
                try:
                    await self.reap()
                except Exception:
                    logger.exception("reaper tick failed")
                try:
                    await asyncio.wait_for(
                        self._reaper_stop.wait(), timeout=interval_s
                    )
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise

    def start_reaper(self) -> asyncio.Task[None]:
        """Schedule ``reap_forever`` as a background task. Idempotent."""
        if self._reaper_task is not None and not self._reaper_task.done():
            return self._reaper_task
        self._reaper_stop.clear()
        self._reaper_task = asyncio.create_task(
            self.reap_forever(), name="test-instance-reaper"
        )
        return self._reaper_task

    async def shutdown(self) -> None:
        """Stop reaper + stop current instance. Called from lifespan exit."""
        self._reaper_stop.set()
        task = self._reaper_task
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=3.0)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
            except Exception:
                logger.exception("reaper shutdown raised")
        self._reaper_task = None
        await self.stop()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _stop_current_locked(self) -> None:
        """Shut down the current instance while holding the manager lock.

        Publishes ``stopping`` → ``stopped`` so the frontend can show the
        transition; clears ``_current`` last.
        """
        inst = self._current
        if inst is None:
            return
        row_id = inst.db_instance_row_id
        ports = {name: sp.port for name, sp in inst.procs.items()}
        pids = {name: sp.pid for name, sp in inst.procs.items()}
        await self._broadcast_status(
            bug_id=inst.bug_id,
            status="stopping",
            row_id=row_id,
            ports=ports,
            pids=pids,
        )
        for sp in inst.procs.values():
            try:
                await processes.stop_process(sp)
            except Exception:
                logger.exception("stop_process failed for %s", sp.repo_name)
        await asyncio.to_thread(self._mark_row_stopped, row_id)
        await self._broadcast_status(
            bug_id=inst.bug_id,
            status="stopped",
            row_id=row_id,
            ports=ports,
            pids=pids,
        )
        self._current = None

    async def _cleanup_partial_spawn(
        self,
        bug_id: int,
        row_id: int,
        spawned: dict[str, SpawnedProcess],
    ) -> None:
        """Stop any partially-spawned procs and flip the DB row to crashed."""
        for sp in spawned.values():
            try:
                await processes.stop_process(sp)
            except Exception:
                logger.exception("cleanup after failed spawn for %s", sp.repo_name)
        await asyncio.to_thread(self._mark_row_crashed, row_id)
        await self._broadcast_status(
            bug_id=bug_id,
            status="crashed",
            row_id=row_id,
            ports={name: sp.port for name, sp in spawned.items()},
            pids={name: sp.pid for name, sp in spawned.items()},
        )

    async def _restore_db(self, project: ProjectConfig) -> str:
        """Restore the test DB from the project's dev DB snapshot.

        We use the backend repo's ``database_url`` / ``test_database_url``
        pair as the project's DB config — AVM's only DB-backed repo is the
        backend. Projects with no DB-backed repo skip this step entirely.
        """
        backend_repo = _pick_db_repo(project)
        if backend_repo is None:
            return ""
        if not backend_repo.database_url or not backend_repo.test_database_url:
            return ""
        test_db_name = _test_db_name_from_url(backend_repo.test_database_url)
        return await asyncio.to_thread(
            db_snapshot.restore_test_db,
            project.id,
            backend_repo.database_url,
            backend_repo.test_database_url,
            test_db_name,
        )

    async def _ensure_npm_install(self, bug_id: int, project: ProjectConfig) -> None:
        """Run ``npm ci`` in each frontend worktree if the hash marker is stale.

        Cache marker path: ``<triage_home>/npm-cache/<repo_name>/<hash>``. On
        a hit the call is a no-op. On a miss we run ``npm ci`` in the
        worktree and drop the marker.
        """
        for repo in project.repos:
            if repo.role != "frontend":
                continue
            worktree = wt_module.path_for(bug_id, repo.name)
            if not worktree.is_dir():
                # The orchestrator's worktree setup must have failed — we
                # can't install here. Let the spawn step fail with a clearer
                # message.
                continue
            current_hash = _npm_lock_hash(worktree)
            marker = _npm_cache_marker(repo.name, current_hash)
            if marker.exists():
                logger.info(
                    "npm cache hit for %s @ %s; skipping npm ci",
                    repo.name,
                    current_hash,
                )
                continue
            logger.info(
                "npm cache miss for %s @ %s; running npm ci in %s",
                repo.name,
                current_hash,
                worktree,
            )
            await _run_npm_ci(worktree)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                f"installed at {_utcnow().isoformat()}\n", encoding="utf-8"
            )

    async def _spawn_all(
        self, *, bug_id: int, project: ProjectConfig
    ) -> dict[str, SpawnedProcess]:
        """Spawn one dev server per repo on its ``test_launch_port``.

        Returns a ``{repo_name: SpawnedProcess}`` map. Raises on the first
        failed spawn — the caller cleans up via ``_cleanup_partial_spawn``.
        """
        spawned: dict[str, SpawnedProcess] = {}
        for repo in project.repos:
            worktree = wt_module.path_for(bug_id, repo.name)
            if not worktree.is_dir():
                raise LaunchFailed(
                    f"Worktree missing for bug={bug_id} repo={repo.name} at {worktree}"
                )
            extra_env = _env_overrides_for(repo)
            sp = await processes.spawn_dev(
                repo=repo,
                worktree=worktree,
                port=repo.test_launch_port,
                extra_env=extra_env,
                bug_id=bug_id,
            )
            spawned[repo.name] = sp
        return spawned

    async def _wait_readiness(self, spawned: dict[str, SpawnedProcess]) -> None:
        """Wait for every spawned proc to become ready, in parallel.

        Raises :class:`ReadinessTimeout` if ANY proc times out. The caller
        cleans up the partial spawn.
        """
        await asyncio.gather(
            *(
                processes.wait_for_readiness(sp.port, _READINESS_TIMEOUT_S)
                for sp in spawned.values()
            )
        )

    async def _broadcast_status(
        self,
        *,
        bug_id: int,
        status: str,
        row_id: int,
        ports: dict[str, int],
        pids: dict[str, int],
        **extras: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "bug_id": bug_id,
            "status": status,
            "row_id": row_id,
            "ports": ports,
            "pids": pids,
        }
        payload.update(extras)
        publish_test_instance_status(payload)

    # ---- Sync DB helpers (called via asyncio.to_thread) ----

    def _load_bug_sync(self, bug_id: int) -> dict[str, Any] | None:
        session = engine_module.SessionLocal()
        try:
            bug = repo_module.get_bug(session, bug_id)
            if bug is None:
                return None
            return _bug_to_dict(bug)
        finally:
            session.close()

    def _insert_starting_row(self, bug_id: int) -> int:
        session = engine_module.SessionLocal()
        try:
            row = repo_module.create_test_instance(
                session, bug_id=bug_id, pids={}, ports={}, status="starting"
            )
            session.commit()
            return int(row.id)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _finalize_running_row(
        self,
        row_id: int,
        pids: dict[str, int],
        ports: dict[str, int],
        snapshot_hash: str,
    ) -> None:
        session = engine_module.SessionLocal()
        try:
            repo_module.update_test_instance_status(
                session,
                row_id,
                status="running",
                pids=pids,
                ports=ports,
                db_snapshot_hash=snapshot_hash or None,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _mark_row_stopped(self, row_id: int) -> None:
        session = engine_module.SessionLocal()
        try:
            repo_module.update_test_instance_status(session, row_id, status="stopped")
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _mark_row_crashed(self, row_id: int) -> None:
        session = engine_module.SessionLocal()
        try:
            repo_module.update_test_instance_status(session, row_id, status="crashed")
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


# --- module-level singleton -------------------------------------------------


_MANAGER: TestInstanceManager | None = None


def get_test_instance_manager() -> TestInstanceManager:
    """Return the process-wide manager, creating it lazily.

    Lazy so pytest can monkeypatch ``_MANAGER`` in tests that need a fresh
    instance per test (see ``test_instance_manager.py``).
    """
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = TestInstanceManager()
    return _MANAGER


def reset_manager_for_tests() -> None:
    """Tear down the module-level singleton. Tests only."""
    global _MANAGER
    _MANAGER = None


# --- helpers ---------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _bug_to_dict(bug: Bug) -> dict[str, Any]:
    return {
        "id": bug.id,
        "project_id": bug.project_id,
        "title": bug.title,
        "state": bug.state,
    }


def _pick_db_repo(project: ProjectConfig) -> RepoConfig | None:
    """Return the first repo in the project with a ``database_url`` set."""
    for r in project.repos:
        if r.database_url:
            return r
    return None


def _test_db_name_from_url(url: str) -> str:
    """Extract the DB name from a ``postgres://.../<dbname>`` URL.

    Defaults to ``ra_triage_test`` if the URL is missing a path or looks
    malformed — which matches the Agent 7 brief's default.
    """
    from urllib.parse import urlparse

    path = urlparse(url).path.lstrip("/")
    return path.split("?", 1)[0] or "ra_triage_test"


def _pick_browser_port(
    project: ProjectConfig, ports: dict[str, int]
) -> int | None:
    """Return the port of the frontend repo (preferred), else the first port."""
    for r in project.repos:
        if r.role == "frontend" and r.name in ports:
            return ports[r.name]
    return next(iter(ports.values()), None)


def _env_overrides_for(repo: RepoConfig) -> dict[str, str]:
    """Env overrides applied to each spawned dev server.

    We route DATABASE_URL at the *test* DB so a bug's migrations / writes
    can't touch Megha's actual dev data. Agent 7's brief mandates this.
    """
    env: dict[str, str] = {"PORT": str(repo.test_launch_port)}
    if repo.test_database_url:
        env["DATABASE_URL"] = repo.test_database_url
    return env


def _npm_lock_hash(worktree: Path) -> str:
    """Short sha256 prefix over ``package.json`` + ``package-lock.json``.

    Missing files contribute ``""`` — that way a repo with no lockfile still
    yields a deterministic marker (and ``npm ci`` will fail loudly with a
    clear error on the first run, which is the right signal).
    """
    digest = hashlib.sha256()
    for fname in ("package.json", "package-lock.json"):
        path = worktree / fname
        if path.exists():
            digest.update(path.read_bytes())
        digest.update(b"\n--sep--\n")
    return digest.hexdigest()[:12]


def _npm_cache_marker(repo_name: str, lock_hash: str) -> Path:
    return get_settings().triage_home / "npm-cache" / repo_name / lock_hash


async def _run_npm_ci(worktree: Path) -> None:
    """Run ``npm ci`` in ``worktree``. Raise :class:`LaunchFailed` on error.

    We don't tee this through the event bus — npm output is noisy and
    Megha inspects it via the log file at
    ``<triage_home>/logs/npm-ci.<repo>.log`` only when something breaks.
    """
    proc = await asyncio.create_subprocess_exec(
        "npm",
        "ci",
        cwd=str(worktree),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=600.0)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise LaunchFailed(f"npm ci in {worktree} timed out after 10m") from exc
    if proc.returncode != 0:
        tail = (stdout or b"").decode("utf-8", errors="replace")[-1500:]
        raise LaunchFailed(
            f"npm ci in {worktree} failed (exit {proc.returncode}):\n{tail}"
        )


# --- Lightweight status read for the API --------------------------------


def most_recent_instance_snapshot() -> dict[str, Any] | None:
    """Read the most recent ``test_instances`` row, regardless of status.

    Used by the WS handler to show 'last known status' on an initial
    connect when there's no currently-running instance — helps the frontend
    distinguish 'crashed' / 'stopped' from 'never launched'.
    """
    session = engine_module.SessionLocal()
    try:
        from sqlalchemy import select

        stmt = (
            select(TestInstance)
            .order_by(TestInstance.launched_at.desc())
            .limit(1)
        )
        row = session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return {
            "bug_id": row.bug_id,
            "status": row.status,
            "ports": json.loads(row.ports or "{}"),
            "pids": json.loads(row.pids or "{}"),
            "db_snapshot_hash": row.db_snapshot_hash,
            "launched_at": row.launched_at.isoformat() if row.launched_at else None,
            "stopped_at": row.stopped_at.isoformat() if row.stopped_at else None,
        }
    finally:
        session.close()


__all__ = [
    "LaunchFailed",
    "RunningInstance",
    "TestInstanceManager",
    "get_test_instance_manager",
    "most_recent_instance_snapshot",
    "reset_manager_for_tests",
]
