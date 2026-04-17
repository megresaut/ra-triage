"""Unit tests for ``ra_triage.workspaces.worktree``.

These tests use a **fixture bare repo** built with real ``git`` in a
``tmp_path`` so we can exercise the full create/cleanup lifecycle offline,
with no network access, and without touching the real RA AVM clones.

Fixture approach (per Agent 4 brief):

- ``triage_home`` (from the shared ``tests/conftest.py``) redirects
  ``settings.triage_home`` to a tmp dir by setting ``$TRIAGE_HOME`` and
  clearing the Settings cache, so ``path_for`` / ``list_worktrees`` /
  ``create_worktrees_for_bug`` all land under ``tmp_path``.
- ``fake_repo`` builds a canonical clone + an ``origin`` bare remote and
  returns a synthetic :class:`RepoConfig` pointed at the canonical clone.
  Registry isn't touched — we construct :class:`RepoConfig` directly with
  the fields the worktree module reads (``path``, ``name``, ``main_branch``,
  ``branch_prefix``).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from ra_triage.config.registry import RepoConfig
from ra_triage.workspaces import worktree as wt

# --- worktree fixtures --------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    """Run ``git <args>`` in ``repo`` synchronously. Fixture helper only."""
    subprocess.check_call(
        [
            "git",
            "-C",
            str(repo),
            # Keep fixture commits reproducible & free of any local config.
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
    canonical: Path, name: str = "demo", branch_prefix: str = "triage/"
) -> RepoConfig:
    """Build a synthetic :class:`RepoConfig` that ``worktree.py`` can drive.

    Fields that worktree.py never reads are stuffed with dummy values — we
    only need name, path, main_branch, and branch_prefix to be real.
    """
    return RepoConfig(
        name=name,
        path=canonical,
        main_branch="main",
        branch_prefix=branch_prefix,
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


@pytest.fixture()
def fake_repo(tmp_path: Path) -> Iterator[Path]:
    """Initialize a canonical clone with a single commit on ``main``.

    Also sets up a bare ``origin`` remote so ``git fetch origin main`` is
    a real, network-free operation. Yields the canonical clone path.
    """
    canonical = tmp_path / "canonical"
    # Use -b main to avoid "master" default on older gits; even newer gits
    # accept this silently.
    subprocess.check_call(["git", "init", "-b", "main", str(canonical)])
    (canonical / "README.md").write_text("hello\n")
    _git(canonical, "add", "-A")
    _git(canonical, "commit", "-m", "init")

    bare = tmp_path / "origin.git"
    subprocess.check_call(
        ["git", "clone", "--bare", str(canonical), str(bare)]
    )
    _git(canonical, "remote", "add", "origin", str(bare))
    _git(canonical, "fetch", "origin")
    yield canonical


# --- path_for / list_worktrees (pure functions) -------------------------


def test_path_for_is_under_triage_home(triage_home: Path) -> None:
    """``path_for`` returns ``<triage_home>/worktrees/<bug_id>/<repo_name>``."""
    got = wt.path_for(42, "demo")
    assert got == triage_home / "worktrees" / "42" / "demo"


def test_list_worktrees_returns_empty_when_bug_dir_absent(
    triage_home: Path,
) -> None:
    """No bug dir on disk → empty dict, not an error."""
    assert wt.list_worktrees(99) == {}


def test_list_worktrees_finds_child_dirs(triage_home: Path) -> None:
    """``list_worktrees`` scans the bug's parent dir and returns child dirs."""
    parent = triage_home / "worktrees" / "42"
    (parent / "repo_a").mkdir(parents=True)
    (parent / "repo_b").mkdir(parents=True)
    got = wt.list_worktrees(42)
    assert set(got.keys()) == {"repo_a", "repo_b"}
    assert got["repo_a"] == parent / "repo_a"


# --- branch_name --------------------------------------------------------


async def test_branch_name_uses_repo_prefix_and_bug_id(
    fake_repo: Path,
) -> None:
    """Branch names are ``<branch_prefix><bug_id>`` — e.g. ``triage/42``."""
    cfg = _make_repo_config(fake_repo)
    assert await wt.branch_name(42, cfg) == "triage/42"

    cfg_custom = _make_repo_config(fake_repo, branch_prefix="bugfix/")
    assert await wt.branch_name(7, cfg_custom) == "bugfix/7"


# --- create_worktrees_for_bug: happy path -------------------------------


async def test_create_worktrees_for_bug_happy_path(
    fake_repo: Path, triage_home: Path
) -> None:
    """Creates a worktree on ``triage/42`` off ``origin/main``."""
    cfg = _make_repo_config(fake_repo)
    result = await wt.create_worktrees_for_bug(42, [cfg])

    expected = triage_home / "worktrees" / "42" / "demo"
    assert result == {"demo": expected}
    assert expected.is_dir()
    assert (expected / "README.md").read_text() == "hello\n"

    # Branch is triage/42 and descends from origin/main.
    branch = subprocess.check_output(
        ["git", "-C", str(expected), "rev-parse", "--abbrev-ref", "HEAD"]
    ).decode().strip()
    assert branch == "triage/42"


