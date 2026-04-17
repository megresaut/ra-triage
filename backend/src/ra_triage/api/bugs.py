"""REST endpoints for bug lifecycle.

Agent 2 surfaces:
  - GET /bugs
  - GET /bugs/{bug_id}
  - GET /bugs/{bug_id}/agent-runs
  - POST /poller/enqueue  (manual-bypass v1 debugging endpoint — Agent 3 will
    add the real Notion-driven poller.)

Pydantic response models are the shape `openapi-typescript` converts into the
frontend's `types.ts`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ra_triage.db import engine as engine_module
from ra_triage.db import repo
from ra_triage.db.models import AgentRun, Bug
from ra_triage.orchestrator.states import BugState

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


@router.post("/poller/enqueue", response_model=BugResponse)
def poller_enqueue(
    body: PollerEnqueueRequest,
    session: Session = Depends(get_session),
) -> BugResponse:
    """Manually enqueue a bug by `notion_page_id`.

    v1 debugging endpoint — Agent 3 will add the real Notion-driven polling
    that populates title/description/etc. from the Notion page. Until then we
    stub those fields so the pipeline can be exercised end-to-end.

    Idempotent: repeated calls with the same `notion_page_id` return the
    existing bug row unchanged.
    """
    title = body.title or f"Bug {body.notion_page_id}"
    description = body.description or "(manually enqueued, no Notion fetch)"
    bug = repo.create_bug(
        session,
        notion_page_id=body.notion_page_id,
        project_id=body.project_id,
        title=title,
        description=description,
    )
    current_test = repo.get_current_test_instance(session)
    launched_bug_id = current_test.bug_id if current_test else None
    return _bug_to_response(bug, launched_bug_id=launched_bug_id)
