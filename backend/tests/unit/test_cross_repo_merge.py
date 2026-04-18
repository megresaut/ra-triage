"""Cross-repo merge tests (Agent 10).

Drives ``orchestrator.merge.merge_bug`` against a bug whose
``affected_repos`` is both AVM repos. Verifies:

  - Happy path: backend PR created → frontend PR created → both bodies
    edited with cross-refs → backend merged → frontend merged → FIXED;
    append_pr_link called once per repo.
  - Frontend-merge failure (cross-repo partial merge): backend merged,
    frontend merge fails → ``## Partial Merge`` appended → BLOCKED.
    Backend PR is NOT auto-reverted; its DB row stays merged.
  - Cross-ref edit failure between create and merge: BLOCKED via plain
    Merge Failure (no PRs merged yet).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ra_triage.config.registry import RepoConfig
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.db.models import Bug
from ra_triage.integrations import github as github_module
from ra_triage.integrations.github import GitHubError, PRInfo
from ra_triage.orchestrator import merge as merge_module
from ra_triage.orchestrator.states import BugState
from ra_triage.workspaces import worktree as wt_module

# --- helpers --------------------------------------------------------------


def _make_cross_repo_rfr_bug(
    session: Any,
    *,
    notion_page_id: str,
) -> Bug:
    """Create a bug and walk it to READY_FOR_REVIEW with cross-repo scope."""
    bug = repo_module.create_bug(
        session,
        notion_page_id=notion_page_id,
        project_id="ra_avm",
        title="Cross-repo bug",
        description="d",
    )
    bug.affected_repos = json.dumps(["avm-backend", "builder-frontend"])
    bug.scope = "cross-repo"
    session.flush()
    for nxt in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.DESIGNING,
        BugState.FIXING_PARALLEL,
        BugState.INTEGRATION_TESTING,
        BugState.READY_FOR_REVIEW,
    ):
        repo_module.update_bug_state(session, bug, nxt)
    session.commit()
    return bug


def _seed_worktrees(
    triage_home: Path, bug_id: int, repo_names: list[str]
) -> dict[str, Path]:
    """Create dummy worktree dirs + TRIAGE.md so merge driver can resolve paths."""
    out: dict[str, Path] = {}
    for name in repo_names:
        worktree = triage_home / "worktrees" / str(bug_id) / name
        worktree.mkdir(parents=True, exist_ok=True)
        # Only the backend gets a real TRIAGE.md per the canonical layout.
        if "backend" in name:
            (worktree / "TRIAGE.md").write_text(
                "## Root Cause\n\nfile:1\n", encoding="utf-8"
            )
        out[name] = worktree
    return out


# --- happy path -----------------------------------------------------------


async def test_cross_repo_merge_happy_path(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend PR + frontend PR → cross-ref edit pass → backend merged → frontend merged → FIXED."""
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = _make_cross_repo_rfr_bug(session, notion_page_id="cross-merge-1")
    bug_id = bug.id
    session.close()
    _seed_worktrees(triage_home, bug_id, ["avm-backend", "builder-frontend"])

    create_calls: list[dict[str, Any]] = []
    edit_calls: list[dict[str, Any]] = []
    merge_calls: list[tuple[Path, int]] = []

    pr_url_by_repo = {
        "avm-backend": "https://github.com/megresaut/avm-backend/pull/101",
        "builder-frontend": "https://github.com/megresaut/builder_frontend/pull/202",
    }
    pr_number_by_repo = {"avm-backend": 101, "builder-frontend": 202}

    async def _fake_create(
        *, worktree: Path, repo: RepoConfig, bug_id: int, title: str,
        body_path: Path, cross_ref_body_note: str | None = None,
        cross_ref_url: str | None = None,
    ) -> PRInfo:
        create_calls.append(
            {
                "repo": repo.name,
                "title": title,
                "cross_ref_body_note": cross_ref_body_note,
                "cross_ref_url": cross_ref_url,
            }
        )
        return PRInfo(
            repo_name=repo.name,
            branch=f"triage/{bug_id}",
            number=pr_number_by_repo[repo.name],
            url=pr_url_by_repo[repo.name],
            status="open",
        )

    async def _fake_edit(
        *, worktree: Path, pr_number: int, body_path: Path,
        cross_ref_url: str | None = None,
    ) -> None:
        edit_calls.append(
            {
                "pr_number": pr_number,
                "cross_ref_url": cross_ref_url,
            }
        )

    async def _fake_merge(
        *, worktree: Path, pr_number: int,
        squash: bool = True, delete_branch: bool = True,
    ) -> None:
        merge_calls.append((worktree, pr_number))

    monkeypatch.setattr(github_module, "gh_pr_create", _fake_create)
    monkeypatch.setattr(github_module, "gh_pr_edit_body", _fake_edit)
    monkeypatch.setattr(github_module, "gh_pr_merge", _fake_merge)

    link_calls: list[tuple[str, str, str]] = []

    def _fake_append_pr_link(
        page_id: str, pr_url: str, repo_name: str, note: str | None = None
    ) -> None:
        link_calls.append((page_id, pr_url, repo_name))

    monkeypatch.setattr(
        merge_module.notion_module, "append_pr_link", _fake_append_pr_link
    )

    cleanup_calls: list[int] = []

    async def _fake_cleanup(bug_id: int, repos: list[RepoConfig]) -> None:
        cleanup_calls.append(bug_id)

    monkeypatch.setattr(wt_module, "cleanup", _fake_cleanup)

    await merge_module.merge_bug(bug_id)

    # Final state: FIXED, both PRs merged.
    session = engine_module.SessionLocal()
    try:
        final = repo_module.get_bug(session, bug_id)
        assert final is not None
        assert final.state == BugState.FIXED.value
        assert final.terminated_at is not None
        prs = repo_module.list_pull_requests(session, bug_id)
        assert len(prs) == 2
        for pr in prs:
            assert pr.status == "merged"
            assert pr.pr_number in {101, 202}
    finally:
        session.close()

    # Create order: backend first, frontend second (registry order).
    assert [c["repo"] for c in create_calls] == [
        "avm-backend",
        "builder-frontend",
    ]
    # Both creates carry a placeholder cross_ref_body_note so the body
    # mentions the cross-ref concept before the edit pass runs.
    assert all(c["cross_ref_body_note"] for c in create_calls)
    assert all(c["cross_ref_url"] is None for c in create_calls)

    # Edit pass: each PR points at the OTHER repo's URL.
    assert len(edit_calls) == 2
    edit_by_pr = {e["pr_number"]: e["cross_ref_url"] for e in edit_calls}
    assert (
        edit_by_pr[101]
        == "https://github.com/megresaut/builder_frontend/pull/202"
    )
    assert (
        edit_by_pr[202]
        == "https://github.com/megresaut/avm-backend/pull/101"
    )

    # Merge order: backend first, frontend second.
    assert [m[1] for m in merge_calls] == [101, 202]

    # Notion link append fires once per PR.
    assert len(link_calls) == 2
    repos_linked = {entry[2] for entry in link_calls}
    assert repos_linked == {"avm-backend", "builder-frontend"}

    # Cleanup invoked once for the bug.
    assert cleanup_calls == [bug_id]


