"""Per-bug pipeline driver.

``run_pipeline(bug_id)`` advances a bug through the state machine,
spawning agents as needed. Agent 5 implemented OPEN → TRIAGED →
INVESTIGATING → (BLOCKED | READY_FOR_REVIEW stub). Agent 6 fills in the
single-repo happy path: INVESTIGATING (scope=single-repo) → FIXING →
TESTING → READY_FOR_REVIEW, with BLOCKED transitions on Fixer/Tester
failure and on Tester prod-file safety violations. Agent 10 fills in
the cross-repo path.

Key invariants:
  - Every state transition goes through ``db.repo.update_bug_state``
    (validates via the state machine, fires the Notion write-back hook).
    Never write ``bug.state = ...`` directly from here.
  - Every agent invocation is bracketed by an ``agent_runs`` row:
    insert with ``started_at=now()``, then update with the completion
    details once ``agent.run`` returns.
  - Before each transition, re-load the bug so we observe any concurrent
    cancellation. If the bug is now terminal or CANCELLED, return early.
  - Concurrency is bounded by :data:`orchestrator.concurrency.PIPELINE_SEMAPHORE`.

Worktree strategy for TRIAGE.md: cross-repo bugs still converge on ONE
TRIAGE.md. We pick the backend repo's worktree as the canonical home;
single-repo bugs naturally use that one. If no repo has ``role=="backend"``
we fall back to the first configured repo.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ra_triage.agents.base import AgentResult
from ra_triage.agents.fixer import Fixer
from ra_triage.agents.investigator import Investigator
from ra_triage.agents.tester import Tester
from ra_triage.config.registry import ProjectConfig, RepoConfig, get_project
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.db.models import AgentRun, Bug
from ra_triage.orchestrator import concurrency, tester_safety
from ra_triage.orchestrator.states import (
    TERMINAL_STATES,
    BugState,
    IllegalBugStateTransition,
)
from ra_triage.triage_md import parser as triage_parser
from ra_triage.workspaces import worktree as wt_module

logger = logging.getLogger(__name__)


# --- Public entry point --------------------------------------------------


async def run_pipeline(bug_id: int) -> None:
    """Drive a single bug through the pipeline.

    Never raises: all failures are logged and translated into a BLOCKED
    transition (when the current state permits) so bugs don't get stuck
    in phantom states. The caller schedules this via
    ``asyncio.create_task(run_pipeline(bug_id))``.
    """
    async with concurrency.PIPELINE_SEMAPHORE:
        try:
            await _run_pipeline_body(bug_id)
        except Exception:
            logger.exception(
                "run_pipeline crashed for bug_id=%s — check agent_runs for state",
                bug_id,
            )


async def _run_pipeline_body(bug_id: int) -> None:
    """Pipeline body — runs with the semaphore held."""
    bug_snapshot = await _load_bug(bug_id)
    if bug_snapshot is None:
        logger.warning("run_pipeline: bug_id=%s not found; aborting", bug_id)
        return
    if _is_terminal(bug_snapshot["state"]):
        logger.info(
            "run_pipeline: bug_id=%s already terminal (%s); aborting",
            bug_id,
            bug_snapshot["state"],
        )
        return

    project = get_project(bug_snapshot["project_id"])

    # Step: OPEN → TRIAGED
    if bug_snapshot["state"] == BugState.OPEN.value:
        await _transition(bug_id, BugState.TRIAGED)
        bug_snapshot = await _load_bug(bug_id)
        if bug_snapshot is None or _is_terminal(bug_snapshot["state"]):
            return

    # Step: TRIAGED → INVESTIGATING (with worktree creation + Investigator)
    if bug_snapshot["state"] == BugState.TRIAGED.value:
        try:
            worktrees = await wt_module.create_worktrees_for_bug(
                bug_id, project.repos
            )
        except wt_module.DirtyCanonicalCloneError as exc:
            await _block_with_escalation(
                bug_id,
                project=project,
                reason_title="Dirty canonical clone",
                reason_body=str(exc),
            )
            return
        except wt_module.WorktreeError as exc:
            logger.exception("Worktree setup failed for bug %d", bug_id)
            await _block_with_escalation(
                bug_id,
                project=project,
                reason_title="Worktree setup failed",
                reason_body=str(exc),
            )
            return

        triage_md_path = _triage_md_worktree(project, worktrees) / "TRIAGE.md"
        # Create an empty TRIAGE.md so the Investigator has something to
        # append to if the agent prefers append-only writes.
        triage_md_path.parent.mkdir(parents=True, exist_ok=True)
        if not triage_md_path.exists():
            triage_md_path.write_text("", encoding="utf-8")

        await _transition(bug_id, BugState.INVESTIGATING)
        bug_snapshot = await _load_bug(bug_id)
        if bug_snapshot is None or _is_terminal(bug_snapshot["state"]):
            return

        result = await _run_investigator(
            bug_id=bug_id,
            project=project,
            worktrees=worktrees,
            triage_md_path=triage_md_path,
        )

        await _route_after_investigator(
            bug_id=bug_id,
            project=project,
            result=result,
            triage_md_path=triage_md_path,
        )
        return

    if bug_snapshot["state"] == BugState.INVESTIGATING.value:
        # Resume logic is Agent 12's scope. If we see a bug already in
        # INVESTIGATING here, that means a prior run left it hanging.
        # For THIS PR we log and exit; Agent 12 will re-spawn.
        logger.info(
            "run_pipeline: bug_id=%s already INVESTIGATING; leaving for Agent 12 resume",
            bug_id,
        )
        return

    # Any other state means a later-agent responsibility (FIXING, TESTING,
    # DESIGNING, FIXING_PARALLEL, INTEGRATION_TESTING, READY_FOR_REVIEW,
    # BLOCKED, MERGING). Surface a clear error marker.
    raise NotImplementedError(
        f"pipeline step {bug_snapshot['state']} belongs to a later agent "
        f"(bug_id={bug_id})"
    )


# --- Investigator run + routing -----------------------------------------


async def _run_investigator(
    *,
    bug_id: int,
    project: ProjectConfig,
    worktrees: dict[str, Path],
    triage_md_path: Path,
) -> AgentResult:
    """Spawn the Investigator, record its agent_runs row, return the result."""
    home_worktree = _triage_md_worktree(project, worktrees)
    attempt = await _next_attempt(bug_id, role="investigator")
    started_at = _utcnow()

    # Pre-insert the agent_runs row so operators can observe an in-flight
    # run. The post-run update below back-fills the completion fields.
    run_id = await _insert_agent_run_start(
        bug_id=bug_id,
        role="investigator",
        attempt=attempt,
        started_at=started_at,
        # Base class also writes these; we compute the paths in advance
        # so the DB row is populated from the outset.
        log_path=_predicted_log_path(bug_id, "investigator", attempt),
        prompt_path=_predicted_prompt_path(bug_id, "investigator", attempt),
    )

    bug = await _load_bug_row(bug_id)
    if bug is None:
        raise RuntimeError(f"Bug {bug_id} vanished mid-pipeline")

    repos_context = [
        {
            "name": r.name,
            "role": r.role,
            "worktree_path": str(worktrees.get(r.name, "")),
        }
        for r in project.repos
    ]

    context: dict[str, Any] = {
        "bug": {
            "id": bug["id"],
            "title": bug["title"],
            "description": bug["description"],
            "repro_steps": bug["repro_steps"] or "(none)",
            "severity": bug["severity"] or "(unspecified)",
            "reporter": bug["reporter"] or "(unspecified)",
        },
        "project": {"display_name": project.display_name},
        "repos": repos_context,
        "deny_list": {
            "paths": list(project.deny_list.paths),
            "categories": list(project.deny_list.categories),
        },
        "rejection_feedback": None,
        "triage_md_path": str(triage_md_path),
        "attempt": attempt,
    }

    investigator = Investigator()

    # The base class returns an AgentResult wrapping the subprocess outcome.
    # We deliberately run the subprocess from the TRIAGE.md home worktree —
    # for single-repo it matches that repo; for cross-repo the Investigator
    # can still Read files in other worktrees via absolute paths from the
    # context (``repos[*].worktree_path``).
    result = await investigator.run(
        bug=_BugView(**bug),
        worktree=home_worktree,
        context=context,
    )

    await _finalize_agent_run(
        run_id=run_id,
        ended_at=result.ended_at,
        exit_code=result.exit_code,
        completion_token=result.completion_token,
        log_path=result.log_path,
        prompt_path=result.prompt_path,
        triage_md_snapshot=result.triage_md,
    )

    return result


async def _route_after_investigator(
    *,
    bug_id: int,
    project: ProjectConfig,
    result: AgentResult,
    triage_md_path: Path,
) -> None:
    """Decide the next state based on TRIAGE.md + completion token."""
    sections = triage_parser.parse(triage_md_path)
    scope = triage_parser.get_scope(sections)

    # Persist scope + affected_repos + deny_list_hit on the bug regardless
    # of next transition. These columns drive the frontend queue view.
    await _persist_scope(bug_id, scope)

    completion_token = result.completion_token
    escalated = triage_parser.has_escalation(sections)

    if completion_token == "INVESTIGATOR_FAILED" or escalated:
        logger.info(
            "Investigator routed bug %d to BLOCKED (token=%s, escalation=%s)",
            bug_id,
            completion_token,
            escalated,
        )
        await _transition(bug_id, BugState.BLOCKED)
        return

    if scope["scope"] == "single-repo":
        # Agent 6: real single-repo happy path. Drives INVESTIGATING →
        # FIXING → TESTING → READY_FOR_REVIEW (or BLOCKED on failure).
        await _run_single_repo_fix_and_test(
            bug_id=bug_id,
            project=project,
            scope=scope,
            triage_md_path=triage_md_path,
        )
        return

    if scope["scope"] == "cross-repo":
        logger.warning(
            "Agent 5 stub: cross-repo happy path not yet implemented — "
            "marking bug %d READY_FOR_REVIEW directly (Agent 10 fills in DESIGNING → "
            "FIXING_PARALLEL → INTEGRATION_TESTING)",
            bug_id,
        )
        await _stub_advance_through_cross_repo(bug_id)
        return

    # Malformed / missing scope — treat as BLOCKED with a brief.
    logger.warning(
        "Investigator produced malformed or missing Scope for bug %d; routing to BLOCKED",
        bug_id,
    )
    triage_parser.append(
        triage_md_path,
        "Escalation Brief",
        "- **TL;DR:** Investigator's Scope section was missing or malformed; "
        "human review required.\n"
        "- **Why blocked:** Orchestrator cannot decide single-repo vs cross-repo "
        "without a parseable Scope block.\n"
        "- **What I need from you:** Inspect TRIAGE.md and either re-run the "
        "Investigator or take over manually.",
    )
    await _transition(bug_id, BugState.BLOCKED)


async def _run_single_repo_fix_and_test(
    *,
    bug_id: int,
    project: ProjectConfig,
    scope: dict[str, Any],
    triage_md_path: Path,
) -> None:
    """Drive INVESTIGATING → FIXING → TESTING → READY_FOR_REVIEW (or BLOCKED).

    Single-repo only. Picks the single affected repo from the
    Investigator's Scope section, runs the Fixer, then the Tester. On
    any failure (Fixer failed/escalated, Tester failed, Tester
    produced a prod-code modification that would have violated the
    test-files-only constraint), the bug lands in BLOCKED with the
    relevant diagnostic section appended to TRIAGE.md.
    """
    # Resolve the single affected repo. If the Investigator listed zero
    # repos or more than one, bail to BLOCKED with a clear reason —
    # cross-repo is Agent 10's scope.
    picked = _pick_single_affected_repo(project, scope)
    if picked is None:
        triage_parser.append(
            triage_md_path,
            "Escalation Brief",
            "- **TL;DR:** Investigator's Scope section listed no affected "
            "repos; cannot proceed with single-repo fix.\n"
            "- **What I need from you:** Re-investigate or take over manually.",
        )
        await _transition(bug_id, BugState.BLOCKED)
        return
    if picked == _MULTIPLE_REPOS_SENTINEL:
        triage_parser.append(
            triage_md_path,
            "Escalation Brief",
            "- **TL;DR:** Investigator produced scope=single-repo but listed "
            "multiple affected repos; routing the bug requires cross-repo "
            "support (Agent 10).\n"
            "- **What I need from you:** Re-investigate with a scope that "
            "matches the repo list, or take over manually.",
        )
        await _transition(bug_id, BugState.BLOCKED)
        return

    # Narrow type for mypy: picked is now definitely a RepoConfig.
    assert isinstance(picked, RepoConfig)
    repo_config: RepoConfig = picked
    worktree = wt_module.path_for(bug_id, repo_config.name)

    # ---- FIXING ---------------------------------------------------
    await _transition(bug_id, BugState.FIXING)
    bug_snapshot = await _load_bug(bug_id)
    if bug_snapshot is None or _is_terminal(bug_snapshot["state"]):
        return

    fixer_result = await _run_fixer(
        bug_id=bug_id,
        repo_config=repo_config,
        worktree=worktree,
        triage_md_path=triage_md_path,
    )

    # Post-Fixer routing. We check both the completion token and the
    # presence of a ``## Fixer Escalation`` section — the Fixer may have
    # stopped without printing FIXER_FAILED if it decided the fix
    # required a deny-list path change.
    sections_after_fix = triage_parser.parse(triage_md_path)
    if _has_fixer_escalation(sections_after_fix):
        logger.info(
            "Fixer for bug %d raised escalation — routing to BLOCKED", bug_id
        )
        await _transition(bug_id, BugState.BLOCKED)
        return

    if fixer_result.completion_token != "FIXER_COMPLETE":
        logger.info(
            "Fixer for bug %d did not report success (token=%s) — BLOCKED",
            bug_id,
            fixer_result.completion_token,
        )
        # If the agent crashed without writing a ## Fixer Failure
        # section, append one so the UI has context.
        if not _has_fixer_failure(sections_after_fix):
            triage_parser.append(
                triage_md_path,
                "Fixer Failure",
                (
                    "- **Completion token:** "
                    f"{fixer_result.completion_token or '(none observed — crash)'}\n"
                    f"- **Exit code:** {fixer_result.exit_code}\n"
                    f"- **Log:** {fixer_result.log_path}\n"
                ),
            )
        await _transition(bug_id, BugState.BLOCKED)
        return

    # ---- TESTING --------------------------------------------------
    bug_snapshot = await _load_bug(bug_id)
    if bug_snapshot is None or _is_terminal(bug_snapshot["state"]):
        return
    await _transition(bug_id, BugState.TESTING)
    bug_snapshot = await _load_bug(bug_id)
    if bug_snapshot is None or _is_terminal(bug_snapshot["state"]):
        return

    # Capture the post-Fixer baseline as ``{path: content_hash}``. The
    # safety check compares each post-Tester path's hash against this
    # map: files the Tester didn't modify are skipped, files the Tester
    # changed (or created new) are inspected. Without this, the Fixer's
    # own uncommitted prod edits would trip the "Tester wrote prod
    # code" guard.
    pre_tester_baseline = await tester_safety.snapshot_working_tree(worktree)

    tester_result = await _run_tester(
        bug_id=bug_id,
        repo_config=repo_config,
        worktree=worktree,
        triage_md_path=triage_md_path,
    )

    # Post-Tester safety check: revert any prod-file modifications and
    # block if one was detected. Runs BEFORE we interpret the completion
    # token because a violation always wins — we want to keep the
    # orchestrator-side guarantee that the Tester's branch contains only
    # test-file additions (and the Fixer's pre-existing changes).
    safety = await tester_safety.check_and_revert_prod_changes(
        worktree, baseline=pre_tester_baseline
    )
    if safety.had_violation:
        logger.warning(
            "Tester for bug %d modified %d prod file(s); reverting and BLOCKING",
            bug_id,
            len(safety.reverted_paths),
        )
        _append_tester_safety_violation(triage_md_path, safety)
        await _transition(bug_id, BugState.BLOCKED)
        return

    if tester_result.completion_token == "TESTER_COMPLETE":
        await _transition(bug_id, BugState.READY_FOR_REVIEW)
        return

    # TESTER_FAILED or crash.
    logger.info(
        "Tester for bug %d did not report success (token=%s) — BLOCKED",
        bug_id,
        tester_result.completion_token,
    )
    sections_after_test = triage_parser.parse(triage_md_path)
    if not _has_tester_failure(sections_after_test):
        triage_parser.append(
            triage_md_path,
            "Tester Failure",
            (
                "- **Completion token:** "
                f"{tester_result.completion_token or '(none observed — crash)'}\n"
                f"- **Exit code:** {tester_result.exit_code}\n"
                f"- **Log:** {tester_result.log_path}\n"
            ),
        )
    await _transition(bug_id, BugState.BLOCKED)


# --- Fixer / Tester agent drivers ---------------------------------------


async def _run_fixer(
    *,
    bug_id: int,
    repo_config: RepoConfig,
    worktree: Path,
    triage_md_path: Path,
) -> AgentResult:
    """Spawn the Fixer, record its agent_runs row, return the result."""
    attempt = await _next_attempt(bug_id, role="fixer")
    started_at = _utcnow()

    run_id = await _insert_agent_run_start(
        bug_id=bug_id,
        role="fixer",
        attempt=attempt,
        started_at=started_at,
        log_path=_predicted_log_path(bug_id, "fixer", attempt),
        prompt_path=_predicted_prompt_path(bug_id, "fixer", attempt),
        worktree_repo=repo_config.name,
    )

    bug = await _load_bug_row(bug_id)
    if bug is None:
        raise RuntimeError(f"Bug {bug_id} vanished mid-pipeline")

    project = get_project(bug["project_id"])

    context: dict[str, Any] = {
        "bug": {
            "id": bug["id"],
            "title": bug["title"],
        },
        "worktree_path": str(worktree),
        "build_command": repo_config.build_command,
        "deny_list": {
            "paths": list(project.deny_list.paths),
            "categories": list(project.deny_list.categories),
        },
        # Agent 9 will set ``escalation_context`` via the approve flow.
        # Keep the key present (None) so StrictUndefined doesn't trip.
        "escalation_context": None,
        "attempt": attempt,
    }

    fixer = Fixer()
    result = await fixer.run(
        bug=_BugView(**bug),
        worktree=worktree,
        context=context,
    )

    await _finalize_agent_run(
        run_id=run_id,
        ended_at=result.ended_at,
        exit_code=result.exit_code,
        completion_token=result.completion_token,
        log_path=result.log_path,
        prompt_path=result.prompt_path,
        triage_md_snapshot=result.triage_md,
    )

    return result


async def _run_tester(
    *,
    bug_id: int,
    repo_config: RepoConfig,
    worktree: Path,
    triage_md_path: Path,
) -> AgentResult:
    """Spawn the Tester, record its agent_runs row, return the result."""
    attempt = await _next_attempt(bug_id, role="tester")
    started_at = _utcnow()

    run_id = await _insert_agent_run_start(
        bug_id=bug_id,
        role="tester",
        attempt=attempt,
        started_at=started_at,
        log_path=_predicted_log_path(bug_id, "tester", attempt),
        prompt_path=_predicted_prompt_path(bug_id, "tester", attempt),
        worktree_repo=repo_config.name,
    )

    bug = await _load_bug_row(bug_id)
    if bug is None:
        raise RuntimeError(f"Bug {bug_id} vanished mid-pipeline")

    context: dict[str, Any] = {
        "bug": {
            "id": bug["id"],
            "title": bug["title"],
        },
        "worktree_path": str(worktree),
        "test_command": repo_config.test_command,
        "test_database_url": repo_config.test_database_url or "(not configured)",
        "triage_md_path": str(triage_md_path),
        "attempt": attempt,
    }

    tester = Tester()
    result = await tester.run(
        bug=_BugView(**bug),
        worktree=worktree,
        context=context,
    )

    await _finalize_agent_run(
        run_id=run_id,
        ended_at=result.ended_at,
        exit_code=result.exit_code,
        completion_token=result.completion_token,
        log_path=result.log_path,
        prompt_path=result.prompt_path,
        triage_md_snapshot=result.triage_md,
    )

    return result


# --- Routing helpers ----------------------------------------------------


#: Sentinel returned by :func:`_pick_single_affected_repo` when the Scope
#: section listed more than one affected repo (which is inconsistent with
#: ``scope: single-repo`` but we guard against it).
_MULTIPLE_REPOS_SENTINEL = "__multiple__"


def _pick_single_affected_repo(
    project: ProjectConfig, scope: dict[str, Any]
) -> RepoConfig | str | None:
    """Resolve the single repo the Fixer should target.

    Returns:
      - a ``RepoConfig`` on exactly one match against the project registry.
      - the string :data:`_MULTIPLE_REPOS_SENTINEL` if the Scope section
        lists more than one repo.
      - ``None`` on zero / unknown repo names.
    """
    affected = [str(r).strip() for r in scope.get("affected_repos", []) if str(r).strip()]
    if not affected:
        # Fall back to the project's single repo if unambiguous.
        if len(project.repos) == 1:
            return project.repos[0]
        return None
    if len(affected) > 1:
        return _MULTIPLE_REPOS_SENTINEL
    name = affected[0]
    for repo in project.repos:
        if repo.name == name:
            return repo
    return None


def _has_fixer_failure(sections: dict[str, str]) -> bool:
    return bool(sections.get("Fixer Failure", "").strip())


def _has_fixer_escalation(sections: dict[str, str]) -> bool:
    return bool(sections.get("Fixer Escalation", "").strip())


def _has_tester_failure(sections: dict[str, str]) -> bool:
    return bool(sections.get("Tester Failure", "").strip())


def _append_tester_safety_violation(
    triage_md_path: Path,
    safety: tester_safety.SafetyCheckResult,
) -> None:
    """Write a ``## Tester Safety Violation`` section summarizing the revert."""
    lines: list[str] = [
        "The Tester modified production code (not allowed by the test-files-only "
        "constraint). The orchestrator reverted the offending files below so the "
        "branch contains only test-file additions. The bug is BLOCKED; re-run the "
        "Tester or take over manually.",
        "",
        "**Reverted prod files:**",
    ]
    if safety.reverted_paths:
        for p in safety.reverted_paths:
            lines.append(f"- `{p}`")
    else:
        lines.append("- (none reverted successfully)")
    if safety.revert_errors:
        lines.append("")
        lines.append("**Revert errors (file still dirty):**")
        for path, err in safety.revert_errors:
            lines.append(f"- `{path}`: {err}")
    if safety.test_paths:
        lines.append("")
        lines.append("**Test-file changes kept:**")
        for p in safety.test_paths:
            lines.append(f"- `{p}`")
    triage_parser.append(triage_md_path, "Tester Safety Violation", "\n".join(lines))


