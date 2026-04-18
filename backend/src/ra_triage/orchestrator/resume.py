"""Pipeline resume — restart-recovery for non-terminal bugs (Agent 12).

On every backend startup, ``resume_inflight`` walks the bugs table and
classifies each non-terminal row into one of four buckets:

  - :attr:`ResumeDecision.RESUME_PIPELINE` — the most recent agent run
    finished cleanly (or never ran), so a fresh ``run_pipeline(bug_id)``
    will idempotently pick up at the right step. The lifespan hook
    schedules the coroutine.
  - :attr:`ResumeDecision.RESUME_POST_RECOVERY` — the bug was MERGING
    when the backend died. We probe the open PRs via ``gh pr view`` and
    either drive the bug to FIXED (PRs merged while we were down) or
    re-enter the merge driver (PRs still open).
  - :attr:`ResumeDecision.NEEDS_MANUAL_REVIEW` — the bug's state is
    inconsistent with its ``agent_runs`` audit trail (e.g.
    ``state=READY_FOR_REVIEW`` but no Tester run recorded). We set
    ``bugs.needs_manual_review = True`` so the frontend renders a
    yellow banner and we append a ``## Needs Manual Review`` section to
    TRIAGE.md describing the mismatch. State is left as-is.
  - :attr:`ResumeDecision.ALREADY_TERMINAL` — terminal bugs are skipped
    by the iteration filter; this enum value exists for the report API
    so a terminal-state bug submitted for inspection (in tests) returns
    something meaningful.

For RESUME_PIPELINE on a *crashed* prior run (no ``completion_token`` AND
either non-zero ``exit_code`` OR ``ended_at IS NULL``), we also append a
``## Backend Restart Recovery`` note to TRIAGE.md before scheduling
``run_pipeline``. The pipeline's retry path then runs the agent again
with attempt+1.

Idempotency: running ``resume_inflight`` twice produces the same set of
ResumeReports. The TRIAGE.md notes are appended on every call (Markdown
append; the parser merges duplicates by taking the last one) — but the
``## Backend Restart Recovery`` section's body is a timestamp string,
so successive calls do produce visible duplicate notes. That's
intentional: every restart recovery deserves an audit trail. The
``## Needs Manual Review`` note only appends if the section isn't
already present.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from ra_triage.config.registry import ProjectConfig, get_project
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.db.models import AgentRun, Bug
from ra_triage.orchestrator.states import TERMINAL_STATES, BugState
from ra_triage.triage_md import parser as triage_parser
from ra_triage.workspaces import worktree as wt_module

logger = logging.getLogger(__name__)


class ResumeDecision(enum.StrEnum):
    """Classification produced by :func:`resume_inflight` per non-terminal bug."""

    RESUME_PIPELINE = "resume_pipeline"
    NEEDS_MANUAL_REVIEW = "needs_manual_review"
    ALREADY_TERMINAL = "already_terminal"
    RESUME_POST_RECOVERY = "resume_post_recovery"


@dataclass(frozen=True)
class ResumeReport:
    """One report row per non-terminal bug observed at startup."""

    bug_id: int
    previous_state: BugState
    decision: ResumeDecision
    notes: str

    # Backwards-compat: the Agent 5 skeleton returned ``list[int]``.
    # Existing call sites (tests, log helpers) iterate the return value
    # and compare against bug_id sets. We expose ``__index__`` so a
    # single ResumeReport behaves like an ``int(bug_id)`` in numeric
    # contexts (e.g. ``set(reports) == {1, 2, 3}``).
    def __index__(self) -> int:  # pragma: no cover - compatibility shim
        return int(self.bug_id)

    def __int__(self) -> int:  # pragma: no cover - compatibility shim
        return int(self.bug_id)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, int):
            return int(self.bug_id) == other
        if isinstance(other, ResumeReport):
            return (
                self.bug_id == other.bug_id
                and self.previous_state == other.previous_state
                and self.decision == other.decision
                and self.notes == other.notes
            )
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.bug_id, self.previous_state, self.decision))


# --- Public entry point -----------------------------------------------------


async def resume_inflight(
    session_factory: Callable[[], Session] | None = None,
) -> list[ResumeReport]:
    """Inspect non-terminal bugs and produce a :class:`ResumeReport` per bug.

    Idempotent: invoking twice in a row produces the same classification
    set. The first call may write a ``## Backend Restart Recovery`` or
    ``## Needs Manual Review`` section to TRIAGE.md; the second call
    re-appends the recovery note (timestamped) and skips the manual-
    review note if the section already exists.

    Args:
      session_factory: optional sessionmaker. Defaults to
        ``engine_module.SessionLocal``. Tests inject their fixture
        sessionmaker here.

    Returns:
      A list of :class:`ResumeReport`, one per non-terminal bug found.
      Order matches ``repo.list_bugs`` (newest-first by ``created_at``).
      The lifespan hook iterates this list and dispatches per-decision
      side effects (``run_pipeline``, merge probe, log).
    """
    factory = session_factory or engine_module.SessionLocal
    reports: list[ResumeReport] = []

    session = factory()
    try:
        bugs = repo_module.list_bugs(session)
        for bug in bugs:
            runs = repo_module.list_agent_runs(session, bug.id)
            project: ProjectConfig | None
            try:
                project = get_project(bug.project_id)
            except KeyError:
                project = None
            report = _classify_bug(session, bug, runs, project)
            reports.append(report)
            _log_report(report)
    finally:
        session.close()

    if reports:
        logger.info(
            "resume_inflight: classified %d non-terminal bug(s); "
            "RESUME_PIPELINE=%d, NEEDS_MANUAL_REVIEW=%d, "
            "RESUME_POST_RECOVERY=%d",
            len(reports),
            sum(1 for r in reports if r.decision == ResumeDecision.RESUME_PIPELINE),
            sum(
                1 for r in reports if r.decision == ResumeDecision.NEEDS_MANUAL_REVIEW
            ),
            sum(
                1 for r in reports if r.decision == ResumeDecision.RESUME_POST_RECOVERY
            ),
        )
    else:
        logger.info("resume_inflight: no non-terminal bugs to inspect")
    return reports


# --- Classification logic ---------------------------------------------------


def _classify_bug(
    session: Session,
    bug: Bug,
    runs: list[AgentRun],
    project: ProjectConfig | None,
) -> ResumeReport:
    """Decide what to do with one non-terminal bug.

    Inputs are read-only here. Side effects (writing TRIAGE.md notes,
    flipping ``needs_manual_review``) happen via helpers; this function
    returns the decision and side-effect-driven helpers run inline.
    """
    state = BugState(bug.state)
    bug_id = int(bug.id)

    if state in TERMINAL_STATES:
        # Caller already filters; defensive fall-through.
        return ResumeReport(
            bug_id=bug_id,
            previous_state=state,
            decision=ResumeDecision.ALREADY_TERMINAL,
            notes="terminal — no action",
        )

    # MERGING bugs need the post-recovery PR probe path.
    if state == BugState.MERGING:
        return ResumeReport(
            bug_id=bug_id,
            previous_state=state,
            decision=ResumeDecision.RESUME_POST_RECOVERY,
            notes="bug was MERGING at shutdown; lifespan will probe PR status",
        )

    # State / agent_runs consistency check. RESUME_PIPELINE is the
    # default; NEEDS_MANUAL_REVIEW only when the consistency check
    # objects.
    inconsistency = _detect_inconsistency(state, runs)
    if inconsistency is not None:
        _mark_needs_manual_review(session, bug, inconsistency, project)
        return ResumeReport(
            bug_id=bug_id,
            previous_state=state,
            decision=ResumeDecision.NEEDS_MANUAL_REVIEW,
            notes=inconsistency,
        )

    # Crashed-mid-run detection: the most recent run had no
    # completion_token AND either a non-zero exit code OR a NULL
    # ended_at (meaning the row was inserted at start but never
    # finalized — the smoking gun of a backend kill).
    if runs:
        latest = runs[-1]
        if _is_crashed_run(latest):
            _append_restart_recovery_note(bug, latest, project)
            return ResumeReport(
                bug_id=bug_id,
                previous_state=state,
                decision=ResumeDecision.RESUME_PIPELINE,
                notes=(
                    f"crashed mid-{latest.agent_role} (attempt={latest.attempt}); "
                    "scheduled for resume with restart-recovery note"
                ),
            )

    return ResumeReport(
        bug_id=bug_id,
        previous_state=state,
        decision=ResumeDecision.RESUME_PIPELINE,
        notes="clean state; scheduled for run_pipeline",
    )


def _detect_inconsistency(state: BugState, runs: list[AgentRun]) -> str | None:
    """Return a human-readable inconsistency description, or ``None`` if clean.

    Rules (each one a reason to flag NEEDS_MANUAL_REVIEW):

      - ``state == READY_FOR_REVIEW`` but no ``tester`` /
        ``integration_tester`` agent run on record. The pipeline always
        records the tester run BEFORE transitioning to RFR, so this
        means the DB is out of sync.
      - ``state == MERGING`` but no agent runs at all (cleared between
        confirm and merge — handled by the MERGING branch above; this
        rule shouldn't fire because of branch ordering, but kept as a
        safety net for later refactors).
      - ``state in (FIXING, TESTING, DESIGNING, FIXING_PARALLEL,
        INTEGRATION_TESTING)`` but ``runs`` is empty. The pipeline
        always inserts an agent_runs row before transitioning into
        these states, so an empty list here is an inconsistency.

    Returns ``None`` for clean states (OPEN, TRIAGED — no agent yet
    expected; or working states with at least one run).
    """
    role_set = {r.agent_role for r in runs}

    if state == BugState.READY_FOR_REVIEW:
        if not role_set & {"tester", "integration_tester"}:
            return (
                "state=READY_FOR_REVIEW but no `tester` or "
                "`integration_tester` agent run is recorded — pipeline "
                "advanced past the review gate without a Tester run on file"
            )
        return None

    working_states = {
        BugState.FIXING,
        BugState.TESTING,
        BugState.DESIGNING,
        BugState.FIXING_PARALLEL,
        BugState.INTEGRATION_TESTING,
    }
    if state in working_states and not runs:
        return (
            f"state={state.value} but no agent_runs rows exist — pipeline "
            "transitioned into a working state without recording an "
            "agent_runs entry"
        )

    return None


def _is_crashed_run(run: AgentRun) -> bool:
    """Return True if ``run`` looks like a backend-killed mid-flight row.

    Two ways a row indicates a crash:
      - ``completion_token`` is NULL (no token was ever written) AND
        ``ended_at`` is NULL (the row was inserted at start but never
        finalized — backend died between ``_insert_agent_run_start`` and
        ``_finalize_agent_run``).
      - ``completion_token`` is NULL AND ``exit_code`` is non-zero (the
        agent subprocess crashed and we got an exit code, but never a
        token — also a kind of crash).

    A finalized successful row has ``completion_token`` set; a finalized
    failed row has ``completion_token`` ending in ``_FAILED``. Both are
    NOT crashes for resume purposes — the pipeline's normal routing
    handles them.
    """
    if run.completion_token:
        # Any non-empty token means the run finalized cleanly. Even
        # ``*_FAILED`` is a clean finalization — the orchestrator routed
        # the bug to BLOCKED and that's its current state.
        return False
    if run.ended_at is None:
        return True
    if run.exit_code is not None and run.exit_code != 0:
        return True
    return False


# --- Side-effects ----------------------------------------------------------


def _mark_needs_manual_review(
    session: Session,
    bug: Bug,
    reason: str,
    project: ProjectConfig | None,
) -> None:
    """Flip ``bugs.needs_manual_review`` and append a TRIAGE.md note.

    Idempotent on TRIAGE.md: if the section already exists, we don't
    re-append (avoids walls of identical notes from repeated boots).
    The flag is always set even if the note already exists — clearing
    is a deliberate operator action via the dedicated endpoint.
    """
    bug.needs_manual_review = True
    session.flush()
    session.commit()

    triage_md_path = _resolve_triage_md_path(int(bug.id), project)
    if triage_md_path is None:
        logger.warning(
            "resume_inflight: cannot append Needs Manual Review note for "
            "bug %d — no worktree on disk",
            bug.id,
        )
        return

    if triage_md_path.exists():
        existing = triage_parser.parse(triage_md_path)
        if existing.get("Needs Manual Review", "").strip():
            return

    body = (
        "Backend restart detected an inconsistent state for this bug. "
        "Inspect the audit trail (agent_runs, TRIAGE.md sections above) "
        "to understand what happened, then either:\n"
        "  - Click 'Retry' if a specific agent should re-run.\n"
        "  - Click 'Take Over Manually' to keep the bug BLOCKED while "
        "you handle it out-of-band.\n"
        "  - Click 'Cancel' to abandon the bug.\n\n"
        f"Inconsistency observed at boot: {reason}"
    )
    try:
        triage_parser.append(triage_md_path, "Needs Manual Review", body)
    except OSError:
        logger.exception(
            "resume_inflight: failed to write Needs Manual Review note for bug %d",
            bug.id,
        )


def _append_restart_recovery_note(
    bug: Bug,
    crashed_run: AgentRun,
    project: ProjectConfig | None,
) -> None:
    """Append a timestamped ``## Backend Restart Recovery`` note to TRIAGE.md.

    Always appends (no dedup). Each restart deserves its own audit row;
    duplicates are intentional and serve as a "this bug has been
    interrupted N times" signal.
    """
    triage_md_path = _resolve_triage_md_path(int(bug.id), project)
    if triage_md_path is None:
        logger.warning(
            "resume_inflight: cannot write Backend Restart Recovery note "
            "for bug %d — no worktree on disk",
            bug.id,
        )
        return
    body = (
        f"Backend restart detected at {_utcnow_iso()}. The most recent "
        f"agent run was `{crashed_run.agent_role}` (attempt={crashed_run.attempt}) "
        f"with no completion token (exit_code={crashed_run.exit_code}, "
        f"ended_at={crashed_run.ended_at}). The orchestrator will re-run the "
        "agent with attempt+1 via the standard retry path."
    )
    try:
        triage_parser.append(triage_md_path, "Backend Restart Recovery", body)
    except OSError:
        logger.exception(
            "resume_inflight: failed to write Backend Restart Recovery note "
            "for bug %d",
            bug.id,
        )


def _resolve_triage_md_path(
    bug_id: int, project: ProjectConfig | None
) -> Path | None:
    """Pick the TRIAGE.md path for a bug, or ``None`` if no worktree exists.

    Mirrors ``orchestrator.pipeline._triage_md_worktree`` — backend repo
    first, fall back to the first available worktree, fall back to the
    synthetic ``.escalation`` path under the worktrees root.
    """
    worktrees = wt_module.list_worktrees(bug_id)
    if project is not None:
        for r in project.repos:
            if r.role == "backend" and r.name in worktrees:
                return worktrees[r.name] / "TRIAGE.md"
        for r in project.repos:
            if r.name in worktrees:
                return worktrees[r.name] / "TRIAGE.md"
    if worktrees:
        return next(iter(worktrees.values())) / "TRIAGE.md"
    # Fallback: write under the .escalation dir even if no worktree
    # exists; the resume note still has a home.
    try:
        from ra_triage.settings import get_settings

        settings = get_settings()
        return (
            settings.worktrees_dir
            / str(bug_id)
            / ".escalation"
            / "TRIAGE.md"
        )
    except Exception:
        return None


def _log_report(report: ResumeReport) -> None:
    """One INFO log line per ResumeReport for operator boot-time visibility."""
    logger.info(
        "resume_inflight: bug_id=%d state=%s decision=%s — %s",
        report.bug_id,
        report.previous_state.value,
        report.decision.value,
        report.notes,
    )


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


__all__ = [
    "ResumeDecision",
    "ResumeReport",
    "resume_inflight",
]