# --- partial merge: backend OK, frontend fails ----------------------------


async def test_cross_repo_merge_frontend_fails_after_backend_merges(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend merges; frontend merge fails → BLOCKED + ## Partial Merge.

    The backend PR's pull_requests row stays status='merged'. The
    frontend PR row stays status='open'. The TRIAGE.md ends with a
    ## Partial Merge section listing the backend URL + the failure.
    """
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = _make_cross_repo_rfr_bug(session, notion_page_id="cross-partial-1")
    bug_id = bug.id
    session.close()
    worktrees = _seed_worktrees(
        triage_home, bug_id, ["avm-backend", "builder-frontend"]
    )

    pr_number_by_repo = {"avm-backend": 301, "builder-frontend": 302}
    pr_url_by_repo = {
        "avm-backend": "https://github.com/megresaut/avm-backend/pull/301",
        "builder-frontend": "https://github.com/megresaut/builder_frontend/pull/302",
    }

    async def _fake_create(
        *, worktree: Path, repo: RepoConfig, bug_id: int, title: str,
        body_path: Path, cross_ref_body_note: str | None = None,
        cross_ref_url: str | None = None,
    ) -> PRInfo:
        return PRInfo(
            repo_name=repo.name,
            branch=f"triage/{bug_id}",
            number=pr_number_by_repo[repo.name],
            url=pr_url_by_repo[repo.name],
            status="open",
        )

    async def _fake_edit(
        *, worktree: Path, pr_number: int, body_path: Path,
        cross_ref_url: str | None = None,
    ) -> None:
        pass

    merge_call_count = {"n": 0}

    async def _fake_merge(
        *, worktree: Path, pr_number: int,
        squash: bool = True, delete_branch: bool = True,
    ) -> None:
        merge_call_count["n"] += 1
        if pr_number == 302:
            raise GitHubError(
                "required checks failing on builder-frontend"
            )
        # backend (301) succeeds.

    monkeypatch.setattr(github_module, "gh_pr_create", _fake_create)
    monkeypatch.setattr(github_module, "gh_pr_edit_body", _fake_edit)
    monkeypatch.setattr(github_module, "gh_pr_merge", _fake_merge)
    # No Notion writes on partial merge.
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
        prs = repo_module.list_pull_requests(session, bug_id)
        prs_by_repo = {p.repo_name: p for p in prs}
        # Backend PR was marked merged.
        assert prs_by_repo["avm-backend"].status == "merged"
        assert prs_by_repo["avm-backend"].pr_number == 301
        # Frontend PR is still open (created but not merged).
        assert prs_by_repo["builder-frontend"].status == "open"
        assert prs_by_repo["builder-frontend"].pr_number == 302
    finally:
        session.close()

    # Notion not called on partial merge (consistency: only call on FIXED).
    assert link_calls == []

    # TRIAGE.md has the ## Partial Merge section.
    from ra_triage.triage_md import parser as triage_parser

    sections = triage_parser.parse(
        worktrees["avm-backend"] / "TRIAGE.md"
    )
    assert "Partial Merge" in sections
    assert "301" in sections["Partial Merge"]  # backend PR num appears
    assert "required checks failing" in sections["Partial Merge"]


# --- cross-ref edit failure → plain Merge Failure (no merges yet) --------


async def test_cross_repo_merge_blocks_on_cross_ref_edit_failure(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the cross-ref edit pass fails, no PRs are merged → Merge Failure."""
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = _make_cross_repo_rfr_bug(session, notion_page_id="cross-edit-fail-1")
    bug_id = bug.id
    session.close()
    worktrees = _seed_worktrees(
        triage_home, bug_id, ["avm-backend", "builder-frontend"]
    )

    async def _fake_create(
        *, worktree: Path, repo: RepoConfig, bug_id: int, title: str,
        body_path: Path, cross_ref_body_note: str | None = None,
        cross_ref_url: str | None = None,
    ) -> PRInfo:
        num = 401 if "backend" in repo.name else 402
        return PRInfo(
            repo_name=repo.name,
            branch=f"triage/{bug_id}",
            number=num,
            url=f"https://github.com/x/{repo.name}/pull/{num}",
            status="open",
        )

    async def _fake_edit_fail(
        *, worktree: Path, pr_number: int, body_path: Path,
        cross_ref_url: str | None = None,
    ) -> None:
        raise GitHubError("rate limited on edit")

    async def _fake_merge_should_not_run(
        *, worktree: Path, pr_number: int,
        squash: bool = True, delete_branch: bool = True,
    ) -> None:
        raise AssertionError(
            f"gh_pr_merge must not run on edit-failure path (got #{pr_number})"
        )

    monkeypatch.setattr(github_module, "gh_pr_create", _fake_create)
    monkeypatch.setattr(github_module, "gh_pr_edit_body", _fake_edit_fail)
    monkeypatch.setattr(github_module, "gh_pr_merge", _fake_merge_should_not_run)
    monkeypatch.setattr(
        merge_module.notion_module, "append_pr_link", lambda **kw: None
    )

    await merge_module.merge_bug(bug_id)

    session = engine_module.SessionLocal()
    try:
        final = repo_module.get_bug(session, bug_id)
        assert final is not None
        assert final.state == BugState.BLOCKED.value
        prs = repo_module.list_pull_requests(session, bug_id)
        # Both PRs were created but neither was merged.
        for pr in prs:
            assert pr.status == "open"
    finally:
        session.close()

    from ra_triage.triage_md import parser as triage_parser

    sections = triage_parser.parse(worktrees["avm-backend"] / "TRIAGE.md")
    # Plain Merge Failure (NOT Partial Merge — nothing was merged yet).
    assert "Merge Failure" in sections
    assert "Partial Merge" not in sections


# --- single-repo regression check: cross-ref pass should NOT run ----------


async def test_single_repo_merge_skips_cross_ref_edit_pass(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-repo bugs go through the same code path but skip the edit pass.

    Belt-and-suspenders against a regression where the new cross-ref
    pass might fire on single-repo merges and break the existing
    Agent 8 behavior.
    """
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = repo_module.create_bug(
        session,
        notion_page_id="single-cross-check-1",
        project_id="ra_avm",
        title="Single",
        description="d",
    )
    bug.affected_repos = json.dumps(["avm-backend"])
    bug.scope = "single-repo"
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
    bug_id = bug.id
    session.close()
    _seed_worktrees(triage_home, bug_id, ["avm-backend"])

    async def _fake_create(
        *, worktree: Path, repo: RepoConfig, bug_id: int, title: str,
        body_path: Path, cross_ref_body_note: str | None = None,
        cross_ref_url: str | None = None,
    ) -> PRInfo:
        return PRInfo(
            repo_name=repo.name,
            branch=f"triage/{bug_id}",
            number=501,
            url="https://github.com/x/avm-backend/pull/501",
            status="open",
        )

    edit_calls: list[Any] = []

    async def _fake_edit(**kw: Any) -> None:
        edit_calls.append(kw)

    async def _fake_merge(
        *, worktree: Path, pr_number: int,
        squash: bool = True, delete_branch: bool = True,
    ) -> None:
        pass

    monkeypatch.setattr(github_module, "gh_pr_create", _fake_create)
    monkeypatch.setattr(github_module, "gh_pr_edit_body", _fake_edit)
    monkeypatch.setattr(github_module, "gh_pr_merge", _fake_merge)
    monkeypatch.setattr(
        merge_module.notion_module, "append_pr_link", lambda **kw: None
    )

    async def _noop(bug_id: int, repos: list[RepoConfig]) -> None:
        return None

    monkeypatch.setattr(wt_module, "cleanup", _noop)

    await merge_module.merge_bug(bug_id)

    session = engine_module.SessionLocal()
    try:
        final = repo_module.get_bug(session, bug_id)
        assert final is not None
        assert final.state == BugState.FIXED.value
    finally:
        session.close()

    # Cross-ref edit pass MUST NOT run on single-repo merges.
    assert edit_calls == []