async def _stub_advance_through_cross_repo(bug_id: int) -> None:
    """Stub for Agent 10: advance INVESTIGATING → ... → READY_FOR_REVIEW (cross-repo)."""
    await _transition(bug_id, BugState.DESIGNING)
    await _transition(bug_id, BugState.FIXING_PARALLEL)
    await _transition(bug_id, BugState.INTEGRATION_TESTING)
    await _transition(bug_id, BugState.READY_FOR_REVIEW)


# --- DB helpers (all sync work dispatched to threadpool) -----------------


def _is_terminal(state_value: str) -> bool:
    try:
        return BugState(state_value) in TERMINAL_STATES
    except ValueError:
        return False


async def _load_bug(bug_id: int) -> dict[str, Any] | None:
    """Load a bug snapshot as a plain dict (JSON-serializable, loop-safe)."""
    return await asyncio.to_thread(_load_bug_sync, bug_id)


async def _load_bug_row(bug_id: int) -> dict[str, Any] | None:
    """Alias for ``_load_bug`` — used where the caller wants to emphasize
    a fresh re-load (e.g. right before an agent spawn)."""
    return await _load_bug(bug_id)


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
        "repro_steps": bug.repro_steps,
        "severity": bug.severity,
        "reporter": bug.reporter,
        "state": bug.state,
        "scope": bug.scope,
        "affected_repos": bug.affected_repos,
        "deny_list_hit": bug.deny_list_hit,
        "rejection_count": bug.rejection_count,
    }


