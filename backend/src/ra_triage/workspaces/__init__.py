"""Git worktree lifecycle + on-merge rebase sweeper.

Ownership: **Agent 4** writes worktree.py; **Agent 11** writes rebase.py.

Public API (from ``worktree``):

- :func:`create_worktrees_for_bug` — async, create one worktree per repo.
- :func:`cleanup` — async, remove the per-bug worktrees (retains branches).
- :func:`list_worktrees` — sync, scan disk for existing worktrees.
- :func:`path_for` — sync, deterministic destination path.
- :func:`branch_name` — async (for API uniformity), deterministic branch name.
- :class:`WorktreeError` / :class:`DirtyCanonicalCloneError` /
  :class:`WorktreeAlreadyExistsError` — exception hierarchy.
"""

from ra_triage.workspaces.worktree import (
    DirtyCanonicalCloneError,
    WorktreeAlreadyExistsError,
    WorktreeError,
    branch_name,
    cleanup,
    create_worktrees_for_bug,
    list_worktrees,
    path_for,
)

__all__ = [
    "DirtyCanonicalCloneError",
    "WorktreeAlreadyExistsError",
    "WorktreeError",
    "branch_name",
    "cleanup",
    "create_worktrees_for_bug",
    "list_worktrees",
    "path_for",
]
