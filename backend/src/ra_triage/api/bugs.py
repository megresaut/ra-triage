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
import subprocess
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ra_triage.config.registry import ProjectConfig, RepoConfig, get_project
from ra_triage.db import engine as engine_module
from ra_triage.db import repo
from ra_triage.db.models import AgentRun, Bug
from ra_triage.integrations.notion import NotionClient, normalize_page
from ra_triage.orchestrator.merge import merge_bug
from ra_triage.orchestrator.pipeline import (
    reinvestigate_with_feedback,
    retry_last_agent,
    run_pipeline,
)
from ra_triage.orchestrator.states import (
    TERMINAL_STATES,
    BugState,
    IllegalBugStateTransition,
)
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


class PullRequestResponse(BaseModel):
    """Public view of a ``pull_requests`` row for a bug.

    Populated by Agent 8's merge driver; rendered on BugDetail so Megha
    sees the PR URL immediately after clicking Confirm (and tracks it to
    ``merged`` status once :func:`orchestrator.merge.merge_bug` finishes).
    """

    id: int
    repo_name: str
    branch: str
    pr_url: str | None
    pr_number: int | None
    status: str
    merged_at: datetime | None
    created_at: datetime


class BugResponse(BaseModel):
    """Public view of a bug.

    `current_agent` and `conflict_files` are always null in Agent 2's surface
    — Agent 5 wires `current_agent` when it tracks the in-flight subprocess,
    and Agent 11 wires `conflict_files` when rebase conflicts are recorded.

    ``pull_requests`` is populated by Agent 8's merge flow (empty list
    until the Confirm button is clicked).
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
    pull_requests: list[PullRequestResponse] = Field(default_factory=list)


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
    # bug.pull_requests is eager-loaded via the ORM relationship.
    prs = [
        PullRequestResponse(
            id=pr.id,
            repo_name=pr.repo_name,
            branch=pr.branch,
            pr_url=pr.pr_url,
            pr_number=pr.pr_number,
            status=pr.status,
            merged_at=pr.merged_at,
            created_at=pr.created_at,
        )
        for pr in sorted(
            bug.pull_requests, key=lambda p: (p.created_at, p.id)
        )
    ]
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
        pull_requests=prs,
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
    """Best-effort ``asyncio.create_task(run_pipeline(bug_id))``."""
    _schedule_coro(run_pipeline(bug_id), name=f"run_pipeline:{bug_id}")


def _schedule_coro(coro: Any, *, name: str) -> None:
    """Best-effort ``asyncio.create_task(coro)`` on the app event loop.

    Endpoints run on FastAPI's threadpool (sync def), so the event loop
    we want is the one attached to the event bus by the lifespan hook.
    If we're already on the loop thread (async endpoint), we use that
    directly. On failure we log and drop — the caller will surface a
    stale state via polling eventually.
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
            # Close the coroutine so we don't leak a "never awaited" warning.
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


# --- Diff endpoints (Agent 6) --------------------------------------------


class DiffResponse(BaseModel):
    """Unified diff of a bug's worktree vs ``origin/<main_branch>``."""

    repo: str
    base_ref: str
    head_ref: str
    diff: str


class DiffSummaryResponse(BaseModel):
    """Shortstat summary of the Fixer's changes in a worktree."""

    repo: str
    files_changed: int
    insertions: int
    deletions: int


def _resolve_repo_config(project_id: str, repo_name: str) -> RepoConfig:
    """Return the registry entry for ``(project_id, repo_name)`` or 404."""
    try:
        project = get_project(project_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"Unknown project: {project_id}"
        ) from exc
    for r in project.repos:
        if r.name == repo_name:
            return r
    raise HTTPException(
        status_code=404,
        detail=f"Repo {repo_name!r} not configured on project {project_id!r}",
    )


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``cwd``; capture stdout as text.

    Uses ``subprocess.run`` (blocking) because the diff endpoints run on
    FastAPI's threadpool (sync def) and the canonical git subprocess
    helpers in ``workspaces.worktree`` are async. Keeping this local
    keeps the module dependency surface small.
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


