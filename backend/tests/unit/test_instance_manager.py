"""Tests for ``test_runner.instance.TestInstanceManager``.

We mock ``processes.spawn_dev``, ``processes.wait_for_readiness``,
``processes.stop_process``, and ``db_snapshot.restore_test_db`` so we
don't need a real Postgres DB or real Go/Vite installs. Assertions
focus on:

  - Lock semantics: launching a second bug stops the first.
  - DB rows: ``starting`` → ``running``; stopped flips to ``stopped``.
  - ``launched_for_testing`` flag round-trip on ``BugResponse``.
  - Reaper detects a crashed proc and flips to ``crashed``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.orchestrator.states import BugState
from ra_triage.test_runner import db_snapshot, instance, processes
from ra_triage.test_runner.instance import (
    LaunchFailed,
    TestInstanceManager,
    reset_manager_for_tests,
)

# --- fakes -----------------------------------------------------------------


@dataclass
class _FakeProc:
    """Stand-in for ``asyncio.subprocess.Process``."""

    returncode: int | None = None
    stdout: Any = None

    async def wait(self) -> int:
        return self.returncode or 0


def _fake_spawned(repo_name: str, port: int, pid: int) -> processes.SpawnedProcess:
    return processes.SpawnedProcess(
        repo_name=repo_name,
        pid=pid,
        port=port,
        # _FakeProc is a duck-typed stand-in — runtime fine, type-check
        # suppressed deliberately.
        proc=_FakeProc(),  # type: ignore[arg-type]  # noqa: PGH003
        log_path=Path("/dev/null"),
        log_task=None,
    )


def _patch_processes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Patch processes.* to synchronous fakes; return the spawn log."""
    spawned_log: list[tuple[str, int]] = []
    pid_counter = [1000]

    async def _fake_spawn_dev(
        repo: Any, worktree: Any, port: int, extra_env: Any, bug_id: int
    ) -> processes.SpawnedProcess:
        pid_counter[0] += 1
        spawned_log.append((repo.name, port))
        return _fake_spawned(repo.name, port, pid_counter[0])

    async def _fake_wait_for_readiness(port: int, timeout_s: float = 30.0) -> None:
        return None

    async def _fake_stop_process(sp: processes.SpawnedProcess, term_wait: float = 5.0) -> None:
        # Emulate a clean exit. ``Process.returncode`` is read-only at the
        # type level; our fake is a dataclass so object.__setattr__ is the
        # portable way to flip it without a mypy complaint.
        object.__setattr__(sp.proc, "returncode", 0)

    monkeypatch.setattr(processes, "spawn_dev", _fake_spawn_dev)
    monkeypatch.setattr(processes, "wait_for_readiness", _fake_wait_for_readiness)
    monkeypatch.setattr(processes, "stop_process", _fake_stop_process)
    # Also patch the instance-module re-exports, because it imported by name.
    monkeypatch.setattr(instance.processes, "spawn_dev", _fake_spawn_dev)
    monkeypatch.setattr(instance.processes, "wait_for_readiness", _fake_wait_for_readiness)
    monkeypatch.setattr(instance.processes, "stop_process", _fake_stop_process)
    return spawned_log


def _patch_db_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_restore(
        project_id: str,
        dev_url: str,
        test_url: str,
        test_db_name: str = "ra_triage_test",
    ) -> str:
        return "fakehash0000"

    monkeypatch.setattr(db_snapshot, "restore_test_db", _fake_restore)
    monkeypatch.setattr(instance.db_snapshot, "restore_test_db", _fake_restore)


def _patch_npm_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable real npm ci — we don't have node_modules in the fake worktrees."""

    async def _noop(worktree: Path) -> None:
        return None

    monkeypatch.setattr(instance, "_run_npm_ci", _noop)


