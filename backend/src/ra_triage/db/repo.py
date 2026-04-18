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

from ra_triage.db.models import (
    AgentRun,
    Bug,
    Escalation,
    PullRequest,
    RebaseAttempt,
    Rejection,
    TestInstance,
)
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
    through callers). As of Agent 9 this fans out to three best-effort
    hooks; each one catches its own exceptions so a downstream outage
    never rolls back the DB transition:

      1. Notion status write-back (Agent 3).
      2. Slack escalation ping (Agent 9) — only on BLOCKED when a
         populated Escalation Brief is present in the bug's TRIAGE.md.
      3. Slack ready-for-review ping (Agent 9) — only on entering
         READY_FOR_REVIEW.

    All side effects are best-effort: this function never raises. The
    individual helpers log their own failures.
    """
    _run_notion_writeback(bug, new_state)
    _run_slack_escalation_hook(bug, new_state)
    _run_slack_rfr_hook(bug, new_state)


def _run_notion_writeback(bug: Bug, new_state: BugState) -> None:
    """Fire the Notion status write-back; swallow + log on any failure."""
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


def _run_slack_escalation_hook(bug: Bug, new_state: BugState) -> None:
    """Ping Slack on BLOCKED when an unresolved ``escalations`` row exists.

    The pipeline persists the escalations row BEFORE calling
    :func:`update_bug_state` (per Agent 9 contract), so by the time
    this hook runs, the row is already in the DB if the transition was
    triggered by an Investigator-produced Escalation Brief.

    Transitions to BLOCKED that have no corresponding escalation row
    (e.g. Take Over Manually, Rejection Limit Reached, Merge Failure,
    worktree setup failure) skip the Slack ping — those flows already
    have their own surfaces in the frontend + Notion write-back.

    Best-effort; failures are logged + swallowed so the transition
    never rolls back.
    """
    if new_state != BugState.BLOCKED:
        return
    import logging

    log = logging.getLogger(__name__)
    try:
        from ra_triage.config.registry import get_project
        from ra_triage.integrations import slack as slack_mod
    except Exception:  # pragma: no cover - defensive
        return

    # Look for an unresolved escalation row on the current session's
    # engine. We open a short-lived session here so we don't have to
    # thread the caller's session through the hook signature.
    try:
        from ra_triage.db import engine as engine_module

        session = engine_module.SessionLocal()
        try:
            latest = get_latest_unresolved_escalation(session, int(bug.id))
        finally:
            session.close()
    except Exception:
        log.exception(
            "Slack escalation hook: failed to load escalation row for bug_id=%s",
            bug.id,
        )
        return

    if latest is None:
        log.debug(
            "Slack escalation hook: no unresolved escalation for bug_id=%s; "
            "skipping (BLOCKED from a non-escalation path)",
            bug.id,
        )
        return

    try:
        project_cfg = get_project(bug.project_id)
    except KeyError:
        return
    except Exception:
        log.exception(
            "Slack escalation hook: could not resolve project for bug_id=%s",
            bug.id,
        )
        return

    try:
        slack_mod.notify_escalation(bug, project_cfg)
    except Exception:  # pragma: no cover - defensive
        log.exception(
            "Slack escalation hook: notify_escalation raised for bug_id=%s "
            "(swallowed)",
            bug.id,
        )


def _run_slack_rfr_hook(bug: Bug, new_state: BugState) -> None:
    """Ping Slack on entering READY_FOR_REVIEW. Best-effort."""
    if new_state != BugState.READY_FOR_REVIEW:
        return
    try:
        from ra_triage.config.registry import get_project
        from ra_triage.integrations import slack as slack_mod
    except Exception:  # pragma: no cover
        return
    try:
        project_cfg = get_project(bug.project_id)
    except KeyError:
        return
    except Exception:  # pragma: no cover
        import logging

        logging.getLogger(__name__).exception(
            "Slack RFR hook: could not resolve project for bug_id=%s", bug.id
        )
        return
    try:
        slack_mod.notify_ready_for_review(bug, project_cfg)
    except Exception:  # pragma: no cover - defensive
        import logging

        logging.getLogger(__name__).exception(
            "Slack RFR hook failed for bug_id=%s (swallowed)", bug.id
        )




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


# --- Escalation --------------------------------------------------------------


def record_escalation(
    session: Session,
    *,
    bug_id: int,
    triggered_rule: str | None,
    brief_markdown: str,
    options: str,
) -> Escalation:
    """Persist an ``escalations`` row for a bug.

    Called by the orchestrator when it observes an Escalation Brief in
    TRIAGE.md and transitions the bug to BLOCKED. The brief + structured
    options are stored verbatim (options as a JSON-string of
    ``ParsedBriefOption`` dicts) so the API can replay both for the
    frontend's action buttons.
    """
    row = Escalation(
        bug_id=bug_id,
        triggered_rule=triggered_rule,
        brief_markdown=brief_markdown,
        options=options,
        resolution=None,
        resolved_option=None,
    )
    session.add(row)
    session.flush()
    return row


def list_escalations_for_bug(session: Session, bug_id: int) -> list[Escalation]:
    """Return every ``escalations`` row for ``bug_id`` ordered oldest-first."""
    stmt = (
        select(Escalation)
        .where(Escalation.bug_id == bug_id)
        .order_by(Escalation.created_at.asc(), Escalation.id.asc())
    )
    return list(session.execute(stmt).scalars())


def get_latest_unresolved_escalation(
    session: Session, bug_id: int
) -> Escalation | None:
    """Return the most recent ``escalations`` row for ``bug_id`` whose
    ``resolution`` is still NULL, or ``None`` if no such row exists.

    Used by the approve / pick-alt / take-over flows to locate the row
    they should mark as resolved when the user picks an option.
    """
    stmt = (
        select(Escalation)
        .where(Escalation.bug_id == bug_id)
        .where(Escalation.resolution.is_(None))
        .order_by(Escalation.created_at.desc(), Escalation.id.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def update_escalation_resolution(
    session: Session,
    *,
    escalation_id: int,
    resolution: str,
    resolved_option: str | None,
) -> Escalation:
    """Mark an escalation resolved.

    ``resolution`` is one of ``"approved"``, ``"picked_alt"``,
    ``"taken_over"`` (and could grow in the future). ``resolved_option``
    is the ``ParsedBriefOption.id`` the user picked (or ``None`` for
    take-over, which has no specific option beyond the implied
    ``"take_over"`` — but callers typically pass ``"take_over"``
    explicitly for clarity).

    Raises :class:`ValueError` if ``escalation_id`` isn't found.
    """
    row = session.get(Escalation, escalation_id)
    if row is None:
        raise ValueError(f"escalations row {escalation_id} not found")
    row.resolution = resolution
    row.resolved_option = resolved_option
    row.resolved_at = _utcnow()
    session.flush()
    return row


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


def parse_conflict_files(raw: str | None) -> list[str] | None:
    """Parse the ``bugs.conflict_files`` JSON column into a list or None.

    Returns ``None`` (not ``[]``) when the column is NULL so the frontend
    can distinguish "no conflict" (None) from "conflict with empty file
    list" (a weird state we never intentionally produce, but we handle
    it as an empty list for safety).
    """
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(value, list):
        return [str(v) for v in value]
    return None


# --- Agent 11: rebase sweeper support ---------------------------------------


#: States that represent in-flight bug work. A bug in any of these states may
#: hold a worktree that needs rebasing when a sibling bug's PR merges. MERGING
#: is deliberately EXCLUDED because that bug is itself being merged (rebasing
#: it mid-merge would be racy); terminal states are excluded for obvious
#: reasons.
_INFLIGHT_STATES: tuple[str, ...] = (
    BugState.TRIAGED.value,
    BugState.INVESTIGATING.value,
    BugState.DESIGNING.value,
    BugState.FIXING.value,
    BugState.FIXING_PARALLEL.value,
    BugState.TESTING.value,
    BugState.INTEGRATION_TESTING.value,
    BugState.READY_FOR_REVIEW.value,
    BugState.BLOCKED.value,
)


def list_inflight_for_repo(
    session: Session,
    repo_name: str,
    *,
    exclude_bug_id: int | None = None,
) -> list[Bug]:
    """Return non-terminal, non-MERGING bugs whose ``affected_repos`` includes ``repo_name``.

    Used by the rebase sweeper (:func:`workspaces.rebase.rebase_in_flight`)
    when a trigger bug finishes merging — it walks every other in-flight
    bug touching the same repo and attempts an automatic ``git rebase
    origin/<main>``.

    Filtering rules:
      - State must be in :data:`_INFLIGHT_STATES` (excludes terminal states
        and MERGING — a bug being merged RIGHT NOW must not race rebase).
      - ``affected_repos`` JSON must contain ``repo_name``.
      - ``exclude_bug_id`` (the trigger bug) is filtered out.

    Returned list is ordered by ``id ASC`` so callers see deterministic
    ordering across the sweep. The function is sync + session-bound; the
    orchestrator dispatches it on :func:`asyncio.to_thread` from the
    async sweep task.
    """
    stmt = (
        select(Bug)
        .where(Bug.state.in_(_INFLIGHT_STATES))
        .order_by(Bug.id.asc())
    )
    if exclude_bug_id is not None:
        stmt = stmt.where(Bug.id != exclude_bug_id)
    bugs = list(session.execute(stmt).scalars())
    # Filter on the JSON ``affected_repos`` column in Python — SQLite doesn't
    # give us portable JSON containment queries and the in-flight set is
    # small (hard capped by PIPELINE_SEMAPHORE=3).
    out: list[Bug] = []
    for b in bugs:
        names = parse_affected_repos(b.affected_repos)
        if repo_name in names:
            out.append(b)
    return out


def record_rebase_attempt(
    session: Session,
    *,
    bug_id: int,
    repo_name: str,
    trigger_bug_id: int,
    result: str,
    conflict_files: list[str] | None = None,
) -> RebaseAttempt:
    """Persist an audit row for one rebase attempt.

    ``result`` is one of ``"clean"``, ``"conflict"``, ``"error"``. ``conflict_files``
    is a list of paths (encoded as JSON in the text column) on ``"conflict"``;
    ``None`` on the other results.

    Raises :class:`ValueError` for an unknown result string so typos don't
    silently produce bogus audit rows.
    """
    if result not in ("clean", "conflict", "error"):
        raise ValueError(
            f"record_rebase_attempt: result must be 'clean'|'conflict'|'error', got {result!r}"
        )
    files_json: str | None = None
    if conflict_files is not None:
        files_json = json.dumps(list(conflict_files))
    row = RebaseAttempt(
        bug_id=bug_id,
        repo_name=repo_name,
        trigger_bug_id=trigger_bug_id,
        result=result,
        conflict_files=files_json,
    )
    session.add(row)
    session.flush()
    return row


def list_rebase_attempts(session: Session, bug_id: int) -> list[RebaseAttempt]:
    """Return every ``rebase_attempts`` row for ``bug_id``, oldest-first."""
    stmt = (
        select(RebaseAttempt)
        .where(RebaseAttempt.bug_id == bug_id)
        .order_by(RebaseAttempt.created_at.asc(), RebaseAttempt.id.asc())
    )
    return list(session.execute(stmt).scalars())


def mark_blocked_with_conflict(
    session: Session,
    bug_id: int,
    repo_name: str,
    conflict_files: list[str],
) -> Bug:
    """Flip a bug to BLOCKED + stamp ``bugs.conflict_files`` with the conflict list.

    Also ensures the LATEST ``rebase_attempts`` row for this bug/repo has
    its ``conflict_files`` column populated — the sweeper normally calls
    :func:`record_rebase_attempt` first to insert the audit row, and this
    helper backfills the file list onto that row in the same session.

    Transition semantics:
      - From an in-flight state (FIXING, FIXING_PARALLEL, TESTING,
        INTEGRATION_TESTING, READY_FOR_REVIEW, INVESTIGATING, DESIGNING,
        TRIAGED) → BLOCKED via :func:`update_bug_state`.
      - If the bug is ALREADY BLOCKED, we just stamp the column and
        audit-row fields without trying to transition (that would raise
        :class:`IllegalBugStateTransition`).
      - If the bug is terminal or MERGING, we leave state alone and stamp
        the column + audit row anyway — the caller already filtered via
        :func:`list_inflight_for_repo`, so this is a defensive branch.

    Returns the (possibly-updated) bug so callers can publish events or
    re-render without a second fetch.
    """
    bug = session.get(Bug, bug_id)
    if bug is None:
        raise ValueError(f"mark_blocked_with_conflict: unknown bug_id={bug_id}")

    # Stamp the denorm column first so observers watching the state_change
    # event see the conflict_files already populated.
    bug.conflict_files = json.dumps(list(conflict_files))
    bug.updated_at = _utcnow()

    current = BugState(bug.state)
    if current not in TERMINAL_STATES and current not in (
        BugState.BLOCKED,
        BugState.MERGING,
    ):
        # Go through the state machine so the hook fires.
        update_bug_state(session, bug, BugState.BLOCKED)
    else:
        session.flush()

    # Backfill the conflict_files list onto the most recent ``rebase_attempts``
    # row for (bug_id, repo_name) so the audit trail is fully populated.
    attempts = list_rebase_attempts(session, bug_id)
    matching = [a for a in attempts if a.repo_name == repo_name]
    if matching:
        latest = matching[-1]
        latest.conflict_files = json.dumps(list(conflict_files))
        session.flush()

    return bug


def clear_conflict_files(session: Session, bug_id: int) -> None:
    """Null out ``bugs.conflict_files`` — called when a conflict is resolved.

    The retry-fixer-with-conflict flow (:func:`orchestrator.pipeline.retry_fixer_with_conflict`)
    fires this when it transitions BLOCKED → FIXING so the frontend no
    longer renders the conflict panel once the Fixer is re-engaged.
    """
    bug = session.get(Bug, bug_id)
    if bug is None:
        return
    bug.conflict_files = None
    bug.updated_at = _utcnow()
    session.flush()
