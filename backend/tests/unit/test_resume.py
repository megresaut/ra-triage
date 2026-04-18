"""Tests for Agent 12's real ``orchestrator.resume.resume_inflight``.

Covers each :class:`ResumeDecision` variant + idempotency + the lifespan
dispatch hook (using a mock ``run_pipeline``).

The existing ``test_resume_skeleton.py`` tests cover the legacy API
shape (returning bug ids); these are the Agent 12 semantic tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.orchestrator import resume as resume_module
from ra_triage.orchestrator.resume import ResumeDecision, ResumeReport
from ra_triage.orchestrator.states import BugState
from ra_triage.triage_md import parser as triage_parser


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _seed_bug_at_state(
    session: Any, *, notion_page_id: str, state: BugState
) -> int:
    bug = repo_module.create_bug(
        session,
        notion_page_id=notion_page_id,
        project_id="ra_avm",
        title="t",
        description="d",
    )
    walk: list[BugState] = {
        BugState.OPEN: [],
        BugState.TRIAGED: [BugState.TRIAGED],
        BugState.INVESTIGATING: [BugState.TRIAGED, BugState.INVESTIGATING],
        BugState.FIXING: [
            BugState.TRIAGED,
            BugState.INVESTIGATING,
            BugState.FIXING,
        ],
        BugState.TESTING: [
            BugState.TRIAGED,
            BugState.INVESTIGATING,
            BugState.FIXING,
            BugState.TESTING,
        ],
        BugState.READY_FOR_REVIEW: [
            BugState.TRIAGED,
            BugState.INVESTIGATING,
            BugState.FIXING,
            BugState.TESTING,
            BugState.READY_FOR_REVIEW,
        ],
        BugState.MERGING: [
            BugState.TRIAGED,
            BugState.INVESTIGATING,
            BugState.FIXING,
            BugState.TESTING,
            BugState.READY_FOR_REVIEW,
            BugState.MERGING,
        ],
        BugState.BLOCKED: [
            BugState.TRIAGED,
            BugState.INVESTIGATING,
            BugState.BLOCKED,
        ],
    }[state]
    for nxt in walk:
        repo_module.update_bug_state(session, bug, nxt)
    session.commit()
    return int(bug.id)


# --- Decision matrix -------------------------------------------------------


async def test_clean_open_bug_resumes_pipeline(
    alembic_db: Path, triage_home: Path
) -> None:
    """OPEN bug with no agent_runs → RESUME_PIPELINE."""
    session = engine_module.SessionLocal()
    try:
        bug_id = _seed_bug_at_state(
            session, notion_page_id="open-clean", state=BugState.OPEN
        )
    finally:
        session.close()

    reports = await resume_module.resume_inflight()
    matching = [r for r in reports if r.bug_id == bug_id]
    assert matching
    assert matching[0].decision == ResumeDecision.RESUME_PIPELINE


async def test_clean_completed_run_resumes_pipeline(
    alembic_db: Path, triage_home: Path
) -> None:
    """A bug with a clean COMPLETE agent_run → RESUME_PIPELINE."""
    session = engine_module.SessionLocal()
    try:
        bug_id = _seed_bug_at_state(
            session,
            notion_page_id="inv-complete",
            state=BugState.INVESTIGATING,
        )
        repo_module.record_agent_run(
            session,
            bug_id=bug_id,
            agent_role="investigator",
            attempt=1,
            started_at=_now(),
            ended_at=_now(),
            exit_code=0,
            completion_token="INVESTIGATOR_COMPLETE",
            log_path="/tmp/i.log",
            prompt_path="/tmp/i.md",
        )
        session.commit()
    finally:
        session.close()

    reports = await resume_module.resume_inflight()
    matching = [r for r in reports if r.bug_id == bug_id]
    assert matching
    assert matching[0].decision == ResumeDecision.RESUME_PIPELINE


async def test_failed_token_run_still_resumes_pipeline(
    alembic_db: Path, triage_home: Path
) -> None:
    """A *_FAILED completion token is a clean finalization → RESUME_PIPELINE."""
    session = engine_module.SessionLocal()
    try:
        bug_id = _seed_bug_at_state(
            session,
            notion_page_id="failed-tok",
            state=BugState.BLOCKED,
        )
        repo_module.record_agent_run(
            session,
            bug_id=bug_id,
            agent_role="fixer",
            attempt=1,
            started_at=_now(),
            ended_at=_now(),
            exit_code=1,
            completion_token="FIXER_FAILED",
            log_path="/tmp/f.log",
            prompt_path="/tmp/f.md",
        )
        session.commit()
    finally:
        session.close()

    reports = await resume_module.resume_inflight()
    matching = [r for r in reports if r.bug_id == bug_id]
    assert matching
    assert matching[0].decision == ResumeDecision.RESUME_PIPELINE


async def test_crashed_mid_run_resumes_with_recovery_note(
    alembic_db: Path, triage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No completion_token + null ended_at → RESUME_PIPELINE + TRIAGE.md note."""
    # Stub list_worktrees to return a synthetic path so the recovery
    # note has a place to land.
    from ra_triage.workspaces import worktree as wt_module

    worktree_dir = triage_home / "worktrees" / "1" / "avm-backend"
    worktree_dir.mkdir(parents=True, exist_ok=True)

    def _fake_list(bug_id: int) -> dict[str, Path]:
        return {"avm-backend": worktree_dir}

    monkeypatch.setattr(wt_module, "list_worktrees", _fake_list)

    session = engine_module.SessionLocal()
    try:
        bug_id = _seed_bug_at_state(
            session,
            notion_page_id="crashed-mid",
            state=BugState.INVESTIGATING,
        )
        # Insert as if the row was started but never finalized: no
        # completion_token, no ended_at.
        repo_module.record_agent_run(
            session,
            bug_id=bug_id,
            agent_role="investigator",
            attempt=1,
            started_at=_now(),
            ended_at=None,
            exit_code=None,
            completion_token=None,
            log_path="/tmp/c.log",
            prompt_path="/tmp/c.md",
        )
        session.commit()
    finally:
        session.close()

    reports = await resume_module.resume_inflight()
    matching = [r for r in reports if r.bug_id == bug_id]
    assert matching
    assert matching[0].decision == ResumeDecision.RESUME_PIPELINE
    assert "crashed mid-investigator" in matching[0].notes

    triage_md = worktree_dir / "TRIAGE.md"
    assert triage_md.exists(), "TRIAGE.md should have been written"
    sections = triage_parser.parse(triage_md)
    assert "Backend Restart Recovery" in sections
    body = sections["Backend Restart Recovery"]
    assert "investigator" in body
    assert "attempt=1" in body


