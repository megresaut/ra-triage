"""Test that the test-instance reaper broadcasts crash + log tail.

Agent 12 polish: when ``TestInstanceManager.reap()`` flips a running
instance's status to ``crashed``, the broadcast frame must include
``reason="process_exited"`` and ``log_tails`` (a per-repo dict of the
last 20 lines of the proc's log file).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ra_triage.test_runner import instance as instance_module
from ra_triage.test_runner.instance import RunningInstance, TestInstanceManager
from ra_triage.test_runner.processes import SpawnedProcess
from ra_triage.utils.events import bus, topic_test_instance_status


def _make_spawned(
    *,
    repo_name: str,
    pid: int,
    port: int,
    log_path: Path,
    returncode: int | None,
) -> SpawnedProcess:
    fake_proc = MagicMock()
    fake_proc.returncode = returncode
    return SpawnedProcess(
        repo_name=repo_name,
        pid=pid,
        port=port,
        proc=fake_proc,
        log_path=log_path,
    )


async def test_reap_broadcasts_crashed_with_log_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alembic_db: Path
) -> None:
    """A crashed proc → WS frame with reason + log_tails populated."""
    # Wire a manager with a fake running instance.
    mgr = TestInstanceManager()

    log_path_be = tmp_path / "be.log"
    log_path_fe = tmp_path / "fe.log"
    # Write 30 lines to backend log (so we can verify the tail trims to 20).
    log_path_be.write_text(
        "\n".join(f"be line {i}" for i in range(1, 31)) + "\n",
        encoding="utf-8",
    )
    log_path_fe.write_text("fe live line\n", encoding="utf-8")

    # Pretend the BE proc died (returncode=1) and FE is still alive.
    procs = {
        "avm-backend": _make_spawned(
            repo_name="avm-backend",
            pid=1234,
            port=18787,
            log_path=log_path_be,
            returncode=1,
        ),
        "avm-frontend": _make_spawned(
            repo_name="avm-frontend",
            pid=5678,
            port=15174,
            log_path=log_path_fe,
            returncode=None,
        ),
    }
    mgr._current = RunningInstance(
        bug_id=42,
        procs=procs,
        snapshot_hash="hash",
        db_instance_row_id=1,
    )

    # Stub out the DB write + the live-proc cleanup to keep the test
    # purely about the broadcast.
    monkeypatch.setattr(mgr, "_mark_row_crashed", lambda row_id: None)

    async def _noop_stop(sp: Any) -> None:
        return None

    monkeypatch.setattr(
        "ra_triage.test_runner.processes.stop_process", _noop_stop
    )

    # Subscribe before the reap so we don't miss the broadcast.
    queue = await bus.subscribe(topic_test_instance_status())
    try:
        crashed = await mgr.reap()
        assert crashed is True

        try:
            frame = await asyncio.wait_for(queue.get(), timeout=1.0)
        except TimeoutError:  # pragma: no cover
            pytest.fail("Expected a test_instance_status crash broadcast")
    finally:
        await bus.unsubscribe(topic_test_instance_status(), queue)

    assert frame["type"] == "test_instance_status"
    assert frame["status"] == "crashed"
    assert frame["bug_id"] == 42
    assert frame["reason"] == "process_exited"
    assert "log_tails" in frame
    log_tails = frame["log_tails"]
    assert "avm-backend" in log_tails
    assert "avm-frontend" in log_tails
    # Tail of BE should be the LAST 20 lines (lines 11..30).
    assert len(log_tails["avm-backend"]) == 20
    assert log_tails["avm-backend"][0] == "be line 11"
    assert log_tails["avm-backend"][-1] == "be line 30"
    # FE has only one line.
    assert log_tails["avm-frontend"] == ["fe live line"]


async def test_reap_releases_lock_via_clearing_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alembic_db: Path
) -> None:
    """After a crash, ``_current`` is None so the next launch is allowed."""
    mgr = TestInstanceManager()
    log_path = tmp_path / "be.log"
    log_path.write_text("oops\n", encoding="utf-8")
    procs = {
        "avm-backend": _make_spawned(
            repo_name="avm-backend",
            pid=42,
            port=18787,
            log_path=log_path,
            returncode=137,
        )
    }
    mgr._current = RunningInstance(
        bug_id=7,
        procs=procs,
        snapshot_hash="",
        db_instance_row_id=1,
    )
    monkeypatch.setattr(mgr, "_mark_row_crashed", lambda row_id: None)
    async def _noop_stop(sp: Any) -> None:
        return None

    monkeypatch.setattr(
        "ra_triage.test_runner.processes.stop_process", _noop_stop
    )

    crashed = await mgr.reap()
    assert crashed is True
    assert mgr._current is None


def test_tail_log_helper_reads_last_n_lines(tmp_path: Path) -> None:
    """The internal ``_tail_log`` helper returns the last n lines (no \\n)."""
    log_path = tmp_path / "tail.log"
    log_path.write_text(
        "\n".join(f"line {i}" for i in range(1, 51)) + "\n",
        encoding="utf-8",
    )
    out = instance_module._tail_log(log_path, 5)
    assert out == ["line 46", "line 47", "line 48", "line 49", "line 50"]


def test_tail_log_helper_handles_missing_file(tmp_path: Path) -> None:
    out = instance_module._tail_log(tmp_path / "nope.log", 5)
    assert out == []