@router.get("/bugs/{bug_id}/diff", response_model=DiffResponse)
def get_bug_diff(
    bug_id: int,
    repo: str = Query(..., description="Repo name from the project registry"),
    session: Session = Depends(get_session),
) -> DiffResponse:
    """Return the unified diff of the bug's worktree vs ``origin/<main>``.

    Strategy: locate the bug, resolve the repo via the project registry,
    derive the worktree path via ``workspaces.worktree.path_for``. 404 if
    the worktree doesn't exist on disk (pipeline hasn't started, bug was
    cleaned up, or the repo name is wrong). Runs ``git diff --no-color
    origin/<main_branch>`` in the worktree and returns stdout verbatim.

    On a git failure we surface the stderr as a 500 — the frontend
    renders the error text so operators can act.
    """
    # Reuse the shadowing-adjacent name resolution.
    bug = _repo_module_get_bug(session, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} not found")

    repo_config = _resolve_repo_config(bug.project_id, repo)
    worktree = wt_module.path_for(bug_id, repo_config.name)
    if not worktree.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"No worktree on disk for bug {bug_id} repo {repo_config.name!r}",
        )

    base_ref = f"origin/{repo_config.main_branch}"
    head_ref = f"{repo_config.branch_prefix}{bug_id}"

    completed = _run_git(
        ["diff", "--no-color", base_ref],
        cwd=worktree,
    )
    if completed.returncode != 0:
        logger.warning(
            "git diff failed for bug %d repo %s: %s",
            bug_id,
            repo_config.name,
            completed.stderr.strip(),
        )
        raise HTTPException(
            status_code=500,
            detail=f"git diff failed: {completed.stderr.strip()}",
        )

    return DiffResponse(
        repo=repo_config.name,
        base_ref=base_ref,
        head_ref=head_ref,
        diff=completed.stdout,
    )


@router.get("/bugs/{bug_id}/diff/summary", response_model=DiffSummaryResponse)
def get_bug_diff_summary(
    bug_id: int,
    repo: str = Query(..., description="Repo name from the project registry"),
    session: Session = Depends(get_session),
) -> DiffSummaryResponse:
    """Return ``{files_changed, insertions, deletions}`` from ``git diff --shortstat``.

    Cheaper than fetching the full diff; useful for the queue view.
    """
    bug = _repo_module_get_bug(session, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} not found")

    repo_config = _resolve_repo_config(bug.project_id, repo)
    worktree = wt_module.path_for(bug_id, repo_config.name)
    if not worktree.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"No worktree on disk for bug {bug_id} repo {repo_config.name!r}",
        )

    base_ref = f"origin/{repo_config.main_branch}"
    completed = _run_git(
        ["diff", "--no-color", "--shortstat", base_ref],
        cwd=worktree,
    )
    if completed.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"git diff --shortstat failed: {completed.stderr.strip()}",
        )

    files, ins, dels = _parse_shortstat(completed.stdout)
    return DiffSummaryResponse(
        repo=repo_config.name,
        files_changed=files,
        insertions=ins,
        deletions=dels,
    )


def _parse_shortstat(text: str) -> tuple[int, int, int]:
    """Parse ``git diff --shortstat`` output.

    Example: ``" 2 files changed, 4 insertions(+), 1 deletion(-)"``.
    Any missing piece (no insertions, no deletions, or empty input)
    collapses to zero.
    """
    files = ins = dels = 0
    for chunk in text.strip().split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split()
        if not parts:
            continue
        try:
            n = int(parts[0])
        except ValueError:
            continue
        if "file" in chunk:
            files = n
        elif "insertion" in chunk:
            ins = n
        elif "deletion" in chunk:
            dels = n
    return files, ins, dels