async def test_inconsistent_rfr_marks_needs_manual_review(
    alembic_db: Path, triage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """READY_FOR_REVIEW with no Tester run → NEEDS_MANUAL_REVIEW + flag set."""
    from ra_triage.workspaces import worktree as wt_module

    worktree_dir = triage_home / "worktrees" / "1" / "avm-backend"
    worktree_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        wt_module, "list_worktrees", lambda bug_id: {"avm-backend": worktree_dir}
    )

    session = engine_module.SessionLocal()
    try:
        bug_id = _seed_bug_at_state(
            session,
            notion_page_id="rfr-no-tester",
            state=BugState.READY_FOR_REVIEW,
        )
        # Investigator + Fixer ran; Tester DID NOT.
        for role in ("investigator", "fixer"):
            repo_module.record_agent_run(
                session,
                bug_id=bug_id,
                agent_role=role,
                attempt=1,
                started_at=_now(),
                ended_at=_now(),
                exit_code=0,
                completion_token=f"{role.upper()}_COMPLETE",
                log_path=f"/tmp/{role}.log",
                prompt_path=f"/tmp/{role}.md",
            )
        session.commit()
    finally:
        session.close()

    reports = await resume_module.resume_inflight()
    matching = [r for r in reports if r.bug_id == bug_id]
    assert matching
    assert matching[0].decision == ResumeDecision.NEEDS_MANUAL_REVIEW
    # State stays as READY_FOR_REVIEW.
    assert matching[0].previous_state == BugState.READY_FOR_REVIEW

    # Flag is set on the bug row.
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.get_bug(session, bug_id)
        assert bug is not None
        assert bool(bug.needs_manual_review) is True
    finally:
        session.close()

    triage_md = worktree_dir / "TRIAGE.md"
    sections = triage_parser.parse(triage_md)
    assert "Needs Manual Review" in sections


