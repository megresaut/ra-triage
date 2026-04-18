"""Unit tests for ``ra_triage.workspaces.rebase``.

We use a fixture canonical clone with a bare ``origin`` and prepare two
parallel branches:

  - ``triage/<trigger_bug>``: modifies ``invoice.txt`` line 1 → "FROM TRIGGER"
    and gets fast-forwarded into ``main`` to simulate the merge that fires
    the sweep.
  - ``triage/<target_bug>``: also modifies ``invoice.txt`` line 1 → "FROM TARGET"
    on a worktree at ``<triage_home>/worktrees/<target_bug>/<repo_name>/``.

Running ``rebase_in_flight(trigger_bug, repo_name)`` rebases the target's
branch onto the new ``origin/main``. The conflict on ``invoice.txt`` is
unavoidable, so the result is ``"conflict"``, the rebase is aborted, the
target bug flips to BLOCKED, ``bugs.conflict_files`` is populated, and a
``rebase_attempts`` row records the conflict.

For the clean case we modify a DIFFERENT file in the trigger branch so
the rebase has nothing to merge.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from ra_triage.config.registry import ProjectConfig, RepoConfig
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.orchestrator.states import BugState
from ra_triage.workspaces import rebase as rebase_module
from ra_triage.workspaces import worktree as wt_module

# --- fixture helpers ----------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    """Run ``git <args>`` in ``repo`` synchronously with deterministic config."""
    subprocess.check_call(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            *args,
        ]
    )


def _make_repo_config(
    canonical: Path, name: str = "demo"
) -> RepoConfig:
    return RepoConfig(
        name=name,
        path=canonical,
        main_branch="main",
        branch_prefix="triage/",
        dev_command="true",
        build_command="true",
        test_command="true",
        dev_port=1,
        test_launch_port=2,
        integration_port=3,
        env_file=".env",
        role="backend",
        github_origin="github.com/test/demo",
    )


def _make_project(repo_cfg: RepoConfig) -> ProjectConfig:
    from ra_triage.config.registry import DenyList

    return ProjectConfig(
        id="ra_avm",
        display_name="Test Project",
        repos=[repo_cfg],
        deny_list=DenyList(paths=[], categories=[]),
        slack_webhook_env="UNUSED",
        auto_merge=False,
    )


@pytest.fixture()
def canonical_repo(tmp_path: Path) -> Iterator[Path]:
    """Initialize a canonical clone with a single commit on ``main``.

    Adds a ``invoice.txt`` file with a known initial line so the rebase
    tests can produce a deterministic conflict.
    """
    canonical = tmp_path / "canonical"
    subprocess.check_call(["git", "init", "-b", "main", str(canonical)])
    (canonical / "invoice.txt").write_text("ORIGINAL\n", encoding="utf-8")
    (canonical / "other.txt").write_text("untouched\n", encoding="utf-8")
    _git(canonical, "add", "-A")
    _git(canonical, "commit", "-m", "init")

    bare = tmp_path / "origin.git"
    subprocess.check_call(
        ["git", "clone", "--bare", str(canonical), str(bare)]
    )
    _git(canonical, "remote", "add", "origin", str(bare))
    _git(canonical, "fetch", "origin")
    yield canonical


def _make_target_bug(
    session,
    *,
    notion_page_id: str,
    affected_repos: list[str],
):
    """Create a bug, walk to FIXING via legal transitions, set affected_repos."""
    bug = repo_module.create_bug(
        session,
        notion_page_id=notion_page_id,
        project_id="ra_avm",
        title="target",
        description="d",
    )
    bug.affected_repos = json.dumps(affected_repos)
    session.flush()
    for step in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.FIXING,
    ):
        repo_module.update_bug_state(session, bug, step)
    session.commit()
    return bug


def _make_trigger_bug(session, *, notion_page_id: str):
    """A bug to use as the rebase trigger; state doesn't matter for the sweep."""
    bug = repo_module.create_bug(
        session,
        notion_page_id=notion_page_id,
        project_id="ra_avm",
        title="trigger",
        description="d",
    )
    session.commit()
    return bug