# --- idempotency --------------------------------------------------------


async def test_create_is_idempotent_on_matching_branch(
    fake_repo: Path, triage_home: Path
) -> None:
    """Calling create twice is a no-op: same path, no error, branch intact."""
    cfg = _make_repo_config(fake_repo)
    first = await wt.create_worktrees_for_bug(42, [cfg])
    second = await wt.create_worktrees_for_bug(42, [cfg])
    assert first == second

    branch = subprocess.check_output(
        ["git", "-C", str(first["demo"]), "rev-parse", "--abbrev-ref", "HEAD"]
    ).decode().strip()
    assert branch == "triage/42"


# --- dirty canonical clone ---------------------------------------------


async def test_create_raises_on_dirty_canonical_clone(
    fake_repo: Path, triage_home: Path
) -> None:
    """Uncommitted changes in the canonical clone → DirtyCanonicalCloneError."""
    (fake_repo / "dirty.txt").write_text("uncommitted\n")
    cfg = _make_repo_config(fake_repo)

    with pytest.raises(wt.DirtyCanonicalCloneError) as excinfo:
        await wt.create_worktrees_for_bug(42, [cfg])

    # Porcelain output must be embedded so Megha can see what's dirty.
    assert "dirty.txt" in str(excinfo.value)


# --- branch mismatch ----------------------------------------------------


async def test_create_raises_when_existing_path_has_wrong_branch(
    fake_repo: Path, triage_home: Path
) -> None:
    """Dest dir exists on a DIFFERENT branch → WorktreeAlreadyExistsError."""
    # Pre-create a worktree on a different branch at the exact destination
    # by using a different branch_prefix first.
    cfg_wrong_prefix = _make_repo_config(fake_repo, branch_prefix="other/")
    await wt.create_worktrees_for_bug(42, [cfg_wrong_prefix])

    # Now try with the canonical prefix — same dest path, different branch.
    cfg_right_prefix = _make_repo_config(fake_repo, branch_prefix="triage/")
    with pytest.raises(wt.WorktreeAlreadyExistsError):
        await wt.create_worktrees_for_bug(42, [cfg_right_prefix])


async def test_create_raises_when_existing_path_is_not_a_worktree(
    fake_repo: Path, triage_home: Path
) -> None:
    """Dest dir exists but has no ``.git`` marker → WorktreeAlreadyExistsError."""
    dest = wt.path_for(42, "demo")
    dest.mkdir(parents=True)
    (dest / "stranger.txt").write_text("who put this here\n")

    cfg = _make_repo_config(fake_repo)
    with pytest.raises(wt.WorktreeAlreadyExistsError):
        await wt.create_worktrees_for_bug(42, [cfg])


# --- cleanup ------------------------------------------------------------


async def test_cleanup_removes_worktree_but_retains_branch(
    fake_repo: Path, triage_home: Path
) -> None:
    """``cleanup`` removes the worktree dir; branch survives in canonical."""
    cfg = _make_repo_config(fake_repo)
    paths = await wt.create_worktrees_for_bug(42, [cfg])
    assert paths["demo"].is_dir()

    await wt.cleanup(42, [cfg])

    assert not paths["demo"].exists()
    # Parent bug dir should also be removed once empty.
    assert not (triage_home / "worktrees" / "42").exists()

    # Branch must still exist in the canonical clone — Agent 8 deletes it
    # after PR merge, not us.
    branches = subprocess.check_output(
        ["git", "-C", str(fake_repo), "branch", "--list", "triage/42"]
    ).decode()
    assert "triage/42" in branches


async def test_cleanup_is_idempotent_when_worktree_already_gone(
    fake_repo: Path, triage_home: Path
) -> None:
    """Calling ``cleanup`` twice — or on a never-created bug — is fine."""
    cfg = _make_repo_config(fake_repo)
    # First: nothing to clean.
    await wt.cleanup(999, [cfg])  # should not raise

    # Second scenario: create, clean, clean again.
    await wt.create_worktrees_for_bug(42, [cfg])
    await wt.cleanup(42, [cfg])
    await wt.cleanup(42, [cfg])  # still no error


# --- concurrency sanity -------------------------------------------------


async def test_concurrent_bugs_do_not_collide(
    fake_repo: Path, triage_home: Path
) -> None:
    """Two simultaneous create calls for different bug ids both succeed."""
    import asyncio

    cfg = _make_repo_config(fake_repo)
    results = await asyncio.gather(
        wt.create_worktrees_for_bug(42, [cfg]),
        wt.create_worktrees_for_bug(51, [cfg]),
    )
    assert results[0]["demo"] != results[1]["demo"]
    assert results[0]["demo"].is_dir()
    assert results[1]["demo"].is_dir()