class _BugView:
    """Lightweight duck-typed bug for ``ClaudeCodeAgent.run``.

    The base class only reads ``bug.id``; everything else flows via the
    context dict rendered into the prompt. We keep a tiny value object
    here rather than reaching into the SQLAlchemy session across threads.
    """

    def __init__(self, **fields: Any) -> None:
        for k, v in fields.items():
            setattr(self, k, v)


async def _transition(bug_id: int, new_state: BugState) -> None:
    """Transition the bug, re-loading first to respect cancellation.

    Never raises :class:`IllegalBugStateTransition` out: if the bug has
    already been cancelled or is otherwise in a state that can't reach
    ``new_state``, we log and return. This matches the "respect cancel"
    requirement from Agent 5's brief.
    """
    await asyncio.to_thread(_transition_sync, bug_id, new_state)


def _transition_sync(bug_id: int, new_state: BugState) -> None:
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.get_bug(session, bug_id)
        if bug is None:
            logger.warning("transition: bug_id=%s disappeared; skipping", bug_id)
            return
        if BugState(bug.state) in TERMINAL_STATES:
            logger.info(
                "transition: bug_id=%s already terminal (%s); skipping %s",
                bug_id,
                bug.state,
                new_state.value,
            )
            return
        try:
            repo_module.update_bug_state(session, bug, new_state)
            session.commit()
        except IllegalBugStateTransition as exc:
            session.rollback()
            logger.warning(
                "transition: illegal %s → %s for bug_id=%s: %s",
                bug.state,
                new_state.value,
                bug_id,
                exc,
            )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def _next_attempt(bug_id: int, *, role: str) -> int:
    return await asyncio.to_thread(_next_attempt_sync, bug_id, role)