async def test_merging_bug_classified_as_post_recovery(
    alembic_db: Path, triage_home: Path
) -> None:
    """A bug found in MERGING → RESUME_POST_RECOVERY."""
    session = engine_module.SessionLocal()
    try:
        bug_id = _seed_bug_at_state(
            session, notion_page_id="merging-1", state=BugState.MERGING
        )
    finally:
        session.close()

    reports = await resume_module.resume_inflight()
    matching = [r for r in reports if r.bug_id == bug_id]
    assert matching
    assert matching[0].decision == ResumeDecision.RESUME_POST_RECOVERY


async def test_idempotent_double_invocation(
    alembic_db: Path, triage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running ``resume_inflight`` twice produces the same decision per bug.

    The TRIAGE.md side effects may differ in detail (recovery notes are
    appended on each call) but the classification per bug must be
    stable.
    """
    from ra_triage.workspaces import worktree as wt_module

    worktree_dir = triage_home / "worktrees" / "1" / "avm-backend"
    worktree_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        wt_module, "list_worktrees", lambda bug_id: {"avm-backend": worktree_dir}
    )

    session = engine_module.SessionLocal()
    try:
        bug_id_open = _seed_bug_at_state(
            session, notion_page_id="idem-open", state=BugState.OPEN
        )
        bug_id_inv = _seed_bug_at_state(
            session,
            notion_page_id="idem-inv",
            state=BugState.INVESTIGATING,
        )
        repo_module.record_agent_run(
            session,
            bug_id=bug_id_inv,
            agent_role="investigator",
            attempt=1,
            started_at=_now(),
            ended_at=_now(),
            exit_code=0,
            completion_token="INVESTIGATOR_COMPLETE",
            log_path="/tmp/i.log",
            prompt_path="/tmp/i.md",
        )
        session.commit()
    finally:
        session.close()

    first = await resume_module.resume_inflight()
    second = await resume_module.resume_inflight()

    by_id_first = {r.bug_id: r.decision for r in first}
    by_id_second = {r.bug_id: r.decision for r in second}
    assert by_id_first == by_id_second
    assert by_id_first[bug_id_open] == ResumeDecision.RESUME_PIPELINE
    assert by_id_first[bug_id_inv] == ResumeDecision.RESUME_PIPELINE


# --- Lifespan dispatch (mocked run_pipeline) -------------------------------


async def test_lifespan_dispatches_run_pipeline_per_resume_pipeline_report(
    alembic_db: Path, triage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The main.py dispatch helper schedules ``run_pipeline`` for each
    RESUME_PIPELINE report.

    We simulate the dispatch path directly (without spinning a real
    FastAPI app) by importing the helper and giving it a mock app
    state.
    """
    from ra_triage.main import _dispatch_resume_report

    scheduled: list[int] = []

    # Replace ``run_pipeline`` so the dispatch helper schedules our
    # stub instead of the real coroutine.
    async def _fake_run_pipeline(bug_id: int) -> None:
        scheduled.append(bug_id)

    monkeypatch.setattr("ra_triage.main.run_pipeline", _fake_run_pipeline)

    class _AppShim:
        class _State:
            agent_tasks: set = set()

        state = _State()

    report = ResumeReport(
        bug_id=42,
        previous_state=BugState.INVESTIGATING,
        decision=ResumeDecision.RESUME_PIPELINE,
        notes="clean state",
    )
    # The dispatch helper only touches ``app.state.agent_tasks`` —
    # mypy wants a FastAPI app, but our shim is structurally compatible.
    _dispatch_resume_report(_AppShim(), report)  # type: ignore[arg-type]

    # Let the scheduled task run.
    import asyncio as _asyncio

    await _asyncio.sleep(0.01)
    assert 42 in scheduled