def _seed_target_worktree(
    *,
    canonical: Path,
    triage_home: Path,
    bug_id: int,
    repo_name: str,
    invoice_line: str | None,
) -> Path:
    """Create the target bug's worktree; optionally edit ``invoice.txt``.

    If ``invoice_line`` is provided, rewrite ``invoice.txt`` to that
    content and commit. Callers pass a non-None line when they want a
    deterministic conflict against a trigger that touches the same
    file; they pass ``None`` when they'll drop a different file in
    post-call.
    """
    worktree_path = triage_home / "worktrees" / str(bug_id) / repo_name
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"triage/{bug_id}"
    subprocess.check_call(
        [
            "git",
            "-C",
            str(canonical),
            "worktree",
            "add",
            str(worktree_path),
            "-b",
            branch,
            "origin/main",
        ]
    )
    if invoice_line is not None:
        (worktree_path / "invoice.txt").write_text(
            invoice_line, encoding="utf-8"
        )
        _git(worktree_path, "add", "-A")
        _git(
            worktree_path,
            "commit",
            "-m",
            f"target bug {bug_id} edits invoice",
        )
    return worktree_path


def _push_main_change(
    canonical: Path,
    *,
    file_path: str,
    new_content: str,
) -> None:
    """Modify ``file_path`` on main, commit, and push to ``origin``.

    Simulates the trigger bug's PR merging into main. After this the
    target bug's worktree's ``git fetch origin main`` will see a new
    head; ``git rebase origin/main`` will either no-op (different file)
    or conflict (same file).
    """
    (canonical / file_path).write_text(new_content, encoding="utf-8")
    _git(canonical, "add", "-A")
    _git(canonical, "commit", "-m", f"trigger merged: edits {file_path}")
    _git(canonical, "push", "origin", "main")


# --- tests ---------------------------------------------------------------


async def test_rebase_in_flight_clean_case_no_state_change(
    canonical_repo: Path,
    triage_home: Path,
    alembic_db: Path,
) -> None:
    """Trigger touches a different file → target rebases cleanly, stays in FIXING."""
    _ = alembic_db
    repo_cfg = _make_repo_config(canonical_repo, name="demo")
    project = _make_project(repo_cfg)

    session = engine_module.SessionLocal()
    try:
        trigger = _make_trigger_bug(session, notion_page_id="p-trigger-clean")
        target = _make_target_bug(
            session,
            notion_page_id="p-target-clean",
            affected_repos=["demo"],
        )
        target_id = target.id
        trigger_id = trigger.id
    finally:
        session.close()

    _seed_target_worktree(
        canonical=canonical_repo,
        triage_home=triage_home,
        bug_id=target_id,
        repo_name="demo",
        invoice_line=None,
    )
    # Target writes a different file — clean rebase against main that
    # touches yet a third file.
    target_worktree = wt_module.path_for(target_id, "demo")
    (target_worktree / "target_only.txt").write_text(
        "target work\n", encoding="utf-8"
    )
    _git(target_worktree, "add", "-A")
    _git(target_worktree, "commit", "-m", "target adds its own file")

    # Trigger merges first: edit a DIFFERENT file on main and push.
    _push_main_change(
        canonical_repo,
        file_path="other.txt",
        new_content="trigger touched this\n",
    )

    results = await rebase_module.rebase_in_flight(
        trigger_bug_id=trigger_id,
        repo_name="demo",
        project=project,
    )

    assert len(results) == 1
    assert results[0].result == "clean"
    assert results[0].conflict_files == []
    assert results[0].bug_id == target_id

    # Bug stays in FIXING (no state change).
    session = engine_module.SessionLocal()
    try:
        refreshed = repo_module.get_bug(session, target_id)
        assert refreshed is not None
        assert refreshed.state == BugState.FIXING.value
        assert refreshed.conflict_files is None
        attempts = repo_module.list_rebase_attempts(session, target_id)
        assert len(attempts) == 1
        assert attempts[0].result == "clean"
    finally:
        session.close()


