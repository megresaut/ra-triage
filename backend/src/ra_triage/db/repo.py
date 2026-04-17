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
    - Calls the Notion write-back post-transition hook (Agent 3). Failures
      in that side effect MUST NOT roll back the transition; the hook
      swallows all errors internally.
    """
    previous = BugState(bug.state)
    transition(previous, new_state)

    bug.state = new_state.value
    bug.updated_at = _utcnow()
    if new_state in TERMINAL_STATES:
        bug.terminated_at = _utcnow()
    session.flush()

    _publish_state_change(bug, previous.value, new_state.value)
    _post_transition_hook(bug, new_state)
    return bug


def _post_transition_hook(bug: Bug, new_state: BugState) -> None:
    """Single post-transition side-effect entry point.

    Kept as a thin dispatch so new side effects land here (not sprinkled
    through callers). Currently: Notion status write-back only.

    All side effects are best-effort: this function never raises. The
    individual helpers log their own failures.
    """
    # Deferred import avoids a module-level cycle:
    #   integrations.notion imports orchestrator.states, which is imported
    #   by this file. Import at call time instead.
    try:
        from ra_triage.integrations.notion import status_writeback_for_state
    except Exception:  # pragma: no cover - defensive only
        return
    try:
        status_writeback_for_state(bug, new_state)
    except Exception:  # pragma: no cover - the hook already swallows
        pass


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


def list_rejections(session: Session, bug_id: int) -> list[Rejection]:
    """Return every ``rejections`` row for ``bug_id`` ordered oldest-first.

    The Agent 8 reject endpoint uses this to render the rolling list of
    reasons in the ``## Rejection Limit Reached`` section when a bug hits
    3 rejections and gets pinned into BLOCKED.
    """
    stmt = (
        select(Rejection)
        .where(Rejection.bug_id == bug_id)
        .order_by(Rejection.created_at.asc(), Rejection.id.asc())
    )
    return list(session.execute(stmt).scalars())


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


def list_pull_requests(session: Session, bug_id: int) -> list[PullRequest]:
    """Return every ``pull_requests`` row for ``bug_id``, oldest first."""
    stmt = (
        select(PullRequest)
        .where(PullRequest.bug_id == bug_id)
        .order_by(PullRequest.created_at.asc(), PullRequest.id.asc())
    )
    return list(session.execute(stmt).scalars())


def find_open_pr(
    session: Session, *, bug_id: int, repo_name: str
) -> PullRequest | None:
    """Return the most recent open PR for ``(bug_id, repo_name)`` or None.

    Used by the merge driver (Agent 8) to reuse an already-created PR when
    the user clicks Confirm a second time after a transient merge failure.
    """
    stmt = (
        select(PullRequest)
        .where(PullRequest.bug_id == bug_id)
        .where(PullRequest.repo_name == repo_name)
        .where(PullRequest.status == "open")
        .order_by(PullRequest.created_at.desc(), PullRequest.id.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def update_pr_status(
    session: Session,
    *,
    pr_id: int,
    status: str,
    merged_at: datetime | None = None,
) -> PullRequest:
    """Update a ``pull_requests`` row's ``status`` (and optional ``merged_at``).

    Raises ``ValueError`` if ``pr_id`` isn't found — the caller is holding
    the same ORM session that produced the row so a miss is a bug, not a
    race condition.
    """
    pr = session.get(PullRequest, pr_id)
    if pr is None:
        raise ValueError(f"pull_requests row {pr_id} not found")
    pr.status = status
    if status == "merged" and pr.merged_at is None:
        pr.merged_at = merged_at or _utcnow()
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


def create_test_instance(
    session: Session,
    *,
    bug_id: int,
    pids: dict[str, int],
    ports: dict[str, int],
    status: str = "starting",
    db_snapshot_hash: str | None = None,
) -> TestInstance:
    """Persist a new ``test_instances`` row.

    ``pids`` and ``ports`` are ``{repo_name: value}`` maps serialized as
    JSON (the ORM column is a plain TEXT, matching the Alembic schema).
    Typical call sequence from :class:`TestInstanceManager`:

      1. Create with ``status='starting'`` and empty maps before spawn.
      2. Once spawn + readiness check succeed, flip to ``running`` via
         :func:`update_test_instance_status` with the real pid/port maps.

    Agent 7's manager calls this directly; the Agent 2 surface only ever
    reads instances via :func:`get_current_test_instance`.
    """
    instance = TestInstance(
        bug_id=bug_id,
        pids=json.dumps(pids, sort_keys=True),
        ports=json.dumps(ports, sort_keys=True),
        db_snapshot_hash=db_snapshot_hash,
        status=status,
        launched_at=_utcnow(),
    )
    session.add(instance)
    session.flush()
    return instance


def update_test_instance_status(
    session: Session,
    instance_id: int,
    *,
    status: str,
    pids: dict[str, int] | None = None,
    ports: dict[str, int] | None = None,
    db_snapshot_hash: str | None = None,
    stopped_at: datetime | None = None,
) -> TestInstance:
    """Update a ``test_instances`` row and return it.

    Absent keyword arguments leave the corresponding column untouched — we
    overwrite pids / ports / hash / stopped_at only when the caller passes a
    value. ``stopped_at`` is automatically stamped to ``now`` when ``status``
    flips to a terminal test-instance state (``stopped``, ``crashed``) and
    the caller didn't supply an explicit value.

    Raises ``ValueError`` if the instance_id is unknown — callers are
    expected to hold the same ORM session that produced the row, so a miss
    is a programming error, not a race.
    """
    instance = session.get(TestInstance, instance_id)
    if instance is None:
        raise ValueError(f"test_instances row {instance_id} not found")
    instance.status = status
    if pids is not None:
        instance.pids = json.dumps(pids, sort_keys=True)
    if ports is not None:
        instance.ports = json.dumps(ports, sort_keys=True)
    if db_snapshot_hash is not None:
        instance.db_snapshot_hash = db_snapshot_hash
    if stopped_at is not None:
        instance.stopped_at = stopped_at
    elif status in ("stopped", "crashed") and instance.stopped_at is None:
        instance.stopped_at = _utcnow()
    session.flush()
    return instance


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
