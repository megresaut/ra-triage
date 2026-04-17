"""Tests for ``ra_triage.orchestrator.resume.resume_inflight`` (skeleton)."""

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
    # Walk into the target state via legal transitions.
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

    observed = await resume_module.resume_inflight()

    assert set(observed) == non_term_ids


async def test_resume_inflight_handles_empty_db(
    alembic_db: Path, triage_home: Path
) -> None:
    """No bugs in DB → empty list, no error."""
    observed = await resume_module.resume_inflight()
    assert observed == []


async def test_resume_inflight_inspects_latest_agent_run(
    alembic_db: Path, triage_home: Path
) -> None:
    """When a bug has prior agent_runs, the log line includes the role + attempt."""
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
            log_path="/tmp/x.log",
            prompt_path="/tmp/x.md",
            completion_token="INVESTIGATOR_COMPLETE",
            exit_code=0,
        )
        session.add(run)
        session.commit()
        # Sanity checks before the assertion block below.
        assert repo_module.get_bug(session, bug_id) is not None
        runs = repo_module.list_agent_runs(session, bug_id)
        assert len(runs) == 1
    finally:
        session.close()

    # Capture log calls by attaching a handler to the resume module's
    # logger. We use a custom handler rather than caplog because
    # pytest-asyncio sometimes swaps capture fixtures around awaits.
    import logging

    records: list[logging.LogRecord] = []

    class _Capturer(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capturer(logging.DEBUG)
    resume_module.logger.addHandler(handler)
    prior_level = resume_module.logger.level
    # pytest's log-capture plugin sets ``logger.disabled=True`` during
    # the setup phase; we need to flip it back to emit our records.
    prior_disabled = resume_module.logger.disabled
    resume_module.logger.disabled = False
    resume_module.logger.setLevel(logging.DEBUG)
    try:
        observed = await resume_module.resume_inflight()
    finally:
        resume_module.logger.removeHandler(handler)
        resume_module.logger.setLevel(prior_level)
        resume_module.logger.disabled = prior_disabled

    assert bug_id in observed, f"observed={observed}"
    joined = "\n".join(r.getMessage() for r in records)
    assert records, (
        f"Expected at least one log record; got none. observed={observed}"
    )
    assert "investigator" in joined
    assert "INVESTIGATOR_COMPLETE" in joined