def _next_attempt_sync(bug_id: int, role: str) -> int:
    session = engine_module.SessionLocal()
    try:
        runs = repo_module.list_agent_runs(session, bug_id)
        existing = [r.attempt for r in runs if r.agent_role == role]
        return (max(existing) + 1) if existing else 1
    finally:
        session.close()


async def _insert_agent_run_start(
    *,
    bug_id: int,
    role: str,
    attempt: int,
    started_at: datetime,
    log_path: Path,
    prompt_path: Path,
    worktree_repo: str | None = None,
) -> int:
    return await asyncio.to_thread(
        _insert_agent_run_start_sync,
        bug_id,
        role,
        attempt,
        started_at,
        str(log_path),
        str(prompt_path),
        worktree_repo,
    )


def _insert_agent_run_start_sync(
    bug_id: int,
    role: str,
    attempt: int,
    started_at: datetime,
    log_path: str,
    prompt_path: str,
    worktree_repo: str | None,
) -> int:
    session = engine_module.SessionLocal()
    try:
        run = repo_module.record_agent_run(
            session,
            bug_id=bug_id,
            agent_role=role,
            attempt=attempt,
            started_at=started_at,
            log_path=log_path,
            prompt_path=prompt_path,
            worktree_repo=worktree_repo,
        )
        session.commit()
        return int(run.id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def _finalize_agent_run(
    *,
    run_id: int,
    ended_at: datetime,
    exit_code: int,
    completion_token: str | None,
    log_path: Path,
    prompt_path: Path,
    triage_md_snapshot: str,
) -> None:
    await asyncio.to_thread(
        _finalize_agent_run_sync,
        run_id,
        ended_at,
        exit_code,
        completion_token,
        str(log_path),
        str(prompt_path),
        triage_md_snapshot,
    )


def _finalize_agent_run_sync(
    run_id: int,
    ended_at: datetime,
    exit_code: int,
    completion_token: str | None,
    log_path: str,
    prompt_path: str,
    triage_md_snapshot: str,
) -> None:
    session = engine_module.SessionLocal()
    try:
        run = session.get(AgentRun, run_id)
        if run is None:
            logger.warning("finalize_agent_run: run_id=%s not found", run_id)
            return
        run.ended_at = ended_at
        run.exit_code = exit_code
        run.completion_token = completion_token
        run.log_path = log_path
        run.prompt_path = prompt_path
        run.triage_md_snapshot = triage_md_snapshot
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def _persist_scope(bug_id: int, scope: dict[str, Any]) -> None:
    await asyncio.to_thread(_persist_scope_sync, bug_id, scope)


def _persist_scope_sync(bug_id: int, scope: dict[str, Any]) -> None:
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.get_bug(session, bug_id)
        if bug is None:
            return
        bug.scope = scope["scope"]  # may be None — acceptable
        bug.affected_repos = json.dumps(scope["affected_repos"])
        bug.deny_list_hit = bool(scope["deny_list_hit"])
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def _block_with_escalation(
    bug_id: int,
    *,
    project: ProjectConfig,
    reason_title: str,
    reason_body: str,
) -> None:
    """Write an Escalation Brief to TRIAGE.md and transition to BLOCKED.

    Used when the pipeline itself hits a pre-Investigator failure
    (DirtyCanonicalClone, worktree setup). State order: current state →
    (TRIAGED only if still OPEN) → INVESTIGATING → BLOCKED.
    """
    # TRIAGE.md for bug even if worktree creation failed: write under the
    # worktrees dir even if the repo worktree itself is missing — Agent 12's
    # resume path will still see it.
    triage_md_path = _fallback_triage_md_path(bug_id, project)
    triage_md_path.parent.mkdir(parents=True, exist_ok=True)

    triage_parser.append(
        triage_md_path,
        "Escalation Brief",
        f"- **TL;DR:** {reason_title}.\n\n{reason_body}",
    )

    # Walk whatever state we're in into BLOCKED via legal transitions.
    bug_snapshot = await _load_bug(bug_id)
    if bug_snapshot is None:
        return
    current = BugState(bug_snapshot["state"])
    if current == BugState.OPEN:
        await _transition(bug_id, BugState.TRIAGED)
        current = BugState.TRIAGED
    if current == BugState.TRIAGED:
        await _transition(bug_id, BugState.INVESTIGATING)
        current = BugState.INVESTIGATING
    if current == BugState.INVESTIGATING:
        await _transition(bug_id, BugState.BLOCKED)


# --- Path helpers --------------------------------------------------------


def _triage_md_worktree(
    project: ProjectConfig, worktrees: dict[str, Path]
) -> Path:
    """Pick the worktree that hosts TRIAGE.md for this bug.

    Preference: first repo with ``role == "backend"``, fall back to first
    configured repo. Cross-repo bugs converge here even though their work
    spans multiple worktrees.
    """
    preferred: RepoConfig | None = None
    for r in project.repos:
        if r.role == "backend":
            preferred = r
            break
    if preferred is None and project.repos:
        preferred = project.repos[0]
    if preferred is None:
        raise RuntimeError(
            f"Project {project.id} has no repos; cannot pick TRIAGE.md worktree"
        )
    path = worktrees.get(preferred.name)
    if path is None:
        # Caller didn't create a worktree for the preferred repo — try the
        # first available one instead.
        if worktrees:
            return next(iter(worktrees.values()))
        raise RuntimeError(
            f"No worktrees available for bug; preferred={preferred.name}"
        )
    return path


def _fallback_triage_md_path(bug_id: int, project: ProjectConfig) -> Path:
    """Compute a TRIAGE.md path when worktrees could not be created.

    We still need SOMEWHERE to persist the Escalation Brief explaining
    why the pipeline stalled. We put it at
    ``<worktrees_root>/<bug_id>/.escalation/TRIAGE.md`` — this dir is
    never a git worktree, so the resume path can tell it apart.
    """
    from ra_triage.settings import get_settings

    settings = get_settings()
    return settings.worktrees_dir / str(bug_id) / ".escalation" / "TRIAGE.md"


def _predicted_log_path(bug_id: int, role: str, attempt: int) -> Path:
    from ra_triage.settings import get_settings

    return get_settings().logs_dir / str(bug_id) / f"{role}.{attempt}.log"


def _predicted_prompt_path(bug_id: int, role: str, attempt: int) -> Path:
    from ra_triage.settings import get_settings

    return get_settings().prompts_dir / str(bug_id) / f"{role}.{attempt}.md"


# --- Misc ----------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


__all__ = ["run_pipeline"]
