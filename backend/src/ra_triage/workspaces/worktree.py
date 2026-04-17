"""Per-bug git worktree lifecycle for RA Triage.

Each bug in flight gets one worktree per affected repo, checked out on a
branch ``<branch_prefix><bug_id>`` (e.g. ``triage/42``) off
``origin/<main_branch>``, under::

    <triage_home>/worktrees/<bug_id>/<repo_name>/

This module provides the async-native create / cleanup / list API the
orchestrator pipeline drives. Agent 4 only exposes the functions; Agent 5
wires ``create_worktrees_for_bug`` into the OPEN → INVESTIGATING transition
in ``orchestrator/pipeline.py``.

Design notes
------------

- **Async-native.** ``asyncio.create_subprocess_exec`` is used for every
  ``git`` invocation so the orchestrator (itself async) never blocks its
  event loop on a subprocess. Each call has a 60-second timeout.
- **Fail loud on dirty canonical clones.** Before fetching, we check
  ``git status --porcelain`` in the canonical clone (``repo.path``). If it
  is non-empty, we raise :class:`DirtyCanonicalCloneError` with the
  porcelain output embedded — we never silently skip. This matches the
  tech plan §10.1 contract.
- **Idempotency.** If ``create_worktrees_for_bug`` is called twice for the
  same bug and the existing per-repo worktree is already on the expected
  branch, the second call is a no-op and returns the same paths. If a
  directory exists at the destination but the branch is wrong — or the
  directory isn't a git worktree at all — we raise
  :class:`WorktreeAlreadyExistsError` rather than clobber the user's state.
  The orchestrator doc says "pick one and document it"; we pick
  no-op-on-match, raise-on-mismatch.
- **Cleanup is idempotent.** Calling ``cleanup`` on a bug whose worktrees
  were already removed manually is not an error — we log and continue.
  Branches are NOT deleted here; per tech plan §10.1 they are retained
  until PR merge (Agent 8's concern).
- **All paths resolve under ``settings.triage_home``.** We never hardcode
  ``~`` or absolute user paths. Tests can redirect by setting the
  ``TRIAGE_HOME`` env var before calling into this module; see the
  ``triage_home`` fixture in ``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from pathlib import Path

from ra_triage.config.registry import RepoConfig
from ra_triage.settings import get_settings

logger = logging.getLogger(__name__)

# Every git subprocess gets the same ceiling. 60s is comfortably long for a
# worktree add / fetch against a local canonical clone; if we ever hit it,
# something is wrong and we want to surface it rather than hang.
_GIT_TIMEOUT_SECONDS = 60.0


# --- exceptions ---------------------------------------------------------


class WorktreeError(Exception):
    """Base for all worktree-management errors raised by this module."""


class DirtyCanonicalCloneError(WorktreeError):
    """The canonical clone has uncommitted changes; refusing to fetch/branch.

    We refuse rather than silently continue because a dirty canonical clone
    means Megha has in-progress work there, and ``git worktree add`` off
    ``origin/main`` could surprise her. The porcelain status is embedded
    in the message so the caller sees exactly what's dirty.
    """


class WorktreeAlreadyExistsError(WorktreeError):
    """A conflicting worktree / directory already occupies the destination.

    Raised when the destination path exists but is NOT a worktree on the
    expected branch (e.g. wrong branch checked out, or a non-git directory
    someone dropped there). The caller should inspect and resolve manually.
    """


# --- public API ---------------------------------------------------------


async def branch_name(bug_id: int, repo: RepoConfig) -> str:
    """Return the deterministic branch name for this bug in this repo.

    Formed as ``<repo.branch_prefix><bug_id>`` — e.g. ``triage/42`` for the
    RA AVM registry entry. Async for API uniformity; no IO is performed.
    """
    return f"{repo.branch_prefix}{bug_id}"


def path_for(bug_id: int, repo_name: str) -> Path:
    """Return the deterministic worktree path for ``(bug_id, repo_name)``.

    Rooted under ``settings.triage_home / "worktrees"``; does NOT create the
    directory. Pure function (modulo the settings lookup).
    """
    return get_settings().worktrees_dir / str(bug_id) / repo_name


def list_worktrees(bug_id: int) -> dict[str, Path]:
    """Return ``{repo_name: path}`` for every existing worktree under
    ``<triage_home>/worktrees/<bug_id>/``.

    Only returns entries that exist on disk as directories; doesn't
    validate that they are real git worktrees (use ``git worktree list`` in
    the canonical clone for that). Empty dict if the bug's parent dir
    doesn't exist yet.
    """
    parent = get_settings().worktrees_dir / str(bug_id)
    if not parent.is_dir():
        return {}
    return {child.name: child for child in sorted(parent.iterdir()) if child.is_dir()}


async def create_worktrees_for_bug(
    bug_id: int, repos: list[RepoConfig]
) -> dict[str, Path]:
    """Create (or verify-existing) one worktree per repo for this bug.

    For each repo:
      1. Verify the canonical clone at ``repo.path`` is clean; if not,
         raise :class:`DirtyCanonicalCloneError`.
      2. ``git fetch origin <repo.main_branch>`` in the canonical clone.
      3. Ensure ``<triage_home>/worktrees/<bug_id>/`` exists.
      4. If the destination already exists and is a worktree on the
         expected branch, no-op. If it exists with a different branch OR
         without a ``.git`` marker, raise :class:`WorktreeAlreadyExistsError`.
      5. Otherwise ``git worktree add <dest> -b <branch> origin/<main_branch>``
         from within the canonical clone.

    Returns a ``{repo.name: worktree_path}`` dict on success. Raises on any
    failure; partial creation is possible if one repo succeeds and a later
    one fails — the caller (orchestrator) is responsible for cleanup on
    failure. We do NOT roll back successful creations because that would
    lose work in the common case where repo N+1 simply has a transient
    hiccup.
    """
    results: dict[str, Path] = {}
    for repo in repos:
        dest = path_for(bug_id, repo.name)
        branch = await branch_name(bug_id, repo)

        # Step 1: dirty check on the canonical clone. Do this BEFORE fetch
        # so we don't modify the user's refs if we're about to abort.
        await _assert_canonical_clone_clean(repo)

        # Step 2: fetch. Small and safe even if we end up no-op'ing below.
        await _run_git(
            ["fetch", "origin", repo.main_branch],
            cwd=repo.path,
            description=f"fetch origin/{repo.main_branch} in {repo.path}",
        )

        # Step 3: make sure the bug's parent dir exists.
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Step 4/5: idempotency branch.
        if dest.exists():
            existing = await _existing_worktree_branch(dest)
            if existing == branch:
                logger.info(
                    "Worktree for bug %d repo %s already exists on branch %s; no-op.",
                    bug_id,
                    repo.name,
                    branch,
                )
                results[repo.name] = dest
                continue
            raise WorktreeAlreadyExistsError(
                f"Destination {dest} already exists but is not a worktree on "
                f"branch {branch!r} (found: {existing!r}). Inspect and clean "
                "up manually before retrying."
            )

        await _run_git(
            ["worktree", "add", str(dest), "-b", branch, f"origin/{repo.main_branch}"],
            cwd=repo.path,
            description=f"worktree add {dest} -b {branch} for bug {bug_id}",
        )
        logger.info(
            "Created worktree for bug %d repo %s at %s on branch %s.",
            bug_id,
            repo.name,
            dest,
            branch,
        )
        results[repo.name] = dest

    return results


async def cleanup(bug_id: int, repos: list[RepoConfig]) -> None:
    """Remove all per-repo worktrees for this bug. Idempotent.

    For each repo, runs ``git -C <repo.path> worktree remove --force <dest>``;
    if the destination directory is already gone, we skip it (and still run
    ``worktree prune`` below so stale metadata is cleaned up).

    After per-repo removal, we run ``git worktree prune`` in each canonical
    clone for hygiene, and finally remove the now-empty
    ``<triage_home>/worktrees/<bug_id>/`` parent directory. Branches are
    NOT deleted — Agent 8 handles branch deletion after PR merge.
    """
    for repo in repos:
        dest = path_for(bug_id, repo.name)

        if dest.exists():
            remove_result = await _run_git(
                ["worktree", "remove", "--force", str(dest)],
                cwd=repo.path,
                description=f"worktree remove {dest}",
                check=False,
            )
            if remove_result.returncode != 0:
                # Fallback: if git refuses for some odd reason, blow the
                # directory away directly and prune below. We log but don't
                # raise — cleanup is best-effort.
                logger.warning(
                    "git worktree remove exited %d for %s; stderr=%r. "
                    "Falling back to shutil.rmtree.",
                    remove_result.returncode,
                    dest,
                    remove_result.stderr,
                )
                shutil.rmtree(dest, ignore_errors=True)
        else:
            logger.info(
                "Worktree path %s already gone; skipping git worktree remove.", dest
            )

        # Prune stale worktree metadata regardless of above outcome.
        await _run_git(
            ["worktree", "prune"],
            cwd=repo.path,
            description=f"worktree prune in {repo.path}",
            check=False,
        )

    # Remove the parent <bug_id>/ dir once empty. If siblings somehow
    # remain (unexpected), leave it alone — caller can inspect.
    parent = get_settings().worktrees_dir / str(bug_id)
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()


# --- internals ----------------------------------------------------------


class _GitResult:
    """Small container for a completed ``git`` subprocess invocation."""

    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


async def _run_git(
    args: list[str],
    *,
    cwd: Path,
    description: str,
    check: bool = True,
) -> _GitResult:
    """Run ``git <args>`` in ``cwd`` with a bounded timeout.

    Captures stdout+stderr as text. On ``check=True`` (default) a non-zero
    exit raises :class:`WorktreeError` with stdout/stderr embedded. Timeouts
    also raise :class:`WorktreeError` after killing the subprocess.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=_GIT_TIMEOUT_SECONDS
        )
    except TimeoutError as exc:
        # Kill the hanging subprocess before re-raising so we don't leak it.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise WorktreeError(
            f"git {' '.join(args)} in {cwd} timed out after "
            f"{_GIT_TIMEOUT_SECONDS:.0f}s ({description})"
        ) from exc

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    result = _GitResult(proc.returncode or 0, stdout, stderr)

    if check and result.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed in {cwd} "
            f"(exit {result.returncode}): {description}\n"
            f"stdout: {stdout.strip()}\n"
            f"stderr: {stderr.strip()}"
        )

    return result


