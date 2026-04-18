"""Unit test: merging a bug schedules a rebase sweep per affected repo.

We drive :func:`orchestrator.merge.merge_bug` through its happy path with
stubbed ``gh`` CLI + Notion integrations and a spy for
:func:`workspaces.rebase.rebase_in_flight`. On MERGING → FIXED, the
merge driver MUST schedule one ``rebase_in_flight`` task per merged
repo with the right ``trigger_bug_id`` / ``repo_name`` / ``project``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ra_triage.config.registry import ProjectConfig, RepoConfig
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.db.models import Bug
from ra_triage.integrations import github as github_module
from ra_triage.integrations import notion as notion_module
from ra_triage.integrations.github import PRInfo
from ra_triage.orchestrator import merge as merge_module
from ra_triage.orchestrator.states import BugState
from ra_triage.workspaces import rebase as rebase_module
from ra_triage.workspaces import worktree as wt_module


def _make_rfr_bug(
    session,
    *,
    notion_page_id: str = "page-sweep",
    affected: list[str] | None = None,
) -> Bug:
    """Build a bug and walk it to READY_FOR_REVIEW with the given affected repos."""
    bug = repo_module.create_bug(
        session,
        notion_page_id=notion_page_id,
        project_id="ra_avm",
        title="Sweep trigger",
        description="d",
    )
    if affected is None:
        affected = ["avm-backend"]
    bug.affected_repos = json.dumps(affected)
    bug.scope = "single-repo" if len(affected) == 1 else "cross-repo"
    session.flush()
    for nxt in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.FIXING,
        BugState.TESTING,
        BugState.READY_FOR_REVIEW,
    ):
        repo_module.update_bug_state(session, bug, nxt)
    session.commit()
    return bug


def _seed_worktree(triage_home: Path, bug_id: int, repo_name: str) -> Path:
    worktree = triage_home / "worktrees" / str(bug_id) / repo_name
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "TRIAGE.md").write_text(
        "## Root Cause\n\nfile:1\n", encoding="utf-8"
    )
    return worktree


async def test_merge_single_repo_schedules_rebase_sweep(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-repo merge → one sweeper task scheduled for that repo."""
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = _make_rfr_bug(session, notion_page_id="page-sweep-single")
    bug_id = bug.id
    session.close()

    _seed_worktree(triage_home, bug_id, "avm-backend")

    # Stubs for gh + Notion + cleanup.
    async def _fake_create(
        *, worktree: Path, repo: RepoConfig, bug_id: int, title: str,
        body_path: Path, cross_ref_body_note: str | None = None,
    ) -> PRInfo:
        return PRInfo(
            repo_name=repo.name,
            branch=f"triage/{bug_id}",
            number=101,
            url=f"https://example/{repo.name}/pull/101",
            status="open",
        )

    async def _fake_merge(
        *, worktree: Path, pr_number: int,
        squash: bool = True, delete_branch: bool = True,
    ) -> None:
        return None

    monkeypatch.setattr(github_module, "gh_pr_create", _fake_create)
    monkeypatch.setattr(github_module, "gh_pr_merge", _fake_merge)
    monkeypatch.setattr(
        merge_module.notion_module,
        "append_pr_link",
        lambda page_id, pr_url, repo_name, note=None: None,
    )

    async def _fake_cleanup(bug_id: int, repos: list[RepoConfig]) -> None:
        return None

    monkeypatch.setattr(wt_module, "cleanup", _fake_cleanup)

    # Spy on rebase_in_flight.
    scheduled_calls: list[dict[str, Any]] = []

    async def _fake_rebase_in_flight(
        *,
        trigger_bug_id: int,
        repo_name: str,
        project: ProjectConfig,
    ) -> list[rebase_module.RebaseResult]:
        scheduled_calls.append(
            {
                "trigger_bug_id": trigger_bug_id,
                "repo_name": repo_name,
                "project_id": project.id,
            }
        )
        return []

    monkeypatch.setattr(
        rebase_module, "rebase_in_flight", _fake_rebase_in_flight
    )

    await merge_module.merge_bug(bug_id)
    # Give the fire-and-forget task a chance to run.
    await asyncio.sleep(0.05)

    assert len(scheduled_calls) == 1
    call = scheduled_calls[0]
    assert call["trigger_bug_id"] == bug_id
    assert call["repo_name"] == "avm-backend"
    assert call["project_id"] == "ra_avm"


