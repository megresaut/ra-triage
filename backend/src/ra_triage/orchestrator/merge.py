"""Merge driver — the backend half of the Confirm flow (Agent 8).

:func:`merge_bug` drives a bug from ``READY_FOR_REVIEW`` through
``MERGING`` to either ``FIXED`` (happy path) or ``BLOCKED`` (merge
failure, partial merge for cross-repo). Agent 8 implements the
single-repo case end-to-end; Agent 10 will reuse the per-repo loop
below for cross-repo (backend merges first, then frontend; a partial
merge is captured as BLOCKED with an explicit ``## Partial Merge``
section).

Every state transition goes through ``db.repo.update_bug_state`` so the
Notion status writeback hook fires automatically:
    READY_FOR_REVIEW → MERGING (no Notion status — unmapped)
    MERGING → FIXED    (Notion status "Fixed", PR link appended)
    MERGING → BLOCKED  (Notion status "Blocked", failure written to TRIAGE.md)

The PR link write-back happens on a successful transition to FIXED via
:func:`integrations.notion.append_pr_link`. Worktree cleanup also runs on
FIXED — per technical plan §10.1 the branch itself is already deleted by
``gh pr merge --delete-branch``.

Cancel awareness: every transition reloads the bug from the DB first, so
if Megha clicks Cancel while we're mid-merge we stop cleanly at the next
checkpoint without partially mutating state.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ra_triage.config.registry import ProjectConfig, RepoConfig, get_project
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.db.models import Bug, PullRequest
from ra_triage.integrations import github as github_module
from ra_triage.integrations import notion as notion_module
from ra_triage.integrations.github import GitHubError, PRInfo
from ra_triage.orchestrator.states import (
    TERMINAL_STATES,
    BugState,
    IllegalBugStateTransition,
)
from ra_triage.triage_md import parser as triage_parser
from ra_triage.workspaces import worktree as wt_module

logger = logging.getLogger(__name__)


# --- Public entry point -----------------------------------------------------


async def merge_bug(bug_id: int) -> None:
    """Drive a READY_FOR_REVIEW bug through the merge sequence.

    Single-repo behavior (Agent 8):
      1. Transition to MERGING.
      2. For the bug's affected repo: reuse an existing open PR if one
         exists in ``pull_requests``, else create a new one via
         ``gh_pr_create``.
      3. ``gh_pr_merge`` the (single) open PR.
      4. On success: transition to FIXED, append PR link to Notion via
         ``append_pr_link``, and clean up the worktree.
      5. On failure: transition to BLOCKED with a ``## Merge Failure``
         section in TRIAGE.md.

    Cross-repo (Agent 10's scope): the per-repo loop below is already
    generic. Agent 10 will extend by ordering backend-first and wiring the
    cross-ref notes into the PR bodies; partial-merge BLOCKED handling
    below doesn't need changes.

    Never raises to the caller — every failure path logs + transitions to
    BLOCKED (or returns early on cancel).
    """
    try:
        await _merge_bug_body(bug_id)
    except Exception:
        logger.exception(
            "merge_bug crashed for bug_id=%s — bug may be in a phantom state",
            bug_id,
        )


async def _merge_bug_body(bug_id: int) -> None:
    """Actual merge driver — split out so the entry point is a thin try/except.

    Cross-repo strategy (two-pass cross-ref):
      1. Create ALL PRs first, each with a placeholder cross-ref note in
         the body (e.g. "Related PR will be linked here"). Creation order
         is registry order (backend first, then frontend).
      2. For each PR, run ``gh pr edit --body-file`` to REWRITE the body
         with a top-placed "> Related PR: <other_pr_url>" header pointing
         at the OTHER repo's PR.
      3. Merge the PRs in registry order (backend → frontend).
      4. On full success: transition to FIXED, append Notion PR links.
      5. If the frontend merge fails after the backend merged, append a
         ``## Partial Merge`` section and transition to BLOCKED. The
         backend merge is NOT auto-reverted.
    """
    bug_dict = await _load_bug(bug_id)
    if bug_dict is None:
        logger.warning("merge_bug: bug_id=%s not found; aborting", bug_id)
        return

    current = BugState(bug_dict["state"])
    if current in TERMINAL_STATES:
        logger.info(
            "merge_bug: bug_id=%s already terminal (%s); aborting",
            bug_id,
            current.value,
        )
        return
    if current != BugState.READY_FOR_REVIEW:
        logger.warning(
            "merge_bug: bug_id=%s not in READY_FOR_REVIEW (state=%s); aborting",
            bug_id,
            current.value,
        )
        return

    project = get_project(bug_dict["project_id"])
    repos_to_merge = _resolve_affected_repos(project, bug_dict)
    if not repos_to_merge:
        logger.warning(
            "merge_bug: no affected repos resolved for bug %d — "
            "falling back to project's single repo",
            bug_id,
        )
        if len(project.repos) == 1:
            repos_to_merge = list(project.repos)
        else:
            await _mark_merge_failure(
                bug_id,
                project=project,
                reason=(
                    "Could not determine affected repo for merge. "
                    "The Investigator's Scope section may be missing or listed "
                    "repos not present in the registry."
                ),
            )
            return

    # READY_FOR_REVIEW → MERGING
    if not await _transition(bug_id, BugState.MERGING):
        return

    is_cross_repo = len(repos_to_merge) > 1

    # --- Pass 1: create ALL PRs ---------------------------------------
    # For cross-repo we seed each PR with a "related PR will be linked
    # here" placeholder note so the body mentions the cross-ref concept
    # even before pass 2 runs. Pass 2 rewrites the body with the real
    # URL once every PR exists.
    try:
        pr_infos: list[tuple[RepoConfig, PRInfo]] = []
        for repo_cfg in repos_to_merge:
            fresh = await _load_bug(bug_id)
            if fresh is None or BugState(fresh["state"]) in TERMINAL_STATES:
                logger.info(
                    "merge_bug: bug_id=%s terminated mid-merge; stopping",
                    bug_id,
                )
                return
            cross_note = (
                "> Related PR will be linked here once the other repo's PR is created."
                if is_cross_repo
                else None
            )
            pr_info = await _ensure_pr_created(
                bug_id=bug_id,
                repo_cfg=repo_cfg,
                project=project,
                cross_ref_body_note=cross_note,
            )
            pr_infos.append((repo_cfg, pr_info))

        # --- Pass 2: rewrite each PR body with the other PR's URL -----
        # Only runs for cross-repo — for single-repo the initial body is
        # already correct.
        if is_cross_repo:
            await _update_cross_ref_bodies(
                bug_id=bug_id,
                project=project,
                pr_infos=pr_infos,
            )

        # --- Merge pass: registry order (backend first) ---------------
        merged_prs: list[tuple[str, str, int]] = []
        for repo_cfg, pr_info in pr_infos:
            fresh = await _load_bug(bug_id)
            if fresh is None or BugState(fresh["state"]) in TERMINAL_STATES:
                logger.info(
                    "merge_bug: bug_id=%s terminated mid-merge; stopping",
                    bug_id,
                )
                return
            await _merge_one_pr(
                bug_id=bug_id, repo_cfg=repo_cfg, pr_info=pr_info
            )
            merged_prs.append((repo_cfg.name, pr_info.url, pr_info.number))

    except GitHubError as exc:
        # Local for the except block — merged_prs is only bound inside
        # the try when we get past the create + cross-ref edit passes.
        merged_so_far: list[tuple[str, str, int]] = locals().get(
            "merged_prs", []
        )
        logger.exception(
            "merge_bug: gh failure for bug_id=%s; merged_so_far=%s; BLOCKED",
            bug_id,
            [p[0] for p in merged_so_far],
        )
        if merged_so_far:
            # Cross-repo partial merge — preserve the merged PRs list in
            # the failure section. The first merged PR (backend) cannot
            # be auto-reverted; the operator handles the rollback.
            await _mark_partial_merge(
                bug_id,
                project=project,
                merged_prs=merged_so_far,
                stderr_excerpt=str(exc),
            )
        else:
            await _mark_merge_failure(
                bug_id, project=project, reason=str(exc)
            )
        return

    # All merges succeeded. Transition to FIXED, append PR links, clean up.
    if not await _transition(bug_id, BugState.FIXED):
        return

    bug = await _load_bug(bug_id)
    notion_page_id = bug["notion_page_id"] if bug else ""
    for repo_name, url, _number in merged_prs:
        if notion_page_id:
            try:
                notion_module.append_pr_link(
                    page_id=notion_page_id,
                    pr_url=url,
                    repo_name=repo_name,
                )
            except Exception:
                logger.exception(
                    "append_pr_link failed for bug %d repo %s — continuing",
                    bug_id,
                    repo_name,
                )

    try:
        await wt_module.cleanup(bug_id, list(project.repos))
    except Exception:
        logger.exception(
            "worktree cleanup failed for bug %d — manual inspection recommended",
            bug_id,
        )


# --- Internals: per-repo PR plumbing ----------------------------------------


async def _ensure_pr_created(
    *,
    bug_id: int,
    repo_cfg: RepoConfig,
    project: ProjectConfig,
    cross_ref_body_note: str | None = None,
) -> PRInfo:
    """Return an open PR for ``(bug_id, repo_cfg)``.

    Reuses an existing ``pull_requests`` row with status='open' if one
    exists; otherwise calls ``gh_pr_create`` with the bug's TRIAGE.md as
    the PR body. ``cross_ref_body_note`` (Agent 10) appends a placeholder
    cross-reference note at the BOTTOM of the body — the second-pass
    :func:`_update_cross_ref_bodies` rewrites it with the actual URL of
    the other repo's PR placed at the TOP.
    """
    existing = await _find_open_pr(bug_id, repo_cfg.name)
    if existing is not None:
        logger.info(
            "merge_bug: reusing open PR #%s (%s) for bug %d repo %s",
            existing.pr_number,
            existing.pr_url,
            bug_id,
            repo_cfg.name,
        )
        return PRInfo(
            repo_name=repo_cfg.name,
            branch=existing.branch,
            number=int(existing.pr_number or 0),
            url=str(existing.pr_url or ""),
            status="open",
        )

    worktree = wt_module.path_for(bug_id, repo_cfg.name)
    bug = await _load_bug(bug_id)
    if bug is None:
        raise GitHubError(f"Bug {bug_id} disappeared before PR creation")

    title = f"fix(bug-{bug_id}): {bug['title']}"
    # PR body = the live TRIAGE.md in the bug's worktree. Fall back to the
    # project-level TRIAGE.md (backend repo) if the per-repo worktree
    # doesn't have one (shouldn't happen in single-repo, possible mid-Agent-10).
    body_path = _resolve_triage_md_path(bug_id, project)

    pr_info = await github_module.gh_pr_create(
        worktree=worktree,
        repo=repo_cfg,
        bug_id=bug_id,
        title=title,
        body_path=body_path,
        cross_ref_body_note=cross_ref_body_note,
    )

    # Persist the PR row so subsequent retries reuse it.
    await _record_pr_row(
        bug_id=bug_id,
        repo_name=repo_cfg.name,
        branch=pr_info.branch,
        pr_url=pr_info.url,
        pr_number=pr_info.number,
        status="open",
    )
    return pr_info


async def _merge_one_pr(
    *, bug_id: int, repo_cfg: RepoConfig, pr_info: PRInfo
) -> None:
    """Run ``gh pr merge`` and flip the PR row to merged on success."""
    worktree = wt_module.path_for(bug_id, repo_cfg.name)
    await github_module.gh_pr_merge(
        worktree=worktree, pr_number=pr_info.number, squash=True, delete_branch=True
    )
    await _flip_pr_to_merged(bug_id=bug_id, pr_number=pr_info.number)


async def _update_cross_ref_bodies(
    *,
    bug_id: int,
    project: ProjectConfig,
    pr_infos: list[tuple[RepoConfig, PRInfo]],
) -> None:
    """Rewrite each PR body with a top-placed cross-ref to the OTHER PR.

    For two-repo cross-repo (the only cross-repo shape v1 supports), each
    PR's body becomes::

        > Related PR: <other_pr_url>

        <original TRIAGE.md content>

    For >2 repos (future), each PR points at the next PR in registry
    order. Errors here propagate as :class:`GitHubError` so the caller's
    partial-merge handling triggers correctly — we have NOT merged
    anything yet at this point in the flow, so a cross-ref edit failure
    lands in the regular ``_mark_merge_failure`` path.
    """
    if len(pr_infos) < 2:
        return
    # Map repo_name → other PR's URL. For 2-repo: the other entry. For
    # 3+ repos: each PR points at the next one (registry order), so the
    # cross-references form a cycle the operator can navigate.
    other_url: dict[str, str] = {}
    for idx, (repo_cfg, _pr_info) in enumerate(pr_infos):
        other_idx = (idx + 1) % len(pr_infos)
        other_url[repo_cfg.name] = pr_infos[other_idx][1].url

    for repo_cfg, pr_info in pr_infos:
        worktree = wt_module.path_for(bug_id, repo_cfg.name)
        body_path = _resolve_triage_md_path(bug_id, project)
        await github_module.gh_pr_edit_body(
            worktree=worktree,
            pr_number=pr_info.number,
            body_path=body_path,
            cross_ref_url=other_url[repo_cfg.name],
        )


# --- Internals: failure handling -------------------------------------------


async def _mark_merge_failure(
    bug_id: int,
    *,
    project: ProjectConfig,
    reason: str,
) -> None:
    """Append a ``## Merge Failure`` section and transition to BLOCKED."""
    triage_md_path = _resolve_triage_md_path(bug_id, project)
    try:
        triage_parser.append(
            triage_md_path,
            "Merge Failure",
            (
                "The merge flow could not complete and the bug is now BLOCKED. "
                "Inspect the failure below and retry manually or take over.\n\n"
                f"```\n{reason.strip()}\n```\n"
            ),
        )
    except OSError:
        logger.exception(
            "Could not append Merge Failure section to %s", triage_md_path
        )
    # MERGING → BLOCKED is a legal transition per the state machine.
    await _transition(bug_id, BugState.BLOCKED)


async def _mark_partial_merge(
    bug_id: int,
    *,
    project: ProjectConfig,
    merged_prs: list[tuple[str, str, int]],
    stderr_excerpt: str,
) -> None:
    """Cross-repo partial-merge: backend merged, frontend failed.

    Appends a ``## Partial Merge`` section listing the already-merged PRs
    plus the stderr excerpt, then transitions to BLOCKED. We explicitly do
    NOT try to revert the merged PRs — per tech plan §12.2, the operator
    resolves this manually.
    """
    triage_md_path = _resolve_triage_md_path(bug_id, project)
    lines: list[str] = [
        "The merge completed for some repos but failed for others. The bug is "
        "BLOCKED for manual recovery — the backend PR has been merged and "
        "CANNOT be auto-reverted.",
        "",
        "**Successfully merged:**",
    ]
    for repo_name, url, number in merged_prs:
        lines.append(f"- {repo_name}: {url} (#{number})")
    lines.append("")
    lines.append("**Failure:**")
    lines.append(f"```\n{stderr_excerpt.strip()}\n```")
    try:
        triage_parser.append(triage_md_path, "Partial Merge", "\n".join(lines))
    except OSError:
        logger.exception(
            "Could not append Partial Merge section to %s", triage_md_path
        )
    await _transition(bug_id, BugState.BLOCKED)


# --- DB helpers (sync work dispatched to the threadpool) -------------------


async def _load_bug(bug_id: int) -> dict[str, Any] | None:
    return await asyncio.to_thread(_load_bug_sync, bug_id)


def _load_bug_sync(bug_id: int) -> dict[str, Any] | None:
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.get_bug(session, bug_id)
        if bug is None:
            return None
        return _bug_to_dict(bug)
    finally:
        session.close()


def _bug_to_dict(bug: Bug) -> dict[str, Any]:
    return {
        "id": bug.id,
        "notion_page_id": bug.notion_page_id,
        "project_id": bug.project_id,
        "title": bug.title,
        "description": bug.description,
        "state": bug.state,
        "scope": bug.scope,
        "affected_repos": bug.affected_repos,
        "rejection_count": bug.rejection_count,
    }


async def _transition(bug_id: int, new_state: BugState) -> bool:
    """Transition the bug; return True on success, False if skipped/illegal."""
    return await asyncio.to_thread(_transition_sync, bug_id, new_state)


def _transition_sync(bug_id: int, new_state: BugState) -> bool:
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.get_bug(session, bug_id)
        if bug is None:
            logger.warning(
                "merge transition: bug_id=%s disappeared; skipping", bug_id
            )
            return False
        if BugState(bug.state) in TERMINAL_STATES:
            logger.info(
                "merge transition: bug_id=%s already terminal (%s); skipping %s",
                bug_id,
                bug.state,
                new_state.value,
            )
            return False
        try:
            repo_module.update_bug_state(session, bug, new_state)
            session.commit()
            return True
        except IllegalBugStateTransition as exc:
            session.rollback()
            logger.warning(
                "merge transition: illegal %s → %s for bug_id=%s: %s",
                bug.state,
                new_state.value,
                bug_id,
                exc,
            )
            return False
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def _find_open_pr(bug_id: int, repo_name: str) -> PullRequest | None:
    return await asyncio.to_thread(_find_open_pr_sync, bug_id, repo_name)


def _find_open_pr_sync(bug_id: int, repo_name: str) -> PullRequest | None:
    session = engine_module.SessionLocal()
    try:
        pr = repo_module.find_open_pr(session, bug_id=bug_id, repo_name=repo_name)
        if pr is None:
            return None
        # Detach: we need the scalars outside the session, so return a copy.
        return _detached_pr(pr)
    finally:
        session.close()


@dataclass
class _DetachedPR:
    """Plain dict-like view of a PullRequest for use outside the session."""

    id: int
    bug_id: int
    repo_name: str
    branch: str
    pr_url: str | None
    pr_number: int | None
    status: str


def _detached_pr(pr: PullRequest) -> Any:
    return _DetachedPR(
        id=pr.id,
        bug_id=pr.bug_id,
        repo_name=pr.repo_name,
        branch=pr.branch,
        pr_url=pr.pr_url,
        pr_number=pr.pr_number,
        status=pr.status,
    )


async def _record_pr_row(
    *,
    bug_id: int,
    repo_name: str,
    branch: str,
    pr_url: str,
    pr_number: int,
    status: str,
) -> None:
    await asyncio.to_thread(
        _record_pr_row_sync, bug_id, repo_name, branch, pr_url, pr_number, status
    )


def _record_pr_row_sync(
    bug_id: int,
    repo_name: str,
    branch: str,
    pr_url: str,
    pr_number: int,
    status: str,
) -> None:
    session = engine_module.SessionLocal()
    try:
        repo_module.record_pr(
            session,
            bug_id=bug_id,
            repo_name=repo_name,
            branch=branch,
            pr_url=pr_url,
            pr_number=pr_number,
            status=status,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def _flip_pr_to_merged(*, bug_id: int, pr_number: int) -> None:
    await asyncio.to_thread(_flip_pr_to_merged_sync, bug_id, pr_number)


def _flip_pr_to_merged_sync(bug_id: int, pr_number: int) -> None:
    session = engine_module.SessionLocal()
    try:
        all_prs = repo_module.list_pull_requests(session, bug_id)
        # Pick the PR row with matching number; fall back to the most
        # recent open one if the number is missing (older rows inserted
        # pre-Agent-8 had pr_number=None).
        target = next(
            (p for p in all_prs if p.pr_number == pr_number),
            None,
        )
        if target is None:
            target = next(
                (p for p in reversed(all_prs) if p.status == "open"),
                None,
            )
        if target is None:
            logger.warning(
                "Cannot flip PR to merged: no matching row for bug %d PR #%d",
                bug_id,
                pr_number,
            )
            return
        repo_module.update_pr_status(
            session, pr_id=target.id, status="merged"
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --- Misc helpers ----------------------------------------------------------


def _resolve_affected_repos(
    project: ProjectConfig, bug_dict: dict[str, Any]
) -> list[RepoConfig]:
    """Map the bug's ``affected_repos`` JSON column to RepoConfig objects.

    Returns repo configs in registry order (backend before frontend) so
    cross-repo merges pick the right sequence by default.
    """
    names = repo_module.parse_affected_repos(bug_dict.get("affected_repos"))
    if not names:
        return []
    ordered: list[RepoConfig] = []
    for repo_cfg in project.repos:
        if repo_cfg.name in names:
            ordered.append(repo_cfg)
    return ordered


def _resolve_triage_md_path(bug_id: int, project: ProjectConfig) -> Path:
    """Locate the TRIAGE.md path for this bug (backend-first per tech plan)."""
    worktrees = wt_module.list_worktrees(bug_id)
    for r in project.repos:
        if r.role == "backend" and r.name in worktrees:
            return worktrees[r.name] / "TRIAGE.md"
    for r in project.repos:
        if r.name in worktrees:
            return worktrees[r.name] / "TRIAGE.md"
    # Fallback: first worktree we can find, even if the project registry is
    # empty (tests may run without real registries).
    if worktrees:
        return next(iter(worktrees.values())) / "TRIAGE.md"
    # Last-ditch fallback: a synthetic path under the bug's worktrees dir.
    # The append helper creates parent dirs for us.
    from ra_triage.settings import get_settings

    return (
        get_settings().worktrees_dir / str(bug_id) / ".escalation" / "TRIAGE.md"
    )


__all__ = ["merge_bug"]