# Alias to avoid shadowing the module-level ``repo`` import with the
# ``repo`` query parameter on the diff endpoints above. ``repo.get_bug``
# wouldn't work there because ``repo`` is the local query string.
_repo_module_get_bug = repo.get_bug


# --- Agent 8: Confirm / Reject / Cancel / Retry / Take Over --------------

#: Rejection count at which the bug pins to BLOCKED instead of looping back
#: to INVESTIGATING. Matches orchestrator instructions (Flow 3) and product
#: plan.
REJECTION_LIMIT: int = 3


class RejectRequest(BaseModel):
    """Body for ``POST /bugs/{bug_id}/reject``."""

    reason: str = Field(min_length=1, max_length=2000)


class ConfirmResponse(BaseModel):
    """Response for ``POST /bugs/{bug_id}/confirm``."""

    status: str
    bug_id: int


class RejectResponse(BaseModel):
    """Response for ``POST /bugs/{bug_id}/reject``."""

    status: str
    bug_id: int
    rejection_count: int
    new_state: str


class CancelResponse(BaseModel):
    """Response for ``POST /bugs/{bug_id}/cancel``."""

    status: str
    bug_id: int


class RetryResponse(BaseModel):
    """Response for ``POST /bugs/{bug_id}/retry``."""

    status: str
    bug_id: int
    agent_role: str
    attempt: int


class TakeOverResponse(BaseModel):
    """Response for ``POST /bugs/{bug_id}/take-over``."""

    status: str
    bug_id: int


@router.post(
    "/bugs/{bug_id}/confirm",
    response_model=ConfirmResponse,
    status_code=202,
    tags=["review"],
)
def confirm_bug_endpoint(
    bug_id: int,
    session: Session = Depends(get_session),
) -> ConfirmResponse:
    """Confirm the bug's fix — kicks off the merge pipeline.

    Validates the bug is in ``READY_FOR_REVIEW`` (409 otherwise). Returns
    202 immediately; the merge itself runs as a background task via
    :func:`orchestrator.merge.merge_bug`. The frontend subscribes to the
    bug's state channel to observe MERGING → FIXED or MERGING → BLOCKED.
    """
    bug = repo.get_bug(session, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} not found")
    if bug.state != BugState.READY_FOR_REVIEW.value:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Bug {bug_id} is in state {bug.state!r}; confirm requires "
                f"READY_FOR_REVIEW"
            ),
        )
    _schedule_coro(merge_bug(bug_id), name=f"merge_bug:{bug_id}")
    return ConfirmResponse(status="merging_started", bug_id=bug_id)


@router.post(
    "/bugs/{bug_id}/reject",
    response_model=RejectResponse,
    status_code=202,
    tags=["review"],
)
def reject_bug_endpoint(
    bug_id: int,
    body: RejectRequest,
    session: Session = Depends(get_session),
) -> RejectResponse:
    """Reject the current READY_FOR_REVIEW fix.

    Records the rejection row (bumps ``rejection_count``) and either:
      - bumps to the rejection limit → transition to BLOCKED and append a
        ``## Rejection Limit Reached`` section to TRIAGE.md listing every
        reason collected for this bug.
      - otherwise → transition back to INVESTIGATING and schedule
        :func:`orchestrator.pipeline.reinvestigate_with_feedback` with
        this rejection's reason so the Investigator sees it in the
        rendered prompt.

    409 if the bug isn't in READY_FOR_REVIEW.
    """
    bug = repo.get_bug(session, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} not found")
    if bug.state != BugState.READY_FOR_REVIEW.value:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Bug {bug_id} is in state {bug.state!r}; reject requires "
                f"READY_FOR_REVIEW"
            ),
        )

    # Record the rejection first so `rejection_count` reflects THIS click.
    repo.record_rejection(session, bug_id=bug_id, reason=body.reason)
    session.flush()
    new_count = bug.rejection_count

    if new_count >= REJECTION_LIMIT:
        # Pin to BLOCKED with a rolling list of all reasons.
        try:
            repo.update_bug_state(session, bug, BugState.BLOCKED)
        except IllegalBugStateTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.commit()
        _append_rejection_limit_section(session, bug=bug)
        return RejectResponse(
            status="blocked",
            bug_id=bug_id,
            rejection_count=new_count,
            new_state=BugState.BLOCKED.value,
        )

    # Under the limit — transition back to INVESTIGATING and re-run
    # the Investigator with the reason threaded through.
    try:
        repo.update_bug_state(session, bug, BugState.INVESTIGATING)
    except IllegalBugStateTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    _schedule_coro(
        reinvestigate_with_feedback(bug_id, body.reason),
        name=f"reinvestigate:{bug_id}",
    )
    return RejectResponse(
        status="reinvestigating",
        bug_id=bug_id,
        rejection_count=new_count,
        new_state=BugState.INVESTIGATING.value,
    )