def _patch_webbrowser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch webbrowser.open — return the list of URLs the manager tried to open."""
    opened: list[str] = []
    monkeypatch.setattr(instance.webbrowser, "open", opened.append)
    return opened


def _create_worktrees_on_disk(triage_home: Path, bug_id: int) -> None:
    """Create the worktree dirs the manager expects for AVM's two repos."""
    for repo_name in ("avm-backend", "builder-frontend"):
        (triage_home / "worktrees" / str(bug_id) / repo_name).mkdir(
            parents=True, exist_ok=True
        )


def _seed_bug(session_factory: Any, notion_id: str, project_id: str = "ra_avm") -> int:
    """Insert a bug + walk its state to READY_FOR_REVIEW so launch is legal."""
    session = session_factory()
    try:
        bug = repo_module.create_bug(
            session,
            notion_page_id=notion_id,
            project_id=project_id,
            title="fake bug",
            description="fake",
        )
        # Transition through the states so we exercise a realistic bug row,
        # though ``launch`` doesn't currently gate on state.
        for s in [
            BugState.TRIAGED,
            BugState.INVESTIGATING,
            BugState.FIXING,
            BugState.TESTING,
            BugState.READY_FOR_REVIEW,
        ]:
            repo_module.update_bug_state(session, bug, s)
        session.commit()
        return int(bug.id)
    finally:
        session.close()


# --- tests -----------------------------------------------------------------


