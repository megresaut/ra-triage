"""REST endpoints for project registry views.

Read-only in Agent 2; Agent 13 will add settings updates.

Exposes a *public-safe* subset of the registry — env var names, absolute
filesystem paths, and DB URLs are intentionally omitted from the payload. UI
only needs display info plus the per-repo ports / branch prefix / github
origin.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ra_triage.config.registry import PROJECTS, ProjectConfig, RepoConfig

router = APIRouter(tags=["projects"])


class RepoResponse(BaseModel):
    """Public view of a single repo in a project."""

    name: str
    role: str
    branch_prefix: str
    dev_port: int
    test_launch_port: int
    integration_port: int
    github_origin: str
    main_branch: str


class ProjectResponse(BaseModel):
    """Public view of a project in the registry."""

    id: str
    display_name: str
    auto_merge: bool
    repos: list[RepoResponse]
    deny_list_paths: list[str] = Field(default_factory=list)
    deny_list_categories: list[str] = Field(default_factory=list)


def _repo_to_response(repo: RepoConfig) -> RepoResponse:
    return RepoResponse(
        name=repo.name,
        role=repo.role,
        branch_prefix=repo.branch_prefix,
        dev_port=repo.dev_port,
        test_launch_port=repo.test_launch_port,
        integration_port=repo.integration_port,
        github_origin=repo.github_origin,
        main_branch=repo.main_branch,
    )


def _project_to_response(project: ProjectConfig) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        display_name=project.display_name,
        auto_merge=project.auto_merge,
        repos=[_repo_to_response(r) for r in project.repos],
        deny_list_paths=list(project.deny_list.paths),
        deny_list_categories=list(project.deny_list.categories),
    )


@router.get("/projects", response_model=list[ProjectResponse])
def list_projects() -> list[ProjectResponse]:
    """Return all configured projects in public-safe form."""
    return [_project_to_response(p) for p in PROJECTS.values()]


@router.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project_by_id(project_id: str) -> ProjectResponse:
    """Return one project by id, 404 if unknown."""
    project = PROJECTS.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Unknown project: {project_id}")
    return _project_to_response(project)