async def test_rebase_in_flight_conflict_blocks_target_with_files(
    canonical_repo: Path,
    triage_home: Path,
    alembic_db: Path,
) -> None:
    """Trigger + target edit the same file → BLOCKED with conflict_files set."""
    _ = alembic_db
    repo_cfg = _make_repo_config(canonical_repo, name="demo")
    project = _make_project(repo_cfg)

    session = engine_module.SessionLocal()
    try:
        trigger = _make_trigger_bug(
            session, notion_page_id="p-trigger-conflict"
        )
        target = _make_target_bug(
            session,
            notion_page_id="p-target-conflict",
            affected_repos=["demo"],
        )
        target_id = target.id
        trigger_id = trigger.id
    finally:
        session.close()

    _seed_target_worktree(
        canonical=canonical_repo,
        triage_home=triage_home,
        bug_id=target_id,
        repo_name="demo",
        invoice_line="FROM TARGET\n",
    )

    # Trigger merges first: rewrite invoice.txt to a conflicting line.
    _push_main_change(
        canonical_repo,
        file_path="invoice.txt",
        new_content="FROM TRIGGER\n",
    )

    results = await rebase_module.rebase_in_flight(
        trigger_bug_id=trigger_id,
        repo_name="demo",
        project=project,
    )

    assert len(results) == 1
    res = results[0]
    assert res.result == "conflict"
    assert "invoice.txt" in res.conflict_files

    # The bug is now BLOCKED with conflict_files populated.
    session = engine_module.SessionLocal()
    try:
        refreshed = repo_module.get_bug(session, target_id)
        assert refreshed is not None
        assert refreshed.state == BugState.BLOCKED.value
        files = repo_module.parse_conflict_files(refreshed.conflict_files)
        assert files is not None
        assert "invoice.txt" in files

        attempts = repo_module.list_rebase_attempts(session, target_id)
        assert len(attempts) == 1
        assert attempts[0].result == "conflict"
        attempt_files = json.loads(attempts[0].conflict_files or "[]")
        assert "invoice.txt" in attempt_files
    finally:
        session.close()

    # The worktree's tracked files are clean (the rebase --abort succeeded).
    # We deliberately allow TRIAGE.md to show as untracked — the sweeper
    # writes a Merge Conflict section there, which is a new file the
    # original commits never tracked.
    target_worktree = wt_module.path_for(target_id, "demo")
    status = subprocess.check_output(
        ["git", "-C", str(target_worktree), "status", "--porcelain"]
    ).decode()
    # No conflict markers (UU lines) should remain.
    for line in status.splitlines():
        assert "U" not in (line[:2] if len(line) >= 2 else ""), (
            f"rebase --abort should have cleared conflicts; got:\n{status}"
        )

    # TRIAGE.md got a Merge Conflict section appended.
    triage_md = target_worktree / "TRIAGE.md"
    assert triage_md.exists()
    body = triage_md.read_text(encoding="utf-8")
    assert "## Merge Conflict" in body
    assert "invoice.txt" in body
    assert f"Bug #{trigger_id}" in body


async def test_rebase_in_flight_skips_missing_worktree_records_error(
    canonical_repo: Path,
    triage_home: Path,
    alembic_db: Path,
) -> None:
    """If a target's worktree is gone, we skip + record ``result='error'``."""
    _ = alembic_db
    repo_cfg = _make_repo_config(canonical_repo, name="demo")
    project = _make_project(repo_cfg)

    session = engine_module.SessionLocal()
    try:
        trigger = _make_trigger_bug(
            session, notion_page_id="p-trigger-missing"
        )
        target = _make_target_bug(
            session,
            notion_page_id="p-target-missing",
            affected_repos=["demo"],
        )
        target_id = target.id
        trigger_id = trigger.id
    finally:
        session.close()

    # NOTE: we deliberately do NOT seed a worktree.

    results = await rebase_module.rebase_in_flight(
        trigger_bug_id=trigger_id,
        repo_name="demo",
        project=project,
    )
    assert len(results) == 1
    assert results[0].result == "error"

    session = engine_module.SessionLocal()
    try:
        refreshed = repo_module.get_bug(session, target_id)
        assert refreshed is not None
        # Should NOT have been transitioned BLOCKED — error is not a block.
        assert refreshed.state == BugState.FIXING.value
        attempts = repo_module.list_rebase_attempts(session, target_id)
        assert len(attempts) == 1
        assert attempts[0].result == "error"
    finally:
        session.close()


