"""REST endpoints for the v1 Settings page (Agent 13).

Surface:

  - ``GET /settings`` — registry view of every configured project, with
    both the effective ``auto_merge`` value (registry default plus any
    JSON override) and the registry default surfaced separately so the
    frontend can render an "(default: off)" hint.
  - ``PUT /settings/projects/{project_id}`` — update a project's
    overrides. Currently only ``auto_merge`` is mutable.

**Behavior contract.** The ``auto_merge`` toggle is persisted but is
explicitly a no-op in v1 — the product plan's "No auto-merge without
human confirmation" non-goal means ``orchestrator/merge.py`` does NOT
consult :func:`config.settings_store.effective_auto_merge`. The frontend
surfaces a tooltip explaining this. The setting persists across
restarts so v2 can wire it up without losing state.

Pydantic models intentionally mirror the same public-safe subset of
``RepoConfig`` exposed by ``api.projects`` (no filesystem paths, no DB
URLs, no env var names). The shape is duplicated rather than imported so
the OpenAPI schema groups cleanly under the ``settings`` tag and the
frontend doesn't have to follow type aliases across resource boundaries.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, StrictBool

from ra_triage.config import settings_store
from ra_triage.config.registry import PROJECTS, ProjectConfig, RepoConfig

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])


# --- Response shapes ------------------------------------------------------


class SettingsRepoView(BaseModel):
    """Public-safe view of a single repo, mirroring ``api.projects``."""

    name: str
    role: str
    branch_prefix: str
    main_branch: str
    dev_port: int
    test_launch_port: int
    integration_port: int
    github_origin: str


class SettingsDenyListView(BaseModel):
    """Read-only deny-list surface for the Settings UI."""

    paths: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)


class SettingsProjectView(BaseModel):
    """One project's view in the ``GET /settings`` payload.

    ``auto_merge`` is the *effective* value (override-or-default).
    ``auto_merge_default`` is the registry hardcoded value, surfaced so
    the frontend can show "(default: off)" when an override is in place.
    """

    id: str
    display_name: str
    auto_merge: bool
    auto_merge_default: bool
    repos: list[SettingsRepoView]
    deny_list: SettingsDenyListView


class SettingsResponse(BaseModel):
    """Top-level shape of ``GET /settings``."""

    projects: list[SettingsProjectView]


# --- Request shapes -------------------------------------------------------


class UpdateProjectSettingsRequest(BaseModel):
    """Body for ``PUT /settings/projects/{project_id}``.

    ``auto_merge`` is the only mutable field in v1. Made optional so a
    future v2 toggle can be added without forcing every caller to send
    every field. Typed as ``StrictBool`` to reject coerced values like
    ``"yes"`` or ``1`` — settings deserve unambiguous inputs.
    """

    auto_merge: StrictBool | None = Field(default=None)


class UpdateProjectSettingsResponse(BaseModel):
    """Echo of the now-stored override."""

    project_id: str
    auto_merge: bool
    auto_merge_default: bool


# --- Helpers --------------------------------------------------------------


def _repo_to_view(repo: RepoConfig) -> SettingsRepoView:
    return SettingsRepoView(
        name=repo.name,
        role=repo.role,
        branch_prefix=repo.branch_prefix,
        main_branch=repo.main_branch,
        dev_port=repo.dev_port,
        test_launch_port=repo.test_launch_port,
        integration_port=repo.integration_port,
        github_origin=repo.github_origin,
    )


def _project_to_view(project: ProjectConfig) -> SettingsProjectView:
    return SettingsProjectView(
        id=project.id,
        display_name=project.display_name,
        auto_merge=settings_store.effective_auto_merge(project.id),
        auto_merge_default=project.auto_merge,
        repos=[_repo_to_view(r) for r in project.repos],
        deny_list=SettingsDenyListView(
            paths=list(project.deny_list.paths),
            categories=list(project.deny_list.categories),
        ),
    )


# --- Endpoints ------------------------------------------------------------


@router.get("/settings", response_model=SettingsResponse)
def get_settings_endpoint() -> SettingsResponse:
    """Return the full settings surface for every configured project.

    Effective ``auto_merge`` includes any JSON override; ``auto_merge_default``
    is the registry's hardcoded value, surfaced so the UI can hint at
    drift between the two.
    """
    return SettingsResponse(
        projects=[_project_to_view(p) for p in PROJECTS.values()]
    )


@router.put(
    "/settings/projects/{project_id}",
    response_model=UpdateProjectSettingsResponse,
)
def update_project_settings(
    project_id: str, body: UpdateProjectSettingsRequest
) -> UpdateProjectSettingsResponse:
    """Update one project's overrides; returns the resulting state.

    - 404 if ``project_id`` is unknown to the registry (we refuse to
      persist overrides for non-existent projects).
    - 200 with the new effective ``auto_merge`` plus the unchanged
      registry default on success.

    Note: even when ``auto_merge`` is set to ``true`` here, the Confirm
    button in the UI still gates merging in v1 — the toggle is persisted
    for v2 only.
    """
    try:
        updated = settings_store.update_project(
            project_id, auto_merge=body.auto_merge
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"Unknown project: {project_id}"
        ) from exc

    return UpdateProjectSettingsResponse(
        project_id=updated.project_id,
        auto_merge=updated.auto_merge,
        auto_merge_default=settings_store.registry_auto_merge_default(project_id),
    )


__all__ = [
    "SettingsDenyListView",
    "SettingsProjectView",
    "SettingsRepoView",
    "SettingsResponse",
    "UpdateProjectSettingsRequest",
    "UpdateProjectSettingsResponse",
    "router",
]