async def test_launch_persists_running_row_and_reports_current(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """launch() creates a 'running' row; current() reflects it."""
    reset_manager_for_tests()
    _patch_processes(monkeypatch)
    _patch_db_snapshot(monkeypatch)
    _patch_npm_install(monkeypatch)
    _patch_webbrowser(monkeypatch)

    bug_id = _seed_bug(engine_module.SessionLocal, "launch-1")
    _create_worktrees_on_disk(triage_home, bug_id)

    mgr = TestInstanceManager()
    inst = await mgr.launch(bug_id)
    assert inst.bug_id == bug_id

    snapshot = await mgr.current()
    assert snapshot is not None
    assert snapshot["bug_id"] == bug_id
    assert snapshot["status"] == "running"
    assert set(snapshot["ports"].keys()) == {"avm-backend", "builder-frontend"}
    assert snapshot["ports"]["avm-backend"] == 18080
    assert snapshot["ports"]["builder-frontend"] == 15173

    # DB row should be in 'running' status.
    session = engine_module.SessionLocal()
    try:
        current = repo_module.get_current_test_instance(session)
        assert current is not None
        assert current.bug_id == bug_id
        assert current.status == "running"
        assert current.db_snapshot_hash == "fakehash0000"
    finally:
        session.close()

    await mgr.stop()


async def test_launch_second_bug_stops_first(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Launching bug B while bug A is running stops A and leaves only B live."""
    reset_manager_for_tests()
    _patch_processes(monkeypatch)
    _patch_db_snapshot(monkeypatch)
    _patch_npm_install(monkeypatch)
    _patch_webbrowser(monkeypatch)

    bug_a = _seed_bug(engine_module.SessionLocal, "launch-a")
    bug_b = _seed_bug(engine_module.SessionLocal, "launch-b")
    _create_worktrees_on_disk(triage_home, bug_a)
    _create_worktrees_on_disk(triage_home, bug_b)

    mgr = TestInstanceManager()
    await mgr.launch(bug_a)
    await mgr.launch(bug_b)

    snap = await mgr.current()
    assert snap is not None
    assert snap["bug_id"] == bug_b

    # DB should carry a stopped A + running B.
    session = engine_module.SessionLocal()
    try:
        from sqlalchemy import select

        from ra_triage.db.models import TestInstance

        rows = list(
            session.execute(
                select(TestInstance).order_by(TestInstance.launched_at.asc())
            ).scalars()
        )
        assert len(rows) == 2
        assert rows[0].bug_id == bug_a
        assert rows[0].status == "stopped"
        assert rows[0].stopped_at is not None
        assert rows[1].bug_id == bug_b
        assert rows[1].status == "running"
    finally:
        session.close()

    await mgr.stop()


async def test_stop_frees_current_and_marks_row(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop flips the row to stopped and current() returns None."""
    reset_manager_for_tests()
    _patch_processes(monkeypatch)
    _patch_db_snapshot(monkeypatch)
    _patch_npm_install(monkeypatch)
    _patch_webbrowser(monkeypatch)

    bug_id = _seed_bug(engine_module.SessionLocal, "stop-1")
    _create_worktrees_on_disk(triage_home, bug_id)

    mgr = TestInstanceManager()
    await mgr.launch(bug_id)
    await mgr.stop()

    assert await mgr.current() is None
    session = engine_module.SessionLocal()
    try:
        from sqlalchemy import select

        from ra_triage.db.models import TestInstance

        row = session.execute(
            select(TestInstance).where(TestInstance.bug_id == bug_id)
        ).scalar_one()
        assert row.status == "stopped"
        assert row.stopped_at is not None
    finally:
        session.close()


async def test_reaper_flips_crashed_when_proc_exits(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a spawned proc's returncode becomes non-None, reap() reports crashed."""
    reset_manager_for_tests()
    _patch_processes(monkeypatch)
    _patch_db_snapshot(monkeypatch)
    _patch_npm_install(monkeypatch)
    _patch_webbrowser(monkeypatch)

    bug_id = _seed_bug(engine_module.SessionLocal, "crash-1")
    _create_worktrees_on_disk(triage_home, bug_id)

    mgr = TestInstanceManager()
    inst = await mgr.launch(bug_id)

    # Simulate a crash on one of the procs. object.__setattr__ sidesteps
    # asyncio.subprocess.Process.returncode being declared read-only at the
    # type level; our fake dataclass accepts the write fine.
    first = next(iter(inst.procs.values()))
    object.__setattr__(first.proc, "returncode", 7)

    crashed = await mgr.reap()
    assert crashed is True
    assert await mgr.current() is None

    session = engine_module.SessionLocal()
    try:
        from sqlalchemy import select

        from ra_triage.db.models import TestInstance

        row = session.execute(
            select(TestInstance).where(TestInstance.bug_id == bug_id)
        ).scalar_one()
        assert row.status == "crashed"
    finally:
        session.close()


async def test_launched_for_testing_flag_roundtrip(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_current_test_instance`` reports the right bug_id while running."""
    reset_manager_for_tests()
    _patch_processes(monkeypatch)
    _patch_db_snapshot(monkeypatch)
    _patch_npm_install(monkeypatch)
    _patch_webbrowser(monkeypatch)

    bug_id = _seed_bug(engine_module.SessionLocal, "flag-1")
    _create_worktrees_on_disk(triage_home, bug_id)

    mgr = TestInstanceManager()
    await mgr.launch(bug_id)

    session = engine_module.SessionLocal()
    try:
        current = repo_module.get_current_test_instance(session)
        assert current is not None
        assert current.bug_id == bug_id
    finally:
        session.close()

    await mgr.stop()
    session = engine_module.SessionLocal()
    try:
        current = repo_module.get_current_test_instance(session)
        assert current is None
    finally:
        session.close()


async def test_launch_missing_worktree_raises_launch_failed(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Launch fails cleanly with LaunchFailed if worktrees aren't on disk."""
    reset_manager_for_tests()
    _patch_processes(monkeypatch)
    _patch_db_snapshot(monkeypatch)
    _patch_npm_install(monkeypatch)
    _patch_webbrowser(monkeypatch)

    bug_id = _seed_bug(engine_module.SessionLocal, "nowtree-1")
    # Deliberately do NOT create worktrees on disk.

    mgr = TestInstanceManager()
    with pytest.raises(LaunchFailed):
        await mgr.launch(bug_id)

    # The row should be ``crashed`` so the audit trail is preserved.
    session = engine_module.SessionLocal()
    try:
        from sqlalchemy import select

        from ra_triage.db.models import TestInstance

        row = session.execute(
            select(TestInstance).where(TestInstance.bug_id == bug_id)
        ).scalar_one()
        assert row.status == "crashed"
    finally:
        session.close()
