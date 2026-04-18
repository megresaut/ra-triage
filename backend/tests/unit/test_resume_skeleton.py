"""Compat tests for ``ra_triage.orchestrator.resume.resume_inflight``.

These tests originated as the Agent 5 skeleton smoke tests (resume_inflight
returned ``list[int]``). Agent 12 reshapes the API to return
``list[ResumeReport]``; the comparison helpers on ResumeReport accept ints
in equality checks so the legacy assertions still hold.

Detailed Agent 12 behavior is exercised by ``test_resume.py`` — these
tests stay as a smoke layer that proves the basic API shape is preserved
across the upgrade.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.db.models import AgentRun
from ra_triage.orchestrator import resume as resume_module
from ra_triage.orchestrator.states import BugState


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _seed_bug(
    session: Any, *, notion_page_id: str, state: BugState
) -> int:
    bug = repo_module.create_bug(
        session,
        notion_page_id=notion_page_id,
        project_id="ra_avm",
        title="x",
        description="y",
    )
    if state == BugState.OPEN:
        session.commit()
        return int(bug.id)
    current = BugState.OPEN
    for step in [BugState.TRIAGED, BugState.INVESTIGATING]:
        repo_module.update_bug_state(session, bug, step)
        current = step
        if current == state:
            break
    session.commit()
    return int(bug.id)


async def test_resume_inflight_returns_non_terminal_bug_ids(
    alembic_db: Path, triage_home: Path
) -> None:
    """Bugs in OPEN/TRIAGED/INVESTIGATING are listed; terminal bugs are not."""
    session = engine_module.SessionLocal()
    try:
        non_term_ids: set[int] = set()
        non_term_ids.add(_seed_bug(session, notion_page_id="n-1", state=BugState.OPEN))
        non_term_ids.add(
            _seed_bug(session, notion_page_id="n-2", state=BugState.TRIAGED)
        )
        non_term_ids.add(
            _seed_bug(session, notion_page_id="n-3", state=BugState.INVESTIGATING)
        )

        # One terminal bug (should NOT appear).
        term_bug = repo_module.create_bug(
            session,
            notion_page_id="term-1",
            project_id="ra_avm",
            title="done",
            description="x",
        )
        for st in [BugState.TRIAGED, BugState.INVESTIGATING, BugState.FIXING,
                   BugState.TESTING, BugState.READY_FOR_REVIEW, BugState.MERGING,
                   BugState.FIXED]:
            repo_module.update_bug_state(session, term_bug, st)
        session.commit()
    finally:
        session.close()

    reports = await resume_module.resume_inflight()

    observed_ids = {r.bug_id for r in reports}
    assert observed_ids == non_term_ids


async def test_resume_inflight_handles_empty_db(
    alembic_db: Path, triage_home: Path
) -> None:
    """No bugs in DB → empty list, no error."""
    reports = await resume_module.resume_inflight()
    assert reports == []


async def test_resume_inflight_inspects_latest_agent_run(
    alembic_db: Path, triage_home: Path
) -> None:
    """When a bug has prior agent_runs, the resume report still surfaces it."""
    session = engine_module.SessionLocal()
    try:
        bug_id = _seed_bug(
            session, notion_page_id="with-run", state=BugState.INVESTIGATING
        )
        run = AgentRun(
            bug_id=bug_id,
            agent_role="investigator",
            attempt=1,
            started_at=_now(),
            ended_at=_now(),
            log_path="/tmp/x.log",
            prompt_path="/tmp/x.md",
            completion_token="INVESTIGATOR_COMPLETE",
            exit_code=0,
        )
        session.add(run)
        session.commit()
        assert repo_module.get_bug(session, bug_id) is not None
        runs = repo_module.list_agent_runs(session, bug_id)
        assert len(runs) == 1
    finally:
        session.close()

    reports = await resume_module.resume_inflight()

    matching = [r for r in reports if r.bug_id == bug_id]
    assert matching, f"reports={reports}"
    # A clean COMPLETE run on an INVESTIGATING bug → RESUME_PIPELINE.
    assert matching[0].decision == resume_module.ResumeDecision.RESUME_PIPELINE
