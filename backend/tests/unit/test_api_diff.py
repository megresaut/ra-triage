"""Tests for the Agent 6 ``GET /bugs/{id}/diff`` endpoints.

Strategy: build a real git-backed worktree inside TRIAGE_HOME,
insert a bug row whose repo matches the registry's ``avm-backend``
entry, then hit the endpoints via FastAPI's TestClient.

We don't touch the canonical clone in ``config.registry`` (that points
at Megha's real AVM checkout). Instead we build a shadow canonical
repo in the test tmp and use ``git worktree add`` to create a bug
worktree inside ``<TRIAGE_HOME>/worktrees/<bug_id>/<repo_name>/``.
The endpoint uses ``workspaces.worktree.path_for`` which IS rooted in
TRIAGE_HOME, so the worktree path resolution Just Works.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.main import create_app


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True
    )


def _build_worktree_with_diff(
    triage_home: Path, bug_id: int, repo_name: str = "avm-backend"
) -> Path:
    """Create a worktree with a non-empty diff vs origin/main.

    Uses a shadow canonical repo in ``<triage_home>/canonical`` so we
    don't touch the registry's real AVM clone. The worktree ends up at
    the standard ``<triage_home>/worktrees/<bug_id>/<repo_name>/`` path
    that the endpoint expects.
    """
    canonical = triage_home / "canonical"
    canonical.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q", "-b", "main"], cwd=canonical)
    _run_git(
        ["config", "user.email", "triage-test@example.com"], cwd=canonical
    )
    _run_git(["config", "user.name", "Triage Test"], cwd=canonical)

    (canonical / "src").mkdir(exist_ok=True)
    (canonical / "src" / "handler.go").write_text(
        "package main\nfunc Handler() {}\n", encoding="utf-8"
    )
    _run_git(["add", "."], cwd=canonical)
    _run_git(["commit", "-q", "-m", "baseline"], cwd=canonical)

    # Create a "remote" so ``origin/main`` exists for the diff call.
    bare = triage_home / "origin.git"
    _run_git(["init", "-q", "--bare", str(bare)], cwd=triage_home)
    _run_git(["remote", "add", "origin", str(bare)], cwd=canonical)
    _run_git(["push", "-q", "origin", "main"], cwd=canonical)

    # Bug worktree off origin/main, in the standard TRIAGE_HOME layout.
    dest = triage_home / "worktrees" / str(bug_id) / repo_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run_git(
        [
            "worktree",
            "add",
            "-q",
            str(dest),
            "-b",
            f"triage/{bug_id}",
            "origin/main",
        ],
        cwd=canonical,
    )

    # Make an edit so the diff is non-empty.
    (dest / "src" / "handler.go").write_text(
        "package main\nfunc Handler() { /* fixed */ }\n", encoding="utf-8"
    )
    _run_git(["add", "."], cwd=dest)
    _run_git(["commit", "-q", "-m", "fixer: guard nil"], cwd=dest)

    return dest


@pytest.fixture()
def bug_with_diff(alembic_db: Path, triage_home: Path) -> int:
    """Insert an ra_avm bug and build a worktree under TRIAGE_HOME with a diff."""
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.create_bug(
            session,
            notion_page_id="diff-1",
            project_id="ra_avm",
            title="Diff endpoint test",
            description="x",
        )
        session.commit()
        bug_id = bug.id
    finally:
        session.close()

    _build_worktree_with_diff(triage_home, bug_id)
    return bug_id


def test_get_diff_returns_non_empty_unified_diff(bug_with_diff: int) -> None:
    client = TestClient(create_app())
    response = client.get(
        f"/bugs/{bug_with_diff}/diff", params={"repo": "avm-backend"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["repo"] == "avm-backend"
    assert body["base_ref"] == "origin/main"
    assert body["head_ref"] == f"triage/{bug_with_diff}"
    diff = body["diff"]
    assert isinstance(diff, str) and diff.strip() != ""
    assert "src/handler.go" in diff
    # Unified-diff markers — sanity-check the shape.
    assert "---" in diff
    assert "+++" in diff


def test_get_diff_summary_parses_shortstat(bug_with_diff: int) -> None:
    client = TestClient(create_app())
    response = client.get(
        f"/bugs/{bug_with_diff}/diff/summary", params={"repo": "avm-backend"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["repo"] == "avm-backend"
    assert body["files_changed"] == 1
    assert body["insertions"] >= 1
    # The fix replaced one line, so we expect >= 1 insertion and >= 1 deletion.
    assert body["deletions"] >= 1


def test_get_diff_404_for_unknown_bug(alembic_db: Path) -> None:
    client = TestClient(create_app())
    response = client.get(
        "/bugs/99999/diff", params={"repo": "avm-backend"}
    )
    assert response.status_code == 404


def test_get_diff_404_for_unknown_repo(bug_with_diff: int) -> None:
    client = TestClient(create_app())
    response = client.get(
        f"/bugs/{bug_with_diff}/diff", params={"repo": "not-a-repo"}
    )
    assert response.status_code == 404


def test_get_diff_404_when_worktree_missing(alembic_db: Path) -> None:
    """Bug exists but no worktree on disk → 404 with clear detail."""
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.create_bug(
            session,
            notion_page_id="no-wt",
            project_id="ra_avm",
            title="no worktree",
            description="x",
        )
        session.commit()
        bug_id = bug.id
    finally:
        session.close()

    client = TestClient(create_app())
    response = client.get(
        f"/bugs/{bug_id}/diff", params={"repo": "avm-backend"}
    )
    assert response.status_code == 404
