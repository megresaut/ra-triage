"""Unit tests for ``orchestrator.merge.merge_bug``.

We stub out the gh-CLI layer and the Notion client so tests exercise only
the merge driver's state-transition + DB bookkeeping logic.

Happy path: RFR → MERGING → FIXED; pull_requests row flipped to merged;
``append_pr_link`` called once on the Notion page; worktree cleanup hit.

Failure paths: gh_pr_create error → BLOCKED + ``## Merge Failure`` section;
gh_pr_merge error → BLOCKED + ``## Merge Failure`` section. Reuse path:
existing open ``pull_requests`` row is reused (no second ``gh_pr_create``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from ra_triage.config.registry import RepoConfig
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.db.models import Bug
from ra_triage.integrations import github as github_module
from ra_triage.integrations import notion as notion_module
from ra_triage.integrations.github import GitHubError, PRInfo
from ra_triage.orchestrator import merge as merge_module
from ra_triage.orchestrator.states import BugState

# --- helpers ---------------------------------------------------------------


def _make_rfr_bug(
    session: Session,
    *,
    notion_page_id: str = "page-merge-1",
    affected: list[str] | None = None,
) -> Bug:
    """Create a bug and walk it to READY_FOR_REVIEW."""
    bug = repo_module.create_bug(
        session,
        notion_page_id=notion_page_id,
        project_id="ra_avm",
        title="Broken dropdown",
        description="d",
    )
    # Seed the affected_repos column before we walk the state machine — the
    # merge driver reads it to decide which repos to PR/merge.
    if affected is None:
        affected = ["avm-backend"]
    import json

    bug.affected_repos = json.dumps(affected)
    bug.scope = "single-repo"
    session.flush()
    # Walk to RFR.
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
    """Create a dummy worktree directory + TRIAGE.md so the driver can
    resolve body_path for gh_pr_create."""
    worktree = triage_home / "worktrees" / str(bug_id) / repo_name
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "TRIAGE.md").write_text(
        "## Root Cause\n\nfile:1\n", encoding="utf-8"
    )
    return worktree


# --- happy path ------------------------------------------------------------


async def test_merge_bug_single_repo_happy_path(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RFR → MERGING → FIXED with PR link + cleanup."""
    _ = alembic_db

    session = engine_module.SessionLocal()
    bug = _make_rfr_bug(session)
    bug_id = bug.id
    session.close()

    _seed_worktree(triage_home, bug_id, "avm-backend")

    # Stub gh_pr_create + gh_pr_merge.
    created_calls: list[tuple[Any, ...]] = []
    merged_calls: list[tuple[Any, ...]] = []

    async def _fake_create(
        *, worktree: Path, repo: RepoConfig, bug_id: int, title: str,
        body_path: Path, cross_ref_body_note: str | None = None,
    ) -> PRInfo:
        created_calls.append((worktree, repo.name, bug_id, title, str(body_path)))
        return PRInfo(
            repo_name=repo.name,
            branch=f"triage/{bug_id}",
            number=101,
            url="https://github.com/megresaut/avm-backend/pull/101",
            status="open",
        )

    async def _fake_merge(
        *, worktree: Path, pr_number: int,
        squash: bool = True, delete_branch: bool = True,
    ) -> None:
        merged_calls.append((worktree, pr_number, squash, delete_branch))

    monkeypatch.setattr(github_module, "gh_pr_create", _fake_create)
    monkeypatch.setattr(github_module, "gh_pr_merge", _fake_merge)

    # Stub append_pr_link + worktree cleanup.
    link_calls: list[tuple[str, str, str, str | None]] = []

    def _fake_append_pr_link(
        page_id: str, pr_url: str, repo_name: str, note: str | None = None
    ) -> None:
        link_calls.append((page_id, pr_url, repo_name, note))

    monkeypatch.setattr(notion_module, "append_pr_link", _fake_append_pr_link)
    # The merge module imports `notion` directly; patch the attribute it uses.
    monkeypatch.setattr(
        merge_module.notion_module, "append_pr_link", _fake_append_pr_link
    )

    cleanup_calls: list[tuple[int, list[str]]] = []

    async def _fake_cleanup(bug_id: int, repos: list[RepoConfig]) -> None:
        cleanup_calls.append((bug_id, [r.name for r in repos]))

    from ra_triage.workspaces import worktree as wt_module

    monkeypatch.setattr(wt_module, "cleanup", _fake_cleanup)

    # Run.
    await merge_module.merge_bug(bug_id)

    # Assertions.
    session = engine_module.SessionLocal()
    try:
        final = repo_module.get_bug(session, bug_id)
        assert final is not None
        assert final.state == BugState.FIXED.value
        assert final.terminated_at is not None
        prs = repo_module.list_pull_requests(session, bug_id)
        assert len(prs) == 1
        assert prs[0].pr_url == "https://github.com/megresaut/avm-backend/pull/101"
        assert prs[0].pr_number == 101
        assert prs[0].status == "merged"
        assert prs[0].merged_at is not None
    finally:
        session.close()

    assert len(created_calls) == 1
    assert created_calls[0][1] == "avm-backend"
    assert len(merged_calls) == 1
    assert merged_calls[0][1] == 101

    assert len(link_calls) == 1
    page_id, url, repo_name, note = link_calls[0]
    assert page_id == "page-merge-1"
    assert url == "https://github.com/megresaut/avm-backend/pull/101"
    assert repo_name == "avm-backend"
    assert note is None  # default "merged" suffix

    assert len(cleanup_calls) == 1
    assert cleanup_calls[0][0] == bug_id