async def test_fetch_conflict_diff_returns_diff_text(
    canonical_repo: Path,
    triage_home: Path,
    alembic_db: Path,
) -> None:
    """``fetch_conflict_diff`` returns a non-empty unified diff for the
    bug's branch vs ``origin/main``."""
    _ = alembic_db
    repo_cfg = _make_repo_config(canonical_repo, name="demo")
    project = _make_project(repo_cfg)

    session = engine_module.SessionLocal()
    try:
        target = _make_target_bug(
            session,
            notion_page_id="p-diff",
            affected_repos=["demo"],
        )
        target_id = target.id
    finally:
        session.close()

    _seed_target_worktree(
        canonical=canonical_repo,
        triage_home=triage_home,
        bug_id=target_id,
        repo_name="demo",
        invoice_line="HELLO WORLD\n",
    )

    diff = await rebase_module.fetch_conflict_diff(
        bug_id=target_id,
        repo_name="demo",
        project=project,
    )
    assert "invoice.txt" in diff
    assert "HELLO WORLD" in diff


async def test_rebase_in_flight_excludes_trigger_bug(
    canonical_repo: Path,
    triage_home: Path,
    alembic_db: Path,
) -> None:
    """The sweep skips the trigger bug itself even if it's in FIXING."""
    _ = alembic_db
    repo_cfg = _make_repo_config(canonical_repo, name="demo")
    project = _make_project(repo_cfg)

    session = engine_module.SessionLocal()
    try:
        # Trigger is itself in FIXING with the same affected repo; the
        # sweep must NOT process it (caller is responsible for excluding).
        trigger = _make_target_bug(
            session,
            notion_page_id="p-self-trigger",
            affected_repos=["demo"],
        )
        trigger_id = trigger.id
    finally:
        session.close()

    results = await rebase_module.rebase_in_flight(
        trigger_bug_id=trigger_id,
        repo_name="demo",
        project=project,
    )
    assert results == []


async def test_rebase_in_flight_records_attempt_only_for_matching_repo(
    canonical_repo: Path,
    triage_home: Path,
    alembic_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bugs whose affected_repos doesn't include repo_name are not touched."""
    _ = alembic_db
    repo_cfg = _make_repo_config(canonical_repo, name="demo")
    project = _make_project(repo_cfg)

    session = engine_module.SessionLocal()
    try:
        trigger = _make_trigger_bug(session, notion_page_id="p-trig-other")
        # Target affects only "other-repo" — should be skipped.
        target = _make_target_bug(
            session,
            notion_page_id="p-target-other-repo",
            affected_repos=["other-repo"],
        )
        trigger_id = trigger.id
        target_id = target.id
    finally:
        session.close()

    # Patch out the actual rebase so we can assert it was never called.
    called: list[int] = []

    async def _fake_rebase_one_bug(*, bug_id, repo_cfg, trigger_bug_id):
        called.append(bug_id)
        return rebase_module.RebaseResult(
            bug_id=bug_id,
            repo_name=repo_cfg.name,
            result="error",
            conflict_files=[],
            error_message="(unreachable)",
        )

    monkeypatch.setattr(
        rebase_module, "_rebase_one_bug", _fake_rebase_one_bug
    )

    results = await rebase_module.rebase_in_flight(
        trigger_bug_id=trigger_id,
        repo_name="demo",
        project=project,
    )
    assert results == []
    assert called == []
    assert target_id  # silence unused