async def _assert_canonical_clone_clean(repo: RepoConfig) -> None:
    """Raise if ``git status --porcelain`` reports anything in ``repo.path``.

    A clean tree is load-bearing for ``git worktree add`` off
    ``origin/<main_branch>`` — uncommitted changes in the canonical clone
    indicate in-progress work we don't want to disturb.
    """
    if not repo.path.exists():
        raise WorktreeError(
            f"Canonical clone for repo {repo.name!r} not found at {repo.path}. "
            "Check registry.py and your local clones."
        )

    result = await _run_git(
        ["status", "--porcelain"],
        cwd=repo.path,
        description=f"status --porcelain in {repo.path}",
    )
    if result.stdout.strip():
        raise DirtyCanonicalCloneError(
            f"Canonical clone at {repo.path} has uncommitted changes; "
            f"refusing to fetch/branch. Commit or stash first.\n"
            f"git status --porcelain output:\n{result.stdout}"
        )


async def _existing_worktree_branch(dest: Path) -> str | None:
    """Return the current branch in ``dest`` if it is a worktree, else None.

    We detect "is a worktree" by the presence of a ``.git`` file or dir
    inside ``dest`` (git worktrees place a ``.git`` FILE pointing at the
    parent's worktrees/ metadata; full clones have a ``.git`` DIR). If
    neither form is present, we return None so the caller can raise a
    clear "something else is in the way" error.
    """
    git_marker = dest / ".git"
    if not git_marker.exists():
        return None

    result = await _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        cwd=dest,
        description=f"rev-parse HEAD in {dest}",
        check=False,
    )
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    # Detached HEAD reports as "HEAD"; treat as "not on a named branch".
    if not branch or branch == "HEAD":
        return None
    return branch


