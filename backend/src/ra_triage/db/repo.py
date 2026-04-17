"""Repository-style synchronous CRUD helpers.

Synchronous because FastAPI will dispatch sync endpoints on its threadpool
and SQLAlchemy 2.x's sync API is simpler than opting into async here. This
module stays pure: no HTTP, no FastAPI, no logging beyond exceptions — just DB
work and event publication.

State changes publish a `state_change` event on both the per-bug topic and
the queue-wide topic. WS handlers subscribe to those topics and forward JSON
frames to connected clients. Agent 3 will later add a post-transition side
effect here for Notion status write-back.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ra_triage.db.models import AgentRun, Bug, PullRequest, Rejection, TestInstance
from ra_triage.orchestrator.states import (
    TERMINAL_STATES,
    BugState,
    transition,
)
from ra_triage.utils.events import bus, topic_bug_state, topic_bugs_queue


def _utcnow() -> datetime:
    """Timezone-naive UTC "now" — matches how Alembic's CURRENT_TIMESTAMP
    default writes values (no tzinfo on SQLite DATETIME columns).

    Wrapped here so we can move to `datetime.now(timezone.utc).replace(tzinfo=None)`
    or a tz-aware schema in one place later."""
    return datetime.now(UTC).replace(tzinfo=None)


def _serialize_bug_for_event(bug: Bug) -> dict[str, object]:
    """Minimal JSON-ready view of a bug for event payloads.

    Keep this small and stable — every WS subscriber sees it. Full bug
    details are available via REST.
    """
    return {
        "id": bug.id,
        "project_id": bug.project_id,
        "title": bug.title,
        "state": bug.state,
        "rejection_count": bug.rejection_count,
        "terminated_at": bug.terminated_at.isoformat() if bug.terminated_at else None,
    }


def _publish_state_change(bug: Bug, previous_state: str, new_state: str) -> None:
    """Fire state-change events on both per-bug and queue-wide topics."""
    payload: dict[str, object] = {
        "type": "state_change",
        "bug_id": bug.id,
        "previous_state": previous_state,
        "new_state": new_state,
        "bug": _serialize_bug_for_event(bug),
    }
    bus.publish_sync(topic_bug_state(bug.id), payload)
    bus.publish_sync(topic_bugs_queue(), payload)


# --- Bug CRUD ----------------------------------------------------------------


def create_bug(
    session: Session,
    *,
    notion_page_id: str,
    project_id: str,
    title: str,
    description: str,
    repro_steps: str | None = None,
    severity: str | None = None,
    reporter: str | None = None,
) -> Bug:
    """Create a bug in `OPEN` state, or return the existing row if we've seen
    this `notion_page_id` before. Idempotent by design (the poller may retry)."""
    existing = session.execute(
        select(Bug).where(Bug.notion_page_id == notion_page_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    now = _utcnow()
    bug = Bug(
        notion_page_id=notion_page_id,
        project_id=project_id,
        title=title,
        description=description,
        repro_steps=repro_steps,
        severity=severity,
        reporter=reporter,
        state=BugState.OPEN.value,
        scope=None,
        affected_repos=None,
        deny_list_hit=False,
        rejection_count=0,
        created_at=now,
        updated_at=now,
        terminated_at=None,
    )
    session.add(bug)
    session.flush()  # assign PK so the ORM has bug.id before commit
    return bug


def get_bug(session: Session, bug_id: int) -> Bug | None:
    """Look up a bug by primary key."""
    return session.get(Bug, bug_id)


def list_bugs(
    session: Session,
    *,
    project_id: str | None = None,
    state: BugState | None = None,
    include_terminal: bool = False,
) -> list[Bug]:
    """Return bugs matching the filters, newest first.

    By default terminal-state bugs (FIXED, WONT_FIX, CANCELLED) are excluded —
    that matches the queue view's expectation. Passing `state=` implicitly
    overrides this filter for that specific state (so asking for state=FIXED
    returns FIXED bugs even though they are terminal).
    """
    stmt = select(Bug).order_by(Bug.created_at.desc())
    if project_id is not None:
        stmt = stmt.where(Bug.project_id == project_id)
    if state is not None:
        stmt = stmt.where(Bug.state == state.value)
    elif not include_terminal:
        terminal_values = [s.value for s in TERMINAL_STATES]
        stmt = stmt.where(Bug.state.notin_(terminal_values))
    return list(session.execute(stmt).scalars())


def update_bug_state(session: Session, bug: Bug, new_state: BugState) -> Bug:
    """Transition a bug to `new_state`, validating via the state machine.

    - Raises `IllegalBugStateTransition` for illegal moves (wrapped from
      `orchestrator.states.transition`).
    - Updates `updated_at`; stamps `terminated_at` when moving into a terminal
      state.
    - Publishes a `state_change` event on the bug's topic and on the queue
      topic.
    """
    previous = BugState(bug.state)
    transition(previous, new_state)

    bug.state = new_state.value
    bug.updated_at = _utcnow()
    if new_state in TERMINAL_STATES:
        bug.terminated_at = _utcnow()
    session.flush()

    _publish_state_change(bug, previous.value, new_state.value)
    return bug


# --- AgentRun ----------------------------------------------------------------


def record_agent_run(
    session: Session,
    *,
    bug_id: int,
    agent_role: str,
    attempt: int,
    started_at: datetime,
    ended_at: datetime | None = None,
    exit_code: int | None = None,
    completion_token: str | None = None,
    log_path: str,
    prompt_path: str,
    worktree_repo: str | None = None,
    triage_md_snapshot: str | None = None,
) -> AgentRun:
    """Persist an agent run row (one per subprocess invocation)."""
    run = AgentRun(
        bug_id=bug_id,
        agent_role=agent_role,
        attempt=attempt,
        started_at=started_at,
        ended_at=ended_at,
        exit_code=exit_code,
        completion_token=completion_token,
        log_path=log_path,
        prompt_path=prompt_path,
        worktree_repo=worktree_repo,
        triage_md_snapshot=triage_md_snapshot,
    )
    session.add(run)
    session.flush()
    return run


def list_agent_runs(session: Session, bug_id: int) -> list[AgentRun]:
    """All agent runs for a bug, ordered by start time ascending."""
    stmt = (
        select(AgentRun)
        .where(AgentRun.bug_id == bug_id)
        .order_by(AgentRun.started_at.asc(), AgentRun.id.asc())
    )
    return list(session.execute(stmt).scalars())


# --- Rejection ---------------------------------------------------------------


def record_rejection(session: Session, *, bug_id: int, reason: str) -> Rejection:
    """Persist a rejection and bump `bugs.rejection_count` atomically."""
    bug = session.get(Bug, bug_id)
    if bug is None:
        raise ValueError(f"Cannot record rejection for unknown bug_id={bug_id}")
    rejection = Rejection(bug_id=bug_id, reason=reason)
    session.add(rejection)
    bug.rejection_count = (bug.rejection_count or 0) + 1
    bug.updated_at = _utcnow()
    session.flush()
    return rejection


# --- PullRequest -------------------------------------------------------------


def record_pr(
    session: Session,
    *,
    bug_id: int,
    repo_name: str,
    branch: str,
    pr_url: str | None = None,
    pr_number: int | None = None,
    status: str = "open",
) -> PullRequest:
    """Persist a PR row for the given bug + repo."""
    pr = PullRequest(
        bug_id=bug_id,
        repo_name=repo_name,
        branch=branch,
        pr_url=pr_url,
        pr_number=pr_number,
        status=status,
    )
    session.add(pr)
    session.flush()
    return pr


# --- TestInstance ------------------------------------------------------------


def get_current_test_instance(session: Session) -> TestInstance | None:
    """Most recent test_instance row whose status is 'starting' or 'running'.

    Agent 7 owns the real TestInstanceManager; Agent 2 uses this to compute
    each bug's `launched_for_testing` flag for the queue view.
    """
    stmt = (
        select(TestInstance)
        .where(TestInstance.status.in_(("starting", "running")))
        .order_by(TestInstance.launched_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


# --- Utility -----------------------------------------------------------------


def parse_affected_repos(raw: str | None) -> list[str]:
    """Parse the `bugs.affected_repos` JSON column into a list.

    Stored as JSON (per tech plan §3 schema comment); returns [] for null/empty
    and falls back to [] on malformed content rather than raising.
    """
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return []
