"""REST endpoints for the Escalation Brief action flow (Agent 9).

When a bug lands in ``BLOCKED`` with a populated Escalation Brief, the
Investigator has already written the structured brief to ``TRIAGE.md``
and the pipeline has persisted an ``escalations`` row with the parsed
options. The frontend fetches the brief via ``GET /bugs/:id/escalation``
(see :mod:`ra_triage.api.bugs`) and renders three families of action:

  - **Approve Recommended** — ``POST /bugs/:id/escalation/approve``.
    Validates the option id is ``"approve_recommended"``, transitions the
    bug out of ``BLOCKED`` into ``FIXING`` (single-repo) or
    ``FIXING_PARALLEL`` (cross-repo), and kicks off the Fixer with an
    ``escalation_context`` string rendered into its prompt template.
  - **Pick Alternative N** — ``POST /bugs/:id/escalation/pick-alternative``
    with ``{option_id, notes?}``. Same transition as approve, but the
    ``escalation_context`` payload is synthesized from the specific
    alternative's body plus any notes the reviewer attached.
  - **Take Over Manually** — ``POST /bugs/:id/escalation/take-over``.
    Bug stays in ``BLOCKED``; we append a ``## Taken Over Manually``
    section to TRIAGE.md, record ``resolution="taken_over"`` on the
    escalation row, and DO NOT clean up the worktree (Megha uses it out
    of band). Mirrors the generic ``/bugs/:id/take-over`` endpoint but
    preserves the escalation-row resolution tracking.

Status codes:
  - 202 Accepted for approve / pick-alternative (async pipeline kick-off).
  - 200 OK for take-over (synchronous).
  - 400 Bad Request when the ``option_id`` doesn't match the escalation.
  - 404 Not Found when the bug or escalation doesn't exist / is resolved.
  - 409 Conflict when the bug's state doesn't permit an action.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ra_triage.config.registry import get_project
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_lib
from ra_triage.db.models import Bug, Escalation
from ra_triage.orchestrator.pipeline import run_pipeline_with_escalation_context
from ra_triage.orchestrator.states import (
    BugState,
    IllegalBugStateTransition,
)
from ra_triage.triage_md import parser as triage_parser

logger = logging.getLogger(__name__)

router = APIRouter(tags=["escalations"])


# --- Shared dependency (mirrors api.bugs.get_session) ---------------------


def _get_session() -> Iterator[Session]:
    """FastAPI dependency that opens and closes a DB session per request."""
    session = engine_module.SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --- Request / response shapes --------------------------------------------


class ApproveRequest(BaseModel):
    """Body for ``POST /bugs/{bug_id}/escalation/approve``."""

    option_id: str = Field(min_length=1, max_length=64)


class PickAlternativeRequest(BaseModel):
    """Body for ``POST /bugs/{bug_id}/escalation/pick-alternative``."""

    option_id: str = Field(min_length=1, max_length=64)
    notes: str | None = Field(default=None, max_length=500)


class EscalationActionResponse(BaseModel):
    """Response for approve / pick-alternative."""

    status: str
    bug_id: int
    escalation_id: int
    new_state: str
    resolved_option: str


class TakeOverEscalationResponse(BaseModel):
    """Response for take-over."""

    status: str
    bug_id: int
    escalation_id: int
    resolved_option: str


# --- Approve Recommended --------------------------------------------------


@router.post(
    "/bugs/{bug_id}/escalation/approve",
    response_model=EscalationActionResponse,
    status_code=202,
)
def approve_escalation(
    bug_id: int,
    body: ApproveRequest,
    session: Session = Depends(_get_session),
) -> EscalationActionResponse:
    """Approve the Investigator's recommended fix.

    - ``option_id`` must be ``"approve_recommended"`` (defence-in-depth:
      the frontend only offers this value, but a malformed payload
      deserves a clear 400).
    - The bug must be in ``BLOCKED`` with an unresolved escalation row.
    - Transitions BLOCKED → FIXING (single-repo) or FIXING_PARALLEL
      (cross-repo) based on the bug's ``scope`` column, then schedules
      :func:`run_pipeline_with_escalation_context` so the Fixer
      receives the approved brief.
    """
    if body.option_id != "approve_recommended":
        raise HTTPException(
            status_code=400,
            detail=(
                f"option_id must be 'approve_recommended'; got {body.option_id!r}"
            ),
        )
    bug, escalation = _load_bug_and_escalation(session, bug_id)
    _validate_option_in_escalation(escalation, body.option_id)

    target_state = _target_state_for_approve(bug)
    context = _build_approve_context(
        bug=bug, escalation=escalation
    )

    _transition_or_409(session, bug, target_state)
    repo_lib.update_escalation_resolution(
        session,
        escalation_id=escalation.id,
        resolution="approved",
        resolved_option="approve_recommended",
    )
    session.commit()

    _schedule_pipeline_with_context(bug_id, context)

    return EscalationActionResponse(
        status="approved",
        bug_id=bug_id,
        escalation_id=escalation.id,
        new_state=target_state.value,
        resolved_option="approve_recommended",
    )


# --- Pick Alternative N ---------------------------------------------------


@router.post(
    "/bugs/{bug_id}/escalation/pick-alternative",
    response_model=EscalationActionResponse,
    status_code=202,
)
def pick_alternative(
    bug_id: int,
    body: PickAlternativeRequest,
    session: Session = Depends(_get_session),
) -> EscalationActionResponse:
    """Pick a specific alternative from the Escalation Brief.

    - ``option_id`` must be one of the ``alternative_N`` entries in the
      escalation's stored ``options`` list.
    - ``notes`` (optional, 500 chars max) lets the reviewer add
      free-text guidance that gets appended to the ``escalation_context``
      string handed to the Fixer.
    - Transitions + scheduling are identical to approve.
    """
    if not body.option_id.startswith("alternative_"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"option_id must start with 'alternative_'; got {body.option_id!r}"
            ),
        )

    bug, escalation = _load_bug_and_escalation(session, bug_id)
    _validate_option_in_escalation(escalation, body.option_id)

    target_state = _target_state_for_approve(bug)

    # Parse the brief to pull the specific alternative's body so we can
    # include it in escalation_context verbatim. If parsing fails we
    # still continue — the reviewer's notes + the raw brief are enough.
    alternative_body = _extract_alternative_body(escalation, body.option_id)

    context = _build_pick_alternative_context(
        bug=bug,
        escalation=escalation,
        option_id=body.option_id,
        alternative_body=alternative_body,
        notes=body.notes,
    )

    _transition_or_409(session, bug, target_state)
    repo_lib.update_escalation_resolution(
        session,
        escalation_id=escalation.id,
        resolution="picked_alt",
        resolved_option=body.option_id,
    )
    session.commit()

    _schedule_pipeline_with_context(bug_id, context)

    return EscalationActionResponse(
        status="picked_alternative",
        bug_id=bug_id,
        escalation_id=escalation.id,
        new_state=target_state.value,
        resolved_option=body.option_id,
    )


# --- Take Over Manually ---------------------------------------------------


@router.post(
    "/bugs/{bug_id}/escalation/take-over",
    response_model=TakeOverEscalationResponse,
    status_code=200,
)
def take_over_escalation(
    bug_id: int,
    session: Session = Depends(_get_session),
) -> TakeOverEscalationResponse:
    """Mark the escalation taken-over manually; bug stays in BLOCKED.

    Unlike :func:`ra_triage.api.bugs.take_over_bug_endpoint`, which can be
    invoked from any non-terminal state, this endpoint is specifically
    scoped to resolving an active escalation row:

      - Bug must be in ``BLOCKED`` with an unresolved escalation.
      - Records ``resolution='taken_over'`` + ``resolved_option='take_over'``.
      - Appends a ``## Taken Over Manually`` section to TRIAGE.md (shared
        with the generic take-over flow).
      - Does NOT clean up the worktree. Does NOT change the bug state.
    """
    bug, escalation = _load_bug_and_escalation(session, bug_id)

    repo_lib.update_escalation_resolution(
        session,
        escalation_id=escalation.id,
        resolution="taken_over",
        resolved_option="take_over",
    )
    session.commit()

    # Append the ## Taken Over Manually marker using the shared helper
    # in api.bugs so both take-over flows produce identical TRIAGE.md
    # output.
    from ra_triage.api.bugs import _append_take_over_marker

    try:
        _append_take_over_marker(bug)
    except Exception:
        logger.exception(
            "take_over_escalation: failed to append marker for bug_id=%s "
            "(swallowing; escalation row already updated)",
            bug_id,
        )

    return TakeOverEscalationResponse(
        status="taken_over",
        bug_id=bug_id,
        escalation_id=escalation.id,
        resolved_option="take_over",
    )


# --- Helpers --------------------------------------------------------------


def _load_bug_and_escalation(
    session: Session, bug_id: int
) -> tuple[Bug, Escalation]:
    """Load the bug + its latest unresolved escalation, or raise HTTPException.

    Returns the ORM rows still bound to ``session``. All three action
    endpoints require (bug exists) AND (bug is BLOCKED) AND (there's an
    unresolved escalation).
    """
    bug = repo_lib.get_bug(session, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} not found")
    if bug.state != BugState.BLOCKED.value:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Bug {bug_id} is in state {bug.state!r}; escalation actions "
                f"require BLOCKED"
            ),
        )
    escalation = repo_lib.get_latest_unresolved_escalation(session, bug_id)
    if escalation is None:
        raise HTTPException(
            status_code=404,
            detail=f"No active (unresolved) escalation for bug {bug_id}",
        )
    return bug, escalation


def _validate_option_in_escalation(
    escalation: Escalation, option_id: str
) -> None:
    """Ensure the requested ``option_id`` is present in the stored options.

    The escalation's ``options`` column is a JSON string of
    ``ParsedBriefOption`` dicts (``{id, label, kind}``). 400 if the
    option id doesn't appear; 500 if the stored blob is corrupt (the
    orchestrator should have persisted a valid JSON list).
    """
    try:
        parsed_options = json.loads(escalation.options)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Escalation row {escalation.id} has corrupt options JSON: {exc}"
            ),
        ) from exc
    valid_ids = {
        opt.get("id")
        for opt in parsed_options
        if isinstance(opt, dict)
    }
    if option_id not in valid_ids:
        raise HTTPException(
            status_code=400,
            detail=(
                f"option_id {option_id!r} is not one of the stored escalation "
                f"options {sorted(x for x in valid_ids if x)}"
            ),
        )


def _target_state_for_approve(bug: Bug) -> BugState:
    """Decide FIXING vs. FIXING_PARALLEL based on the bug's scope.

    Cross-repo bugs go to FIXING_PARALLEL; single-repo (or unknown scope
    with a single configured repo) go to FIXING.
    """
    scope = (bug.scope or "").strip().lower()
    if scope == "cross-repo":
        return BugState.FIXING_PARALLEL
    return BugState.FIXING


def _transition_or_409(
    session: Session, bug: Bug, new_state: BugState
) -> None:
    """Transition the bug via the state machine; raise 409 on illegal move."""
    try:
        repo_lib.update_bug_state(session, bug, new_state)
    except IllegalBugStateTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _extract_alternative_body(escalation: Escalation, option_id: str) -> str | None:
    """Pull the prose body for ``alternative_N`` from the stored brief.

    Parses the raw brief markdown with :func:`parse_escalation_brief`
    and indexes into ``ranked_alternatives`` by N (1-based). Returns
    ``None`` if the brief is unparseable or the alternative index is out
    of range.
    """
    parsed = triage_parser.parse_escalation_brief(
        {"Escalation Brief": escalation.brief_markdown}
    )
    if parsed is None:
        return None
    # option_id is guaranteed to start with "alternative_" here.
    try:
        n = int(option_id.split("_", 1)[1])
    except (ValueError, IndexError):
        return None
    if n < 1 or n > len(parsed.ranked_alternatives):
        return None
    return parsed.ranked_alternatives[n - 1]


def _build_approve_context(*, bug: Bug, escalation: Escalation) -> str:
    """Assemble the ``escalation_context`` string for the approve flow.

    Includes the TL;DR and the proposed approach so the Fixer sees the
    reviewer-approved direction without having to re-read TRIAGE.md
    end-to-end (the Fixer still has access to the file on disk).
    """
    parsed = triage_parser.parse_escalation_brief(
        {"Escalation Brief": escalation.brief_markdown}
    )
    lines: list[str] = [
        f"The reviewer has APPROVED the Investigator's recommended approach for bug #{bug.id}.",
        "",
    ]
    if parsed and parsed.tldr:
        lines.append(f"TL;DR: {parsed.tldr}")
    if parsed and parsed.proposed_approach:
        lines.append("")
        lines.append("Proposed approach (approved):")
        lines.append(parsed.proposed_approach)
    if parsed and parsed.files_that_would_be_touched:
        lines.append("")
        lines.append("Files in scope:")
        for p in parsed.files_that_would_be_touched:
            lines.append(f"  - {p}")
    # Always include the raw brief at the end so the Fixer has the full
    # context for ambiguous judgment calls.
    lines.append("")
    lines.append("Full escalation brief (verbatim):")
    lines.append(escalation.brief_markdown.rstrip())
    return "\n".join(lines)


def _build_pick_alternative_context(
    *,
    bug: Bug,
    escalation: Escalation,
    option_id: str,
    alternative_body: str | None,
    notes: str | None,
) -> str:
    """Assemble ``escalation_context`` for the pick-alternative flow."""
    parsed = triage_parser.parse_escalation_brief(
        {"Escalation Brief": escalation.brief_markdown}
    )
    lines: list[str] = [
        f"The reviewer has PICKED {option_id} as the preferred approach for bug #{bug.id}.",
        "",
    ]
    if parsed and parsed.tldr:
        lines.append(f"TL;DR: {parsed.tldr}")
    if alternative_body:
        lines.append("")
        lines.append(f"Selected alternative ({option_id}):")
        lines.append(alternative_body)
    if notes:
        lines.append("")
        lines.append("Reviewer notes:")
        lines.append(notes.strip())
    lines.append("")
    lines.append("Full escalation brief (verbatim):")
    lines.append(escalation.brief_markdown.rstrip())
    return "\n".join(lines)


def _schedule_pipeline_with_context(
    bug_id: int, escalation_context: str
) -> None:
    """Best-effort schedule of ``run_pipeline_with_escalation_context``.

    Matches the pattern used elsewhere in ``api.bugs``: if we're on the
    FastAPI threadpool (sync def), attach the task to the event bus's
    stored loop; if we're already on the loop thread, create directly.
    """
    coro = run_pipeline_with_escalation_context(bug_id, escalation_context)
    _schedule_coro(coro, name=f"escalation_pipeline:{bug_id}")


def _schedule_coro(coro: Any, *, name: str) -> None:
    """Best-effort ``asyncio.create_task(coro)`` on the app event loop.

    Extracted here (rather than shared with ``api.bugs``) to keep this
    module import-graph simple.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        from ra_triage.utils.events import bus

        attached = bus.get_loop()
        if attached is None:
            logger.warning(
                "Cannot schedule %s: no running loop (coro=%r)", name, coro
            )
            if hasattr(coro, "close"):
                coro.close()
            return
        target_loop = attached
        try:
            target_loop.call_soon_threadsafe(
                lambda: target_loop.create_task(coro, name=name)
            )
        except RuntimeError:
            logger.exception("Failed to schedule %s", name)
            if hasattr(coro, "close"):
                coro.close()
        return

    loop.create_task(coro, name=name)


# ``get_project`` is imported for side-effect consumers below; keep
# the lint check happy.
_ = get_project


__all__ = [
    "ApproveRequest",
    "EscalationActionResponse",
    "PickAlternativeRequest",
    "TakeOverEscalationResponse",
    "router",
]