def _append_rejection_limit_section(session: Session, *, bug: Bug) -> None:
    """Write a ``## Rejection Limit Reached`` block to TRIAGE.md.

    Lists every reject reason in the order they were recorded so the human
    reviewer can see what's been tried.
    """
    project: ProjectConfig | None
    try:
        project = get_project(bug.project_id)
    except KeyError:
        project = None
    triage_md_path = _merge_triage_md_path(bug.id, project)
    rejections = repo.list_rejections(session, bug.id)
    lines = [
        "The reviewer has rejected this fix 3 times. The bug is now pinned "
        "to BLOCKED — re-investigation alone has not converged on an "
        "acceptable fix. Review the reasons below and either take over "
        "manually, pick an alternative approach, or cancel.",
        "",
        "**Rejection reasons (oldest first):**",
    ]
    for idx, rej in enumerate(rejections, start=1):
        reason = (rej.reason or "").strip() or "(empty)"
        lines.append(f"{idx}. {reason}")
    try:
        triage_parser.append(
            triage_md_path, "Rejection Limit Reached", "\n".join(lines)
        )
    except OSError:
        logger.exception(
            "Could not append Rejection Limit Reached section to %s",
            triage_md_path,
        )


def _merge_triage_md_path(bug_id: int, project: ProjectConfig | None) -> Path:
    """Pick the TRIAGE.md path for post-review writes (Agent 8 helpers).

    Mirrors the merge driver's resolution: backend repo first, fall back
    to the first worktree, ultimately a synthetic path under the bug's
    .escalation dir.
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
    settings = get_settings()
    return (
        settings.worktrees_dir / str(bug_id) / ".escalation" / "TRIAGE.md"
    )


@router.post(
    "/bugs/{bug_id}/cancel",
    response_model=CancelResponse,
    tags=["review"],
)
def cancel_bug_endpoint(
    bug_id: int,
    session: Session = Depends(get_session),
) -> CancelResponse:
    """Cancel the bug. Allowed from any non-terminal state.

    Transitions to CANCELLED (which also fires Notion status write-back
    to "Won't Fix" via the hook) and cleans up the bug's worktrees. The
    cleanup is best-effort — we swallow errors so the state transition
    always commits.
    """
    bug = repo.get_bug(session, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} not found")
    current = BugState(bug.state)
    if current in TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"Bug {bug_id} is already terminal ({current.value})",
        )
    try:
        repo.update_bug_state(session, bug, BugState.CANCELLED)
    except IllegalBugStateTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()

    # Worktree cleanup fires on the event loop so the sync endpoint can
    # return immediately. The cleanup helper is idempotent and logs on
    # failure.
    try:
        project = get_project(bug.project_id)
    except KeyError:
        project = None
    if project is not None:
        _schedule_coro(
            wt_module.cleanup(bug_id, list(project.repos)),
            name=f"cleanup:{bug_id}",
        )
    return CancelResponse(status="cancelled", bug_id=bug_id)


@router.post(
    "/bugs/{bug_id}/retry",
    response_model=RetryResponse,
    status_code=202,
    tags=["review"],
)
def retry_bug_endpoint(
    bug_id: int,
    session: Session = Depends(get_session),
) -> RetryResponse:
    """Re-run the most recent agent role with ``attempt += 1``.

    409 if the bug is terminal or if no prior agent run exists, or if the
    latest run doesn't satisfy retry-eligibility (see
    :func:`orchestrator.pipeline.retry_last_agent`).
    """
    bug = repo.get_bug(session, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} not found")

    current = BugState(bug.state)
    if current in TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"Bug {bug_id} is terminal ({current.value}); retry not allowed",
        )

    runs = repo.list_agent_runs(session, bug_id)
    if not runs:
        raise HTTPException(
            status_code=409,
            detail=f"Bug {bug_id} has no agent runs to retry",
        )
    latest = runs[-1]
    # BLOCKED-path retries require the latest run to be a *_FAILED.
    if current == BugState.BLOCKED:
        tok = latest.completion_token or ""
        if not tok.endswith("_FAILED"):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Bug {bug_id} is BLOCKED but latest agent run isn't a "
                    f"*_FAILED (role={latest.agent_role!r}, token={tok!r}); "
                    "retry is only supported for explicit failures"
                ),
            )

    new_attempt = latest.attempt + 1
    role = latest.agent_role
    _schedule_coro(retry_last_agent(bug_id), name=f"retry:{bug_id}")
    return RetryResponse(
        status="retrying",
        bug_id=bug_id,
        agent_role=role,
        attempt=new_attempt,
    )


@router.post(
    "/bugs/{bug_id}/take-over",
    response_model=TakeOverResponse,
    tags=["review"],
)
def take_over_bug_endpoint(
    bug_id: int,
    session: Session = Depends(get_session),
) -> TakeOverResponse:
    """Mark the bug as taken-over manually.

    Transitions to BLOCKED (so the Notion status flips to "Blocked" via
    the hook) and appends a ``## Taken Over Manually`` marker section to
    TRIAGE.md. Does NOT clean up the worktree — Megha uses it out of
    band.

    Allowed from any non-terminal state.
    """
    bug = repo.get_bug(session, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} not found")
    current = BugState(bug.state)
    if current in TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"Bug {bug_id} is already terminal ({current.value})",
        )
    if current == BugState.BLOCKED:
        # Idempotent — still append the marker so the reason is visible.
        _append_take_over_marker(bug)
        return TakeOverResponse(status="blocked", bug_id=bug_id)
    try:
        repo.update_bug_state(session, bug, BugState.BLOCKED)
    except IllegalBugStateTransition as exc:
        # Some states (e.g. MERGING) only allow BLOCKED as an outgoing
        # edge; if we somehow can't reach BLOCKED, surface the reason.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    _append_take_over_marker(bug)
    return TakeOverResponse(status="blocked", bug_id=bug_id)


def _append_take_over_marker(bug: Bug) -> None:
    """Append a ``## Taken Over Manually`` section to TRIAGE.md."""
    try:
        project = get_project(bug.project_id)
    except KeyError:
        project = None
    triage_md_path = _merge_triage_md_path(bug.id, project)
    body = (
        "The reviewer has chosen to take this bug over manually. The "
        "bug is now BLOCKED; the worktree is retained (not cleaned up) so "
        "it can be used out-of-band. When the work is done, close the PR "
        "manually or mark the bug as Cancelled / Fixed via the usual "
        "channels."
    )
    try:
        triage_parser.append(triage_md_path, "Taken Over Manually", body)
    except OSError:
        logger.exception(
            "Could not append Taken Over Manually section to %s", triage_md_path
        )
