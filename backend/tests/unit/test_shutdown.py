"""Tests for Agent 12's graceful-shutdown coordination."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.orchestrator import pipeline as pipeline_module
from ra_triage.orchestrator import shutdown as shutdown_module
from ra_triage.orchestrator.shutdown import graceful_shutdown, shutdown
from ra_triage.orchestrator.states import BugState


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# --- Flag mechanics --------------------------------------------------------


async def test_shutdown_flag_starts_false() -> None:
    """A freshly-reset flag returns False from is_shutting_down."""
    shutdown_module.reset_for_tests()
    assert shutdown.is_shutting_down() is False


async def test_shutdown_flag_trigger_is_idempotent() -> None:
    """Calling trigger twice doesn't raise; the flag stays True."""
    shutdown_module.reset_for_tests()
    shutdown.trigger()
    shutdown.trigger()
    assert shutdown.is_shutting_down() is True


# --- Pipeline checkpoint integration --------------------------------------


@pytest.fixture()
def seed_worktrees(
    triage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stub worktree creation + listing so the pipeline doesn't shell to git."""
    from ra_triage.workspaces import worktree as wt_module

    async def _fake_create(bug_id: int, repos: list[Any]) -> dict[str, Path]:
        mapping: dict[str, Path] = {}
        for r in repos:
            dest = triage_home / "worktrees" / str(bug_id) / r.name
            dest.mkdir(parents=True, exist_ok=True)
            mapping[r.name] = dest
        return mapping

    def _fake_list(bug_id: int) -> dict[str, Path]:
        parent = triage_home / "worktrees" / str(bug_id)
        if not parent.is_dir():
            return {}
        return {c.name: c for c in sorted(parent.iterdir()) if c.is_dir()}

    monkeypatch.setattr(wt_module, "create_worktrees_for_bug", _fake_create)
    monkeypatch.setattr(wt_module, "list_worktrees", _fake_list)


async def test_shutdown_during_pipeline_keeps_state_unchanged(
    alembic_db: Path,
    triage_home: Path,
    seed_worktrees: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If shutdown trips before _transition fires, the bug stays put.

    We construct a bug that's in OPEN with no agent_runs, trigger
    shutdown, then call run_pipeline. The OPEN → TRIAGED transition
    should be skipped (because _transition observes the flag) and
    the bug should remain in OPEN — exactly what the resume path
    needs to pick up cleanly on the next boot.
    """
    shutdown_module.reset_for_tests()

    session = engine_module.SessionLocal()
    try:
        bug = repo_module.create_bug(
            session,
            notion_page_id="shut-1",
            project_id="ra_avm",
            title="t",
            description="d",
        )
        session.commit()
        bug_id = bug.id
    finally:
        session.close()

    # Trip the flag BEFORE invoking the pipeline.
    shutdown.trigger()

    await pipeline_module.run_pipeline(bug_id)

    session = engine_module.SessionLocal()
    try:
        reloaded = repo_module.get_bug(session, bug_id)
        assert reloaded is not None
        # State stayed in OPEN.
        assert reloaded.state == BugState.OPEN.value
    finally:
        session.close()


async def test_transition_during_shutdown_is_no_op(alembic_db: Path) -> None:
    """The pipeline's ``_transition`` helper bails when shutdown is set."""
    shutdown_module.reset_for_tests()

    session = engine_module.SessionLocal()
    try:
        bug = repo_module.create_bug(
            session,
            notion_page_id="trans-shut",
            project_id="ra_avm",
            title="t",
            description="d",
        )
        session.commit()
        bug_id = bug.id
    finally:
        session.close()

    shutdown.trigger()
    await pipeline_module._transition(bug_id, BugState.TRIAGED)

    session = engine_module.SessionLocal()
    try:
        reloaded = repo_module.get_bug(session, bug_id)
        assert reloaded is not None
        assert reloaded.state == BugState.OPEN.value
    finally:
        session.close()


# --- graceful_shutdown helper ---------------------------------------------


async def test_graceful_shutdown_sets_flag() -> None:
    """The graceful_shutdown helper trips the flag on entry."""
    shutdown_module.reset_for_tests()

    await graceful_shutdown(test_manager=None, agent_tasks=[], deadline_s=0.1)
    assert shutdown.is_shutting_down() is True


async def test_graceful_shutdown_waits_for_clean_tasks() -> None:
    """A task that finishes within the deadline is awaited cleanly."""
    shutdown_module.reset_for_tests()

    completed: list[bool] = []

    async def _short_task() -> None:
        await asyncio.sleep(0.05)
        completed.append(True)

    task = asyncio.create_task(_short_task())
    await graceful_shutdown(
        test_manager=None, agent_tasks=[task], deadline_s=2.0
    )
    assert completed == [True]
    assert task.done()


async def test_graceful_shutdown_cancels_overrunning_tasks() -> None:
    """A task that exceeds the deadline is cancelled."""
    shutdown_module.reset_for_tests()

    async def _long_task() -> None:
        await asyncio.sleep(5.0)

    task = asyncio.create_task(_long_task())
    await graceful_shutdown(
        test_manager=None, agent_tasks=[task], deadline_s=0.05
    )
    # Give the cancel a tick.
    await asyncio.sleep(0.05)
    assert task.cancelled() or task.done()


async def test_graceful_shutdown_broadcasts_to_ws(monkeypatch: pytest.MonkeyPatch) -> None:
    """The helper publishes a backend_shutting_down frame onto the queue topic."""
    shutdown_module.reset_for_tests()
    from ra_triage.utils.events import bus, topic_bugs_queue

    queue = await bus.subscribe(topic_bugs_queue())
    try:
        await graceful_shutdown(
            test_manager=None, agent_tasks=[], deadline_s=0.1
        )
        # Drain at least one frame from the queue — the publish is sync
        # via the loop, so we should see it within a tick.
        try:
            frame = await asyncio.wait_for(queue.get(), timeout=1.0)
        except TimeoutError:  # pragma: no cover - diagnostic
            pytest.fail("Expected backend_shutting_down WS frame")
        assert frame.get("type") == "backend_shutting_down"
    finally:
        await bus.unsubscribe(topic_bugs_queue(), queue)


# --- Resumability after shutdown interrupt ---------------------------------


async def test_resume_picks_up_bug_left_by_shutdown(
    alembic_db: Path, triage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a shutdown leaves a bug in OPEN, resume_inflight schedules it."""
    from ra_triage.orchestrator import resume as resume_module

    shutdown_module.reset_for_tests()

    session = engine_module.SessionLocal()
    try:
        bug = repo_module.create_bug(
            session,
            notion_page_id="resume-after-shut",
            project_id="ra_avm",
            title="t",
            description="d",
        )
        session.commit()
        bug_id = bug.id
    finally:
        session.close()

    # Simulate that the previous boot was interrupted while the bug was
    # in OPEN (no agent_runs).
    reports = await resume_module.resume_inflight()
    matching = [r for r in reports if r.bug_id == bug_id]
    assert matching
    assert matching[0].decision.value == "resume_pipeline"