# --- merge failure ---------------------------------------------------------


async def test_merge_bug_blocks_on_gh_merge_failure(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `gh pr merge` fails, bug lands in BLOCKED with Merge Failure section."""
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = _make_rfr_bug(session, notion_page_id="page-fail")
    bug_id = bug.id
    session.close()

    worktree = _seed_worktree(triage_home, bug_id, "avm-backend")

    async def _fake_create(
        *, worktree: Path, repo: RepoConfig, bug_id: int, title: str,
        body_path: Path, cross_ref_body_note: str | None = None,
    ) -> PRInfo:
        return PRInfo(
            repo_name=repo.name,
            branch=f"triage/{bug_id}",
            number=202,
            url="https://github.com/megresaut/avm-backend/pull/202",
            status="open",
        )

    async def _fake_merge_raise(
        *, worktree: Path, pr_number: int,
        squash: bool = True, delete_branch: bool = True,
    ) -> None:
        raise GitHubError("required checks are failing")

    monkeypatch.setattr(github_module, "gh_pr_create", _fake_create)
    monkeypatch.setattr(github_module, "gh_pr_merge", _fake_merge_raise)
    # Ensure append_pr_link is never called on failure.
    link_calls: list[Any] = []
    monkeypatch.setattr(
        merge_module.notion_module,
        "append_pr_link",
        lambda **kw: link_calls.append(kw),
    )

    await merge_module.merge_bug(bug_id)

    session = engine_module.SessionLocal()
    try:
        final = repo_module.get_bug(session, bug_id)
        assert final is not None
        assert final.state == BugState.BLOCKED.value
    finally:
        session.close()

    # TRIAGE.md should now have a Merge Failure section.
    from ra_triage.triage_md import parser as triage_parser

    sections = triage_parser.parse(worktree / "TRIAGE.md")
    assert "Merge Failure" in sections
    assert "required checks are failing" in sections["Merge Failure"]

    # No Notion PR-link written on failure.
    assert link_calls == []


async def test_merge_bug_blocks_on_gh_create_failure(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if PR creation fails, bug lands in BLOCKED cleanly."""
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = _make_rfr_bug(session, notion_page_id="page-create-fail")
    bug_id = bug.id
    session.close()

    _seed_worktree(triage_home, bug_id, "avm-backend")

    async def _fake_create_raise(
        *, worktree: Path, repo: RepoConfig, bug_id: int, title: str,
        body_path: Path, cross_ref_body_note: str | None = None,
    ) -> PRInfo:
        raise GitHubError("branch not pushed")

    async def _fake_merge_noop(
        *, worktree: Path, pr_number: int,
        squash: bool = True, delete_branch: bool = True,
    ) -> None:
        raise AssertionError("gh_pr_merge must not be called when create fails")

    monkeypatch.setattr(github_module, "gh_pr_create", _fake_create_raise)
    monkeypatch.setattr(github_module, "gh_pr_merge", _fake_merge_noop)

    await merge_module.merge_bug(bug_id)

    session = engine_module.SessionLocal()
    try:
        final = repo_module.get_bug(session, bug_id)
        assert final is not None
        assert final.state == BugState.BLOCKED.value
    finally:
        session.close()


# --- reuse existing open PR -------------------------------------------------


async def test_merge_bug_reuses_existing_open_pr(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If pull_requests already has an open row for (bug, repo), we DON'T
    call gh_pr_create a second time. We just re-invoke gh_pr_merge."""
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = _make_rfr_bug(session, notion_page_id="page-reuse")
    bug_id = bug.id
    repo_module.record_pr(
        session,
        bug_id=bug_id,
        repo_name="avm-backend",
        branch="triage/" + str(bug_id),
        pr_url="https://github.com/megresaut/avm-backend/pull/55",
        pr_number=55,
        status="open",
    )
    session.commit()
    session.close()

    _seed_worktree(triage_home, bug_id, "avm-backend")

    create_calls = []

    async def _fake_create(**kw: Any) -> PRInfo:
        create_calls.append(kw)
        raise AssertionError(
            "gh_pr_create must not be called when an open PR exists"
        )

    merge_calls: list[int] = []

    async def _fake_merge(
        *, worktree: Path, pr_number: int,
        squash: bool = True, delete_branch: bool = True,
    ) -> None:
        merge_calls.append(pr_number)

    monkeypatch.setattr(github_module, "gh_pr_create", _fake_create)
    monkeypatch.setattr(github_module, "gh_pr_merge", _fake_merge)
    monkeypatch.setattr(
        merge_module.notion_module, "append_pr_link", lambda **kw: None
    )

    from ra_triage.workspaces import worktree as wt_module

    async def _no_cleanup(bug_id: int, repos: list[RepoConfig]) -> None:
        return None

    monkeypatch.setattr(wt_module, "cleanup", _no_cleanup)

    await merge_module.merge_bug(bug_id)

    assert create_calls == []
    assert merge_calls == [55]

    session = engine_module.SessionLocal()
    try:
        final = repo_module.get_bug(session, bug_id)
        assert final is not None
        assert final.state == BugState.FIXED.value
        prs = repo_module.list_pull_requests(session, bug_id)
        assert len(prs) == 1
        assert prs[0].status == "merged"
    finally:
        session.close()


# --- cancel-aware -----------------------------------------------------------


async def test_merge_bug_aborts_when_state_not_rfr(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the bug is already terminal / not in RFR, merge_bug returns silently."""
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = repo_module.create_bug(
        session,
        notion_page_id="nope",
        project_id="ra_avm",
        title="t",
        description="d",
    )
    session.commit()
    bug_id = bug.id
    session.close()

    # Stubs that would explode if called.
    async def _fake_create(**kw: Any) -> PRInfo:
        raise AssertionError("should not be invoked on OPEN bug")

    monkeypatch.setattr(github_module, "gh_pr_create", _fake_create)

    await merge_module.merge_bug(bug_id)

    # State unchanged.
    session = engine_module.SessionLocal()
    try:
        final = repo_module.get_bug(session, bug_id)
        assert final is not None
        assert final.state == BugState.OPEN.value
    finally:
        session.close()


# --- unconfigured Notion doesn't break FIXED transition ---------------------


async def test_merge_bug_still_marks_fixed_when_notion_unconfigured(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """append_pr_link no-ops when Notion client is absent; merge still completes."""
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = _make_rfr_bug(session, notion_page_id="page-no-notion")
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
            number=303,
            url="https://github.com/megresaut/avm-backend/pull/303",
            status="open",
        )

    async def _fake_merge_ok(
        *, worktree: Path, pr_number: int,
        squash: bool = True, delete_branch: bool = True,
    ) -> None:
        pass

    monkeypatch.setattr(github_module, "gh_pr_create", _fake_create)
    monkeypatch.setattr(github_module, "gh_pr_merge", _fake_merge_ok)

    # Force the client builder to return None (unconfigured) — append_pr_link
    # should silently no-op.
    notion_module.set_client_builder(lambda: None)

    from ra_triage.workspaces import worktree as wt_module

    async def _noop(bug_id: int, repos: list[RepoConfig]) -> None:
        return None

    monkeypatch.setattr(wt_module, "cleanup", _noop)

    try:
        await merge_module.merge_bug(bug_id)
    finally:
        notion_module.set_client_builder(None)

    session = engine_module.SessionLocal()
    try:
        final = repo_module.get_bug(session, bug_id)
        assert final is not None
        assert final.state == BugState.FIXED.value
    finally:
        session.close()