async def test_merge_cross_repo_schedules_one_sweep_per_repo(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-repo merge → one sweeper task per affected repo."""
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = _make_rfr_bug(
        session,
        notion_page_id="page-sweep-cross",
        affected=["avm-backend", "builder-frontend"],
    )
    bug_id = bug.id
    session.close()

    _seed_worktree(triage_home, bug_id, "avm-backend")
    _seed_worktree(triage_home, bug_id, "builder-frontend")

    pr_numbers = iter([201, 202])

    async def _fake_create(
        *, worktree: Path, repo: RepoConfig, bug_id: int, title: str,
        body_path: Path, cross_ref_body_note: str | None = None,
    ) -> PRInfo:
        num = next(pr_numbers)
        return PRInfo(
            repo_name=repo.name,
            branch=f"triage/{bug_id}",
            number=num,
            url=f"https://example/{repo.name}/pull/{num}",
            status="open",
        )

    async def _fake_merge(
        *, worktree: Path, pr_number: int,
        squash: bool = True, delete_branch: bool = True,
    ) -> None:
        return None

    async def _fake_edit_body(
        *, worktree: Path, pr_number: int, body_path: Path,
        cross_ref_url: str,
    ) -> None:
        return None

    monkeypatch.setattr(github_module, "gh_pr_create", _fake_create)
    monkeypatch.setattr(github_module, "gh_pr_merge", _fake_merge)
    monkeypatch.setattr(github_module, "gh_pr_edit_body", _fake_edit_body)
    monkeypatch.setattr(
        merge_module.notion_module,
        "append_pr_link",
        lambda page_id, pr_url, repo_name, note=None: None,
    )
    monkeypatch.setattr(wt_module, "cleanup", _fake_cleanup := _noop_cleanup)

    scheduled_calls: list[dict[str, Any]] = []

    async def _fake_rebase_in_flight(
        *,
        trigger_bug_id: int,
        repo_name: str,
        project: ProjectConfig,
    ) -> list[rebase_module.RebaseResult]:
        scheduled_calls.append(
            {"trigger_bug_id": trigger_bug_id, "repo_name": repo_name}
        )
        return []

    monkeypatch.setattr(
        rebase_module, "rebase_in_flight", _fake_rebase_in_flight
    )

    await merge_module.merge_bug(bug_id)
    await asyncio.sleep(0.05)

    scheduled_repos = sorted(c["repo_name"] for c in scheduled_calls)
    assert scheduled_repos == sorted(["avm-backend", "builder-frontend"])
    for c in scheduled_calls:
        assert c["trigger_bug_id"] == bug_id


async def _noop_cleanup(bug_id: int, repos: list[RepoConfig]) -> None:
    return None


async def test_merge_failure_does_not_schedule_sweep(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A merge that ends in BLOCKED (failure) must NOT schedule a sweep."""
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = _make_rfr_bug(session, notion_page_id="page-sweep-fail")
    bug_id = bug.id
    session.close()

    _seed_worktree(triage_home, bug_id, "avm-backend")

    async def _fake_create(
        *, worktree: Path, repo: RepoConfig, bug_id: int, title: str,
        body_path: Path, cross_ref_body_note: str | None = None,
    ) -> PRInfo:
        return PRInfo(
            repo_name=repo.name,
            branch=f"triage/{bug_id}",
            number=301,
            url="https://example/pull/301",
            status="open",
        )

    async def _fake_merge_fail(
        *, worktree: Path, pr_number: int,
        squash: bool = True, delete_branch: bool = True,
    ) -> None:
        raise github_module.GitHubError("required checks failing")

    monkeypatch.setattr(github_module, "gh_pr_create", _fake_create)
    monkeypatch.setattr(github_module, "gh_pr_merge", _fake_merge_fail)
    monkeypatch.setattr(
        merge_module.notion_module,
        "append_pr_link",
        lambda page_id, pr_url, repo_name, note=None: None,
    )
    monkeypatch.setattr(wt_module, "cleanup", _noop_cleanup)

    scheduled_calls: list[dict[str, Any]] = []

    async def _fake_rebase_in_flight(**kwargs: Any):
        scheduled_calls.append(kwargs)
        return []

    monkeypatch.setattr(
        rebase_module, "rebase_in_flight", _fake_rebase_in_flight
    )

    await merge_module.merge_bug(bug_id)
    await asyncio.sleep(0.05)

    # Bug should be BLOCKED, and NO sweep should have been scheduled.
    session = engine_module.SessionLocal()
    try:
        refreshed = repo_module.get_bug(session, bug_id)
        assert refreshed is not None
        assert refreshed.state == BugState.BLOCKED.value
    finally:
        session.close()

    assert scheduled_calls == []


# Silence imported-but-unused for the notion_module alias.
_ = notion_module
