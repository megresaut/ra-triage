"""REST endpoints for bug lifecycle.

Agent 2 surfaces:
  - GET /bugs
  - GET /bugs/{bug_id}
  - GET /bugs/{bug_id}/agent-runs
  - POST /poller/enqueue  (manual-bypass for enqueueing a bug by Notion
    page id. Agent 3 adds real-title/description fetch when NOTION_TOKEN
    is configured and the caller omits title/description.)

Pydantic response models are the shape `openapi-typescript` converts into the
frontend's `types.ts`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ra_triage.db import engine as engine_module
from ra_triage.db import repo
from ra_triage.db.models import AgentRun, Bug
from ra_triage.integrations.notion import NotionClient, normalize_page
from ra_triage.orchestrator.pipeline import run_pipeline
from ra_triage.orchestrator.states import BugState
from ra_triage.settings import get_settings
from ra_triage.triage_md import parser as triage_parser
from ra_triage.workspaces import worktree as wt_module

logger = logging.getLogger(__name__)

router = APIRouter(tags=["bugs"])


def get_session() -> Iterator[Session]:
    """FastAPI dependency that opens and closes a DB session per request.

    Resolves `SessionLocal` via the `engine` module (not an import-time bind)
    so tests that rebind `engine_module.SessionLocal` see their override.
    """
    session = engine_module.SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class BugResponse(BaseModel):
    """Public view of a bug.

    `current_agent` and `conflict_files` are always null in Agent 2's surface
    — Agent 5 wires `current_agent` when it tracks the in-flight subprocess,
    and Agent 11 wires `conflict_files` when rebase conflicts are recorded.
    """

    id: int
    notion_page_id: str
    project_id: str
    title: str
    description: str
    repro_steps: str | None
    severity: str | None
    reporter: str | None
    state: str
    scope: str | None
    affected_repos: list[str] = Field(default_factory=list)
    deny_list_hit: bool
    rejection_count: int
    created_at: datetime
    updated_at: datetime
    terminated_at: datetime | None
    current_agent: str | None = None
    conflict_files: list[str] | None = None
    launched_for_testing: bool = False


class AgentRunResponse(BaseModel):
    """Public view of an agent_runs row."""

    id: int
    bug_id: int
    agent_role: str
    attempt: int
    started_at: datetime
    ended_at: datetime | None
    exit_code: int | None
    completion_token: str | None
    log_path: str
    prompt_path: str
    worktree_repo: str | None


class PollerEnqueueRequest(BaseModel):
    """Manual-bypass enqueue payload."""

    notion_page_id: str
    title: str | None = None
    description: str | None = None
    project_id: str = "ra_avm"


def _bug_to_response(bug: Bug, *, launched_bug_id: int | None) -> BugResponse:
    return BugResponse(
        id=bug.id,
        notion_page_id=bug.notion_page_id,
        project_id=bug.project_id,
        title=bug.title,
        description=bug.description,
        repro_steps=bug.repro_steps,
        severity=bug.severity,
        reporter=bug.reporter,
        state=bug.state,
        scope=bug.scope,
        affected_repos=repo.parse_affected_repos(bug.affected_repos),
        deny_list_hit=bug.deny_list_hit,
        rejection_count=bug.rejection_count,
        created_at=bug.created_at,
        updated_at=bug.updated_at,
        terminated_at=bug.terminated_at,
        current_agent=None,  # v2: Agent 5 populates
        conflict_files=None,  # v2: Agent 11 populates
        launched_for_testing=(launched_bug_id is not None and launched_bug_id == bug.id),
    )


def _agent_run_to_response(run: AgentRun) -> AgentRunResponse:
    return AgentRunResponse(
        id=run.id,
        bug_id=run.bug_id,
        agent_role=run.agent_role,
        attempt=run.attempt,
        started_at=run.started_at,
        ended_at=run.ended_at,
        exit_code=run.exit_code,
        completion_token=run.completion_token,
        log_path=run.log_path,
        prompt_path=run.prompt_path,
        worktree_repo=run.worktree_repo,
    )


@router.get("/bugs", response_model=list[BugResponse])
def list_bugs_endpoint(
    project_id: str | None = Query(default=None),
    state: BugState | None = Query(default=None),
    include_terminal: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> list[BugResponse]:
    """List bugs. Non-terminal only by default."""
    bugs = repo.list_bugs(
        session, project_id=project_id, state=state, include_terminal=include_terminal
    )
    current_test = repo.get_current_test_instance(session)
    launched_bug_id = current_test.bug_id if current_test else None
    return [_bug_to_response(b, launched_bug_id=launched_bug_id) for b in bugs]


@router.get("/bugs/{bug_id}", response_model=BugResponse)
def get_bug_endpoint(
    bug_id: int,
    session: Session = Depends(get_session),
) -> BugResponse:
    bug = repo.get_bug(session, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} not found")
    current_test = repo.get_current_test_instance(session)
    launched_bug_id = current_test.bug_id if current_test else None
    return _bug_to_response(bug, launched_bug_id=launched_bug_id)


@router.get("/bugs/{bug_id}/agent-runs", response_model=list[AgentRunResponse])
def list_agent_runs_endpoint(
    bug_id: int,
    session: Session = Depends(get_session),
) -> list[AgentRunResponse]:
    bug = repo.get_bug(session, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} not found")
    runs = repo.list_agent_runs(session, bug_id)
    return [_agent_run_to_response(r) for r in runs]


def _try_fetch_notion_page(page_id: str) -> dict[str, object] | None:
    """Attempt to normalize a Notion page into bug fields.

    Returns a dict of `title`/`description`/`repro_steps`/`severity`/
    `reporter`, or None if Notion is unconfigured or the fetch fails.
    Never raises — callers fall back to placeholders on None.
    """
    settings = get_settings()
    if not settings.notion_token:
        return None
    try:
        client = NotionClient(token=settings.notion_token)
        page = client.fetch_page(page_id)
        normalized = normalize_page(page.page)
    except Exception:
        logger.warning(
            "Notion fetch failed for page_id=%s; falling back to placeholders",
            page_id,
            exc_info=True,
        )
        return None
    return {
        "title": normalized.title,
        "description": normalized.description,
        "repro_steps": normalized.repro_steps,
        "severity": normalized.severity,
        "reporter": normalized.reporter,
    }


@router.post("/poller/enqueue", response_model=BugResponse)
def poller_enqueue(
    body: PollerEnqueueRequest,
    session: Session = Depends(get_session),
) -> BugResponse:
    """Enqueue a bug by `notion_page_id`.

    Behavior:
      - If `title` and `description` are provided in the body, use them
        as-is (manual bypass — Agent 2's original behavior).
      - If both are absent AND `NOTION_TOKEN` is configured, try fetching
        the page from Notion and normalize it into bug fields. On Notion
        fetch failure we fall back to the placeholder behavior and log.
      - If both are absent and Notion isn't configured, use placeholders.

    Idempotent: repeated calls with the same `notion_page_id` return the
    existing bug row unchanged regardless of which branch above fires.
    """
    caller_supplied_fields = body.title is not None or body.description is not None

    # Defaults — used if no Notion fetch happens or it fails.
    title = body.title or f"Bug {body.notion_page_id}"
    description = body.description or "(manually enqueued, no Notion fetch)"
    repro_steps: str | None = None
    severity: str | None = None
    reporter: str | None = None

    if not caller_supplied_fields:
        fetched = _try_fetch_notion_page(body.notion_page_id)
        if fetched is not None:
            title = str(fetched.get("title") or title)
            description = str(fetched.get("description") or description)
            repro_steps = fetched.get("repro_steps")  # type: ignore[assignment]
            severity = fetched.get("severity")  # type: ignore[assignment]
            reporter = fetched.get("reporter")  # type: ignore[assignment]

    bug = repo.create_bug(
        session,
        notion_page_id=body.notion_page_id,
        project_id=body.project_id,
        title=title,
        description=description,
        repro_steps=repro_steps,
        severity=severity,
        reporter=reporter,
    )
    current_test = repo.get_current_test_instance(session)
    launched_bug_id = current_test.bug_id if current_test else None

    # Kick off the pipeline without blocking the HTTP response. If the bug
    # already existed (idempotent re-enqueue), we still schedule a task —
    # the pipeline itself short-circuits on already-terminal/late states.
    _schedule_pipeline(bug.id)

    return _bug_to_response(bug, launched_bug_id=launched_bug_id)


def _schedule_pipeline(bug_id: int) -> None:
    """Best-effort ``asyncio.create_task(run_pipeline(bug_id))``.

    The endpoint runs on FastAPI's threadpool (sync def), so the running
    asyncio loop is the FastAPI app loop. We find it via
    ``asyncio.get_event_loop_policy().get_event_loop()`` when we're not on
    the loop thread, and fall back to the bus's attached loop — the same
    pattern ``EventBus.publish_sync`` uses.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        # No loop on this thread — use the one the lifespan attached to
        # the event bus. That's the FastAPI app loop.
        from ra_triage.utils.events import bus

        attached = bus.get_loop()
        if attached is None:
            logger.warning(
                "Cannot schedule run_pipeline for bug %d: no running loop",
                bug_id,
            )
            return
        target_loop = attached
        try:
            target_loop.call_soon_threadsafe(
                lambda: target_loop.create_task(run_pipeline(bug_id))
            )
        except RuntimeError:
            logger.exception(
                "Failed to schedule run_pipeline for bug %d", bug_id
            )
        return

    loop.create_task(run_pipeline(bug_id))


# --- TRIAGE.md & prompt endpoints (Agent 5) --------------------------


class TriageResponse(BaseModel):
    """Parsed TRIAGE.md payload for a bug."""

    sections: dict[str, str]
    raw: str
    path: str


@router.get("/bugs/{bug_id}/triage", response_model=TriageResponse)
def get_triage_endpoint(
    bug_id: int,
    session: Session = Depends(get_session),
) -> TriageResponse:
    """Return parsed TRIAGE.md sections + raw content for a bug.

    Strategy: identify the bug's primary (backend-first) worktree via the
    project registry, then read ``<worktree>/TRIAGE.md`` from it. 404 if
    no worktree exists on disk. Returns ``sections={}`` with an empty
    ``raw`` if the worktree exists but TRIAGE.md has not been written
    yet.
    """
    bug = repo.get_bug(session, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} not found")

    worktrees = wt_module.list_worktrees(bug_id)
    if not worktrees:
        raise HTTPException(
            status_code=404,
            detail=f"No worktrees on disk for bug {bug_id}; pipeline may not have started",
        )

    triage_md = _pick_triage_md_path(bug.project_id, worktrees)
    sections = triage_parser.parse(triage_md) if triage_md.exists() else {}
    raw = triage_md.read_text(encoding="utf-8") if triage_md.exists() else ""
    return TriageResponse(sections=sections, raw=raw, path=str(triage_md))


def _pick_triage_md_path(project_id: str, worktrees: dict[str, Path]) -> Path:
    """Return the TRIAGE.md path for the bug's primary worktree.

    Mirrors ``orchestrator.pipeline._triage_md_worktree``: prefer the
    backend repo, fall back to the first available worktree.
    """
    try:
        from ra_triage.config.registry import get_project

        project = get_project(project_id)
    except KeyError:
        return next(iter(worktrees.values())) / "TRIAGE.md"

    for r in project.repos:
        if r.role == "backend" and r.name in worktrees:
            return worktrees[r.name] / "TRIAGE.md"
    # fall back to first configured repo that actually has a worktree
    for r in project.repos:
        if r.name in worktrees:
            return worktrees[r.name] / "TRIAGE.md"
    return next(iter(worktrees.values())) / "TRIAGE.md"


@router.get(
    "/bugs/{bug_id}/prompt",
    response_class=PlainTextResponse,
    responses={
        200: {"content": {"text/markdown": {}}, "description": "Rendered prompt"},
        404: {"description": "Prompt not found on disk"},
    },
)
def get_prompt_endpoint(
    bug_id: int,
    role: str = Query(..., description="Agent role, e.g. 'investigator'"),
    attempt: int = Query(..., ge=1),
    session: Session = Depends(get_session),
) -> PlainTextResponse:
    """Return the rendered prompt text for a specific (role, attempt).

    Convenient for debugging agent output against the exact prompt the
    CLI received. 404 if the prompt file is missing.
    """
    bug = repo.get_bug(session, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} not found")

    settings = get_settings()
    prompt_path = settings.prompts_dir / str(bug_id) / f"{role}.{attempt}.md"
    if not prompt_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Prompt not found: {prompt_path}",
        )
    return PlainTextResponse(
        prompt_path.read_text(encoding="utf-8"),
        media_type="text/markdown",
    )
