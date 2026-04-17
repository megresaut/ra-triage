"""Tests for ``test_runner.processes``.

Doesn't need a real Go/Vite install — we exercise the spawn/wait/stop API
against tiny bash and Python fixtures.
"""

from __future__ import annotations

import asyncio
import http.server
import socket
import threading
from pathlib import Path

import pytest

from ra_triage.config.registry import RepoConfig
from ra_triage.test_runner import processes


def _free_port() -> int:
    """Bind 0 to get a fresh ephemeral port, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_repo(dev_command: str) -> RepoConfig:
    return RepoConfig(
        name="fake-repo",
        path=Path("/tmp"),
        main_branch="main",
        branch_prefix="triage/",
        dev_command=dev_command,
        build_command="true",
        test_command="true",
        dev_port=1234,
        test_launch_port=4321,
        integration_port=9876,
        env_file=".env",
        role="backend",
        github_origin="example.com/fake",
    )


async def test_spawn_dev_streams_output_to_log_and_bus(
    tmp_path: Path, triage_home: Path
) -> None:
    """A tiny bash command writes a line; we see it in the log AND on the bus."""
    from ra_triage.utils.events import bus, topic_test_instance_logs

    queue = await bus.subscribe(topic_test_instance_logs())
    try:
        repo = _make_repo("echo hello-world && sleep 2")
        sp = await processes.spawn_dev(
            repo=repo,
            worktree=tmp_path,
            port=1234,  # unused by this fake command
            extra_env={},
            bug_id=42,
        )

        # Wait for the line to land on the bus (give the pump a short window).
        frame = await asyncio.wait_for(queue.get(), timeout=5.0)
        assert frame["type"] == "test_log"
        assert frame["repo"] == "fake-repo"
        assert "hello-world" in frame["line"]

        # Log file should exist and contain the line.
        await asyncio.sleep(0.1)
        assert sp.log_path.exists()
        log_text = sp.log_path.read_text(encoding="utf-8")
        assert "hello-world" in log_text

        await processes.stop_process(sp, term_wait=1.0)
    finally:
        await bus.unsubscribe(topic_test_instance_logs(), queue)


async def test_stop_process_terminates(tmp_path: Path, triage_home: Path) -> None:
    """A long-running bash child exits cleanly under SIGTERM within term_wait."""
    _ = triage_home
    repo = _make_repo("sleep 60")
    sp = await processes.spawn_dev(
        repo=repo,
        worktree=tmp_path,
        port=1234,
        extra_env={},
        bug_id=42,
    )
    await processes.stop_process(sp, term_wait=2.0)
    assert sp.proc.returncode is not None


async def test_wait_for_readiness_ok_against_live_http() -> None:
    """A real ``http.server.HTTPServer`` on a free port satisfies readiness."""
    port = _free_port()

    handler = http.server.SimpleHTTPRequestHandler
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Should return quickly — well under the 10s timeout.
        await processes.wait_for_readiness(port, timeout_s=10.0)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


async def test_wait_for_readiness_times_out_on_dead_port() -> None:
    """A port with nothing listening raises ReadinessTimeout promptly."""
    port = _free_port()  # binds + releases; nothing is listening now
    with pytest.raises(processes.ReadinessTimeout):
        await processes.wait_for_readiness(port, timeout_s=2.0)


async def test_wait_for_readiness_tolerates_404() -> None:
    """A server that responds 404 still counts as ready (<500)."""
    port = _free_port()

    class _AlwaysFourOhFour(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(404)
            self.end_headers()

        def log_message(self, *_args: object, **_kwargs: object) -> None:  # noqa: D401
            """Silence default request logging in test output."""

    server = http.server.HTTPServer(("127.0.0.1", port), _AlwaysFourOhFour)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        await processes.wait_for_readiness(port, timeout_s=5.0)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)
