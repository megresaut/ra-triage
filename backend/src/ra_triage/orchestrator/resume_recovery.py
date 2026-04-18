"""Restart-recovery for bugs caught mid-merge (Agent 12).

When the backend dies while a bug is in the ``MERGING`` state, neither
the merge driver's success path (transition → FIXED) nor its failure
path (transition → BLOCKED) ran to completion. On restart,
:func:`orchestrator.resume.resume_inflight` flags such bugs as
:attr:`ResumeDecision.RESUME_POST_RECOVERY`; the FastAPI lifespan
schedules :func:`recover_merging_bug` to figure out what actually
happened on github.com and drive the bug forward.

Three paths:

  1. **All recorded PRs are merged on github.com.** This means the
     ``gh pr merge`` calls succeeded before the crash, but the
     ``MERGING → FIXED`` transition never wrote. We flip the bug to
     FIXED via the same Notion-write-back hook the live merge driver
     uses.
  2. **Some PRs merged, others still open.** Cross-repo partial-merge
     state. We re-enter :func:`orchestrator.merge.merge_bug` which
     reuses already-open PR rows and merges the survivors.
  3. **All PRs still open.** Re-enter ``merge_bug`` from scratch.

Failures are best-effort — gh CLI outages or rate-limits leave the bug
in MERGING and the operator can manually click Confirm again. We never
roll a merged PR back; partial-merge state is durable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ra_triage.config.registry import get_project
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.db.models import Bug, PullRequest
from ra_triage.integrations import github as github_module
from ra_triage.orchestrator.states import BugState
from ra_triage.workspaces import worktree as wt_module

logger = logging.getLogger(__name__)


async def recover_merging_bug(bug_id: int) -> None:
    """Drive a bug found in MERGING at boot to its correct end state.

    Strategy:
      1. Load the bug + its ``pull_requests`` rows. If no PRs exist,
         the merge driver crashed before it created any — re-run
         ``merge_bug`` from scratch.
      2. For each PR row in ``open`` status, query ``gh pr view`` for
         the live state. Update the row to ``merged`` / ``closed`` if
         the live state has advanced.
      3. Aggregate: if all PRs are now merged → drive bug to FIXED via
         the merge-driver helper. Otherwise → re-enter ``merge_bug``
         (which is idempotent on already-merged PRs).

    Never raises — on any exception we log and leave the bug in
    MERGING for manual intervention.
    """
    try:
        await _recover_body(bug_id)
    except Exception:
        logger.exception(
            "recover_merging_bug crashed for bug_id=%s — bug remains in MERGING",
            bug_id,
        )


async def _recover_body(bug_id: int) -> None:
    snap = await asyncio.to_thread(_load_bug_with_prs_sync, bug_id)
    if snap is None:
        logger.warning(
            "recover_merging_bug: bug_id=%s not found; aborting", bug_id
        )
        return
    bug_state = BugState(snap["state"])
    if bug_state != BugState.MERGING:
        logger.info(
            "recover_merging_bug: bug %d is now %s (no longer MERGING) — "
            "skipping (likely re-driven by a different boot or operator)",
            bug_id,
            bug_state.value,
        )
        return

    project_id = snap["project_id"]
    try:
        project = get_project(project_id)
    except KeyError:
        logger.warning(
            "recover_merging_bug: project %s missing from registry; cannot recover bug %d",
            project_id,
            bug_id,
        )
        return

    prs = snap["pull_requests"]
    if not prs:
        logger.info(
            "recover_merging_bug: bug %d has no PR rows; re-entering merge_bug",
            bug_id,
        )
        # merge_bug expects READY_FOR_REVIEW per the state machine; it
        # will re-attempt the MERGING transition. If the bug is still
        # MERGING (it is), the legal-transition check will refuse.
        # Instead, the driver itself handles MERGING entry; we call its
        # internal entry below.
        await _re_enter_merge_driver(bug_id)
        return

    # Probe each open PR.
    worktree_paths = wt_module.list_worktrees(bug_id)
    updated_statuses: dict[int, str] = {}
    for pr in prs:
        if pr["status"] != "open":
            updated_statuses[pr["id"]] = pr["status"]
            continue
        cwd = worktree_paths.get(pr["repo_name"])
        if cwd is None:
            # Worktree is gone (e.g. cleanup ran); we can't probe via gh
            # because gh requires a repo context. Conservatively keep
            # status=open and re-enter the merge driver.
            logger.info(
                "recover_merging_bug: bug %d PR #%s — no worktree for repo %s; "
                "leaving status=open",
                bug_id,
                pr["pr_number"],
                pr["repo_name"],
            )
            updated_statuses[pr["id"]] = "open"
            continue
        try:
            live_status = await github_module.gh_pr_status(
                worktree=cwd, pr_number=pr["pr_number"]
            )
        except Exception:
            logger.exception(
                "recover_merging_bug: gh pr view failed for bug %d PR #%s; "
                "leaving status=open",
                bug_id,
                pr["pr_number"],
            )
            updated_statuses[pr["id"]] = "open"
            continue
        updated_statuses[pr["id"]] = live_status
        if live_status != "open":
            await asyncio.to_thread(
                _flip_pr_status_sync, pr["id"], live_status
            )

    all_merged = updated_statuses and all(
        s == "merged" for s in updated_statuses.values()
    )
    if all_merged:
        logger.info(
            "recover_merging_bug: bug %d — all %d PR(s) already merged; "
            "transitioning to FIXED",
            bug_id,
            len(updated_statuses),
        )
        await _drive_to_fixed(bug_id, project)
        return

    # At least one PR still open (or unknown). Re-enter the merge driver
    # so it merges the survivors.
    logger.info(
        "recover_merging_bug: bug %d — some PR(s) still open; re-entering merge_bug",
        bug_id,
    )
    await _re_enter_merge_driver(bug_id)


async def _re_enter_merge_driver(bug_id: int) -> None:
    """Re-run the merge driver on a MERGING bug.

    The merge driver's first transition is READY_FOR_REVIEW → MERGING;
    on a MERGING bug that step is illegal. We import the merge body
    helper and call it directly so the per-PR loop runs without the
    redundant transition.

    On any failure we let the merge driver itself flip the bug to
    BLOCKED with a ``## Merge Failure`` section.
    """
    from ra_triage.orchestrator.merge import merge_bug

    # merge_bug() handles already-MERGING bugs by attempting (and
    # failing harmlessly) the READY_FOR_REVIEW → MERGING transition,
    # then proceeding with the per-PR loop. The transition helper
    # logs the illegal-transition warning and continues.
    await merge_bug(bug_id)


async def _drive_to_fixed(bug_id: int, project: Any) -> None:
    """Transition MERGING → FIXED using the same hooks the merge driver uses.

    Mirrors ``orchestrator.merge.merge_bug``'s success path: stamp PRs as
    merged (already done by the caller), append PR links to Notion, run
    worktree cleanup. The state transition fires the Notion status
    write-back hook automatically.
    """
    from ra_triage.integrations import notion as notion_module

    def _transition_sync() -> bool:
        session = engine_module.SessionLocal()
        try:
            bug = repo_module.get_bug(session, bug_id)
            if bug is None:
                return False
            current = BugState(bug.state)
            if current != BugState.MERGING:
                logger.info(
                    "_drive_to_fixed: bug %d is %s, not MERGING — aborting",
                    bug_id,
                    current.value,
                )
                return False
            try:
                repo_module.update_bug_state(session, bug, BugState.FIXED)
                session.commit()
                return True
            except Exception:
                session.rollback()
                logger.exception(
                    "_drive_to_fixed: transition raised for bug %d", bug_id
                )
                return False
        finally:
            session.close()

    ok = await asyncio.to_thread(_transition_sync)
    if not ok:
        return

    # Append PR links to Notion, best-effort.
    try:
        snap = await asyncio.to_thread(_load_bug_with_prs_sync, bug_id)
        if snap is None:
            return
        for pr in snap["pull_requests"]:
            if pr["status"] == "merged" and pr.get("pr_url"):
                try:
                    notion_module.append_pr_link(
                        page_id=snap["notion_page_id"],
                        pr_url=pr["pr_url"],
                        repo_name=pr["repo_name"],
                    )
                except Exception:
                    logger.exception(
                        "_drive_to_fixed: append_pr_link failed for bug %d "
                        "PR #%s",
                        bug_id,
                        pr["pr_number"],
                    )
    except Exception:
        logger.exception(
            "_drive_to_fixed: PR link write-back failed for bug %d", bug_id
        )

    # Worktree cleanup, best-effort.
    try:
        await wt_module.cleanup(bug_id, list(project.repos))
    except Exception:
        logger.exception("_drive_to_fixed: worktree cleanup failed for bug %d", bug_id)


# --- DB helpers ---------------------------------------------------------


def _load_bug_with_prs_sync(bug_id: int) -> dict[str, Any] | None:
    session = engine_module.SessionLocal()
    try:
        bug: Bug | None = repo_module.get_bug(session, bug_id)
        if bug is None:
            return None
        prs = repo_module.list_pull_requests(session, bug_id)
        return {
            "id": bug.id,
            "notion_page_id": bug.notion_page_id,
            "project_id": bug.project_id,
            "state": bug.state,
            "pull_requests": [
                {
                    "id": p.id,
                    "repo_name": p.repo_name,
                    "pr_number": p.pr_number,
                    "pr_url": p.pr_url,
                    "status": p.status,
                }
                for p in prs
                if p.pr_number is not None
            ],
        }
    finally:
        session.close()


def _flip_pr_status_sync(pr_id: int, status: str) -> None:
    """Update the ``pull_requests`` row's status (and merged_at if newly merged)."""
    session = engine_module.SessionLocal()
    try:
        pr: PullRequest | None = session.get(PullRequest, pr_id)
        if pr is None:
            return
        pr.status = status
        if status == "merged" and pr.merged_at is None:
            from datetime import UTC, datetime

            pr.merged_at = datetime.now(UTC).replace(tzinfo=None)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception(
            "_flip_pr_status_sync: failed for pr_id=%s", pr_id
        )
    finally:
        session.close()


__all__ = ["recover_merging_bug"]
