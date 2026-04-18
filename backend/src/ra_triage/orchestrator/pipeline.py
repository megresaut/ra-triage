"""Per-bug pipeline driver.

``run_pipeline(bug_id)`` advances a bug through the state machine,
spawning agents as needed. Agent 5 implemented OPEN → TRIAGED →
INVESTIGATING → (BLOCKED | READY_FOR_REVIEW stub). Agent 6 fills in the
single-repo happy path: INVESTIGATING (scope=single-repo) → FIXING →
TESTING → READY_FOR_REVIEW, with BLOCKED transitions on Fixer/Tester
failure and on Tester prod-file safety violations. Agent 10 fills in
the cross-repo path: INVESTIGATING (scope=cross-repo) → DESIGNING →
FIXING_PARALLEL (asyncio.gather) → INTEGRATION_TESTING →
READY_FOR_REVIEW, with one re-design cycle allowed on either a Fixer
contract concern or an Integration Tester failure (capped by
``_DESIGNER_CYCLE_CAP``). The orchestrator runs a best-effort
``pkill -f "port <integration_port>"`` after the Integration Tester
exits, regardless of outcome, as a teardown backstop.

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
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ra_triage.agents.base import AgentResult
from ra_triage.agents.fix_designer import FixDesigner
from ra_triage.agents.fixer import Fixer
from ra_triage.agents.fixer_backend import FixerBackend
from ra_triage.agents.fixer_frontend import FixerFrontend
from ra_triage.agents.integration_tester import IntegrationTester
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
            worktrees=worktrees,
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
    rejection_feedback: str | None = None,
) -> AgentResult:
    """Spawn the Investigator, record its agent_runs row, return the result.

    ``rejection_feedback`` is threaded into the prompt template's
    ``{% if rejection_feedback %}`` block when the caller is the reject
    flow (Agent 8's :func:`reinvestigate_with_feedback`). Forward-path
    runs leave it ``None``.
    """
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
        "rejection_feedback": rejection_feedback,
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
    worktrees: dict[str, Path] | None = None,
) -> None:
    """Decide the next state based on TRIAGE.md + completion token.

    ``worktrees`` is optional — when supplied (the standard forward
    path) it is the dict ``create_worktrees_for_bug`` returned, used by
    the cross-repo driver so it doesn't have to re-discover paths via
    ``wt_module.path_for`` (which is settings-resolved and may not
    match the test fixture's stub paths). When omitted (the reinvestigate
    + retry paths) the cross-repo driver falls back to ``path_for``.
    """
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
        # Persist the structured Escalation row + mirror to Notion BEFORE
        # the state transition so any downstream observer (hook, API) that
        # races the transition already sees the row.
        if escalated:
            await _persist_escalation_for_bug(
                bug_id=bug_id, sections=sections, scope=scope
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
        # Agent 10: real cross-repo path. Drives INVESTIGATING →
        # DESIGNING → FIXING_PARALLEL → INTEGRATION_TESTING → READY_FOR_REVIEW
        # (or BLOCKED on failure / cycle exhaustion). Prefer the
        # worktree dict the caller passed (matches the on-disk paths
        # ``create_worktrees_for_bug`` actually used); fall back to
        # ``list_worktrees`` for callers that didn't have one.
        worktree_map = worktrees if worktrees is not None else wt_module.list_worktrees(bug_id)
        await _run_cross_repo_design_fix_test(
            bug_id=bug_id,
            project=project,
            triage_md_path=triage_md_path,
            worktrees=worktree_map,
        )
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
    escalation_context: str | None = None,
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
    # If we're already in FIXING (e.g. the approve-escalation flow
    # transitioned us here before calling us), don't attempt the
    # redundant transition — the state machine would reject it.
    bug_snapshot = await _load_bug(bug_id)
    if bug_snapshot is None or _is_terminal(bug_snapshot["state"]):
        return
    if BugState(bug_snapshot["state"]) != BugState.FIXING:
        await _transition(bug_id, BugState.FIXING)
        bug_snapshot = await _load_bug(bug_id)
        if bug_snapshot is None or _is_terminal(bug_snapshot["state"]):
            return

    fixer_result = await _run_fixer(
        bug_id=bug_id,
        repo_config=repo_config,
        worktree=worktree,
        triage_md_path=triage_md_path,
        escalation_context=escalation_context,
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
    escalation_context: str | None = None,
) -> AgentResult:
    """Spawn the Fixer, record its agent_runs row, return the result.

    ``escalation_context`` is threaded through into the Fixer prompt
    template's ``{% if escalation_context %}`` block when the caller is
    the approve / pick-alternative flow (Agent 9). Forward-path runs
    leave it ``None``.
    """
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
        # Agent 9 sets ``escalation_context`` via the approve / pick-alt
        # flow. Forward-path runs pass None; the Fixer prompt's
        # ``{% if escalation_context %}`` block degrades cleanly.
        "escalation_context": escalation_context,
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


# --- Cross-repo agent drivers (Agent 10) --------------------------------


async def _run_fix_designer(
    *,
    bug_id: int,
    project: ProjectConfig,
    be_worktree: Path,
    triage_md_path: Path,
    designer_concerns: str | None = None,
) -> AgentResult:
    """Spawn the Fix Designer, record its agent_runs row, return the result.

    The Designer is read-only; we run it in the backend worktree by
    convention so its cwd matches where TRIAGE.md lives. ``repos`` in
    the render context lists BOTH affected repos so the Designer can
    produce a contract that names them correctly.
    """
    attempt = await _next_attempt(bug_id, role="fix_designer")
    started_at = _utcnow()

    run_id = await _insert_agent_run_start(
        bug_id=bug_id,
        role="fix_designer",
        attempt=attempt,
        started_at=started_at,
        log_path=_predicted_log_path(bug_id, "fix_designer", attempt),
        prompt_path=_predicted_prompt_path(bug_id, "fix_designer", attempt),
        worktree_repo=None,
    )

    bug = await _load_bug_row(bug_id)
    if bug is None:
        raise RuntimeError(f"Bug {bug_id} vanished mid-pipeline")

    repos_context = [
        {
            "name": r.name,
            "role": r.role,
            "worktree_path": str(wt_module.path_for(bug_id, r.name)),
        }
        for r in project.repos
    ]

    context: dict[str, Any] = {
        "bug": {
            "id": bug["id"],
            "title": bug["title"],
        },
        "repos": repos_context,
        "triage_md_path": str(triage_md_path),
        "designer_concerns": designer_concerns,
        "attempt": attempt,
    }

    designer = FixDesigner()
    result = await designer.run(
        bug=_BugView(**bug),
        worktree=be_worktree,
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


async def _run_fixers_in_parallel(
    *,
    bug_id: int,
    project: ProjectConfig,
    fe_repo: RepoConfig,
    be_repo: RepoConfig,
    fe_worktree: Path,
    be_worktree: Path,
    triage_md_path: Path,
    escalation_context: str | None = None,
) -> tuple[AgentResult, AgentResult]:
    """Spawn FixerFrontend + FixerBackend concurrently via ``asyncio.gather``.

    Each fixer runs from its own worktree with its own build_command and
    deny-list context. ``agent_runs`` rows are stamped before
    ``asyncio.gather`` so observers see two overlapping in-flight rows.

    ``escalation_context`` is threaded into BOTH render contexts so the
    Agent 9 approve flow keeps working on cross-repo bugs.

    Returns (fe_result, be_result) — order matches the param order, NOT
    completion order.
    """
    fe_attempt = await _next_attempt(bug_id, role="fixer_frontend")
    be_attempt = await _next_attempt(bug_id, role="fixer_backend")

    # Stamp both starts AS CLOSE TOGETHER as we can so the
    # agent_runs.started_at columns reflect the actual parallel start.
    started_at = _utcnow()
    fe_run_id = await _insert_agent_run_start(
        bug_id=bug_id,
        role="fixer_frontend",
        attempt=fe_attempt,
        started_at=started_at,
        log_path=_predicted_log_path(bug_id, "fixer_frontend", fe_attempt),
        prompt_path=_predicted_prompt_path(bug_id, "fixer_frontend", fe_attempt),
        worktree_repo=fe_repo.name,
    )
    be_run_id = await _insert_agent_run_start(
        bug_id=bug_id,
        role="fixer_backend",
        attempt=be_attempt,
        started_at=started_at,
        log_path=_predicted_log_path(bug_id, "fixer_backend", be_attempt),
        prompt_path=_predicted_prompt_path(bug_id, "fixer_backend", be_attempt),
        worktree_repo=be_repo.name,
    )

    bug = await _load_bug_row(bug_id)
    if bug is None:
        raise RuntimeError(f"Bug {bug_id} vanished mid-pipeline")

    fe_context: dict[str, Any] = {
        "bug": {"id": bug["id"], "title": bug["title"]},
        "side": "Frontend",
        "worktree_path": str(fe_worktree),
        "build_command": fe_repo.build_command,
        "deny_list": {
            "paths": list(project.deny_list.paths),
            "categories": list(project.deny_list.categories),
        },
        "triage_md_path": str(triage_md_path),
        "escalation_context": escalation_context,
        "attempt": fe_attempt,
    }
    be_context: dict[str, Any] = {
        "bug": {"id": bug["id"], "title": bug["title"]},
        "side": "Backend",
        "worktree_path": str(be_worktree),
        "build_command": be_repo.build_command,
        "deny_list": {
            "paths": list(project.deny_list.paths),
            "categories": list(project.deny_list.categories),
        },
        "triage_md_path": str(triage_md_path),
        "escalation_context": escalation_context,
        "attempt": be_attempt,
    }

    fixer_fe = FixerFrontend()
    fixer_be = FixerBackend()

    bug_view = _BugView(**bug)

    fe_task = fixer_fe.run(bug=bug_view, worktree=fe_worktree, context=fe_context)
    be_task = fixer_be.run(bug=bug_view, worktree=be_worktree, context=be_context)

    fe_result, be_result = await asyncio.gather(fe_task, be_task)

    await _finalize_agent_run(
        run_id=fe_run_id,
        ended_at=fe_result.ended_at,
        exit_code=fe_result.exit_code,
        completion_token=fe_result.completion_token,
        log_path=fe_result.log_path,
        prompt_path=fe_result.prompt_path,
        triage_md_snapshot=fe_result.triage_md,
    )
    await _finalize_agent_run(
        run_id=be_run_id,
        ended_at=be_result.ended_at,
        exit_code=be_result.exit_code,
        completion_token=be_result.completion_token,
        log_path=be_result.log_path,
        prompt_path=be_result.prompt_path,
        triage_md_snapshot=be_result.triage_md,
    )

    return fe_result, be_result


async def _run_integration_tester(
    *,
    bug_id: int,
    project: ProjectConfig,
    be_repo: RepoConfig,
    fe_repo: RepoConfig,
    be_worktree: Path,
    fe_worktree: Path,
    triage_md_path: Path,
) -> AgentResult:
    """Spawn the Integration Tester from the backend worktree (by convention).

    The render context provides both repos with ``dev_command`` already
    rendered with the integration port substituted, so the Jinja
    template stays literal.
    """
    attempt = await _next_attempt(bug_id, role="integration_tester")
    started_at = _utcnow()

    run_id = await _insert_agent_run_start(
        bug_id=bug_id,
        role="integration_tester",
        attempt=attempt,
        started_at=started_at,
        log_path=_predicted_log_path(bug_id, "integration_tester", attempt),
        prompt_path=_predicted_prompt_path(
            bug_id, "integration_tester", attempt
        ),
        worktree_repo=be_repo.name,
    )

    bug = await _load_bug_row(bug_id)
    if bug is None:
        raise RuntimeError(f"Bug {bug_id} vanished mid-pipeline")

    fe_dev_command = fe_repo.dev_command.replace(
        "{port}", str(fe_repo.integration_port)
    )
    be_dev_command = be_repo.dev_command.replace(
        "{port}", str(be_repo.integration_port)
    )

    fe_context = {
        "worktree_path": str(fe_worktree),
        "dev_command": fe_dev_command,
        "integration_port": fe_repo.integration_port,
    }
    be_context = {
        "worktree_path": str(be_worktree),
        "dev_command": be_dev_command,
        "integration_port": be_repo.integration_port,
    }

    context: dict[str, Any] = {
        "bug": {"id": bug["id"], "title": bug["title"]},
        "fe": fe_context,
        "be": be_context,
        "test_database_url": be_repo.test_database_url or "(not configured)",
        "triage_md_path": str(triage_md_path),
        "attempt": attempt,
    }

    integration = IntegrationTester()
    result = await integration.run(
        bug=_BugView(**bug),
        worktree=be_worktree,
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


async def _run_cross_repo_design_fix_test(
    *,
    bug_id: int,
    project: ProjectConfig,
    triage_md_path: Path,
    worktrees: dict[str, Path] | None = None,
    escalation_context: str | None = None,
) -> None:
    """Drive INVESTIGATING → DESIGNING → FIXING_PARALLEL → INTEGRATION_TESTING → READY_FOR_REVIEW.

    Cross-repo only. The pipeline:

      1. Spawns the FixDesigner; expects ``FIX_DESIGNER_COMPLETE`` AND
         a populated ``## Cross-Repo Contract`` section in TRIAGE.md.
         Failure → BLOCKED with ``## Fix Designer Failure`` if the agent
         didn't already write one.
      2. Spawns FixerFrontend + FixerBackend concurrently (asyncio.gather).
         Both must emit a ``*_COMPLETE`` token.
         If either fixer emits ``*_FAILED`` (or crashes) → BLOCKED.
         If either fixer wrote a ``## Fixer-<Side> Contract Concern``
         section AND we are still on the FIRST design cycle → loop back
         to DESIGNING with the concern text passed via
         ``designer_concerns`` to the re-rendered Fix Designer prompt.
         If we are on the SECOND design cycle → BLOCKED (one re-design
         cap per tech plan).
         ``escalation_context`` (Agent 9 approve flow) is threaded into
         BOTH fixers' render contexts.
      3. Spawns the IntegrationTester; expects
         ``INTEGRATION_TESTER_COMPLETE`` → READY_FOR_REVIEW.
         ``INTEGRATION_TESTER_FAILED`` and we are on the FIRST design
         cycle → loop back to DESIGNING with the failure context.
         ``INTEGRATION_TESTER_FAILED`` on the SECOND cycle → BLOCKED.
         After the tester exits (regardless of outcome), we run a
         best-effort ``pkill -f "port <integration_port>"`` for both
         integration ports as a teardown backstop in case the agent
         left dev servers running.
    """
    # Pick the FE/BE configs out of the project. Cross-repo bugs are
    # validated upstream to have both roles present, but we guard.
    be_repo = _pick_repo_by_role(project, "backend")
    fe_repo = _pick_repo_by_role(project, "frontend")
    if be_repo is None or fe_repo is None:
        logger.warning(
            "Cross-repo pipeline for bug %d but project %s lacks both "
            "frontend and backend roles — routing to BLOCKED",
            bug_id,
            project.id,
        )
        triage_parser.append(
            triage_md_path,
            "Escalation Brief",
            "- **TL;DR:** Cross-repo bug but project lacks both frontend "
            "and backend roles in the registry.\n"
            "- **What I need from you:** Fix the registry or take over manually.",
        )
        await _transition(bug_id, BugState.BLOCKED)
        return

    # Resolve worktree paths. Prefer the caller-supplied dict (the
    # post-investigator route hands us the dict that ``create_worktrees_for_bug``
    # actually produced); fall back to ``path_for`` for callers that
    # don't have it (e.g. Agent 9 approve-flow re-entry through
    # ``run_pipeline_with_escalation_context``).
    worktrees = worktrees or {}
    be_worktree = worktrees.get(be_repo.name) or wt_module.path_for(
        bug_id, be_repo.name
    )
    fe_worktree = worktrees.get(fe_repo.name) or wt_module.path_for(
        bug_id, fe_repo.name
    )

    # Outer loop: at most 2 iterations (initial + one re-design cycle).
    # The second iteration only runs if either Fixer raised a contract
    # concern OR the Integration Tester failed AND we have not yet
    # exceeded the cycle cap. Cycles are counted by inspecting how many
    # ``fix_designer`` agent_runs already exist for this bug.
    while True:
        # ---- DESIGNING ---------------------------------------------------
        bug_snapshot = await _load_bug(bug_id)
        if bug_snapshot is None or _is_terminal(bug_snapshot["state"]):
            return
        designer_cycle = await _count_agent_runs(bug_id, role="fix_designer")
        if designer_cycle >= _DESIGNER_CYCLE_CAP:
            logger.warning(
                "Cross-repo pipeline for bug %d exceeded designer cycle cap "
                "(%d) — routing to BLOCKED",
                bug_id,
                _DESIGNER_CYCLE_CAP,
            )
            triage_parser.append(
                triage_md_path,
                "Cross-Repo Cycle Exhausted",
                f"- After {designer_cycle} Fix Designer runs the contract "
                "still produces concerns or the Integration Tester still "
                "fails. Manual review required.",
            )
            await _transition(bug_id, BugState.BLOCKED)
            return

        # Walk into DESIGNING from whichever state we are in (INVESTIGATING
        # on first pass; FIXING_PARALLEL or INTEGRATION_TESTING on
        # re-design cycles).
        if BugState(bug_snapshot["state"]) != BugState.DESIGNING:
            await _transition(bug_id, BugState.DESIGNING)

        # Determine designer_concerns input for this cycle. On the first
        # cycle this is None; on the re-design cycle it's whatever
        # combined contract concerns / integration failure context the
        # previous iteration accumulated (cached via the closure below).
        designer_concerns = _designer_concerns_holder.get(bug_id)

        designer_result = await _run_fix_designer(
            bug_id=bug_id,
            project=project,
            be_worktree=be_worktree,
            triage_md_path=triage_md_path,
            designer_concerns=designer_concerns,
        )

        sections_after_design = triage_parser.parse(triage_md_path)
        contract_present = bool(
            sections_after_design.get("Cross-Repo Contract", "").strip()
        )
        if (
            designer_result.completion_token != "FIX_DESIGNER_COMPLETE"
            or not contract_present
        ):
            logger.info(
                "Fix Designer for bug %d did not produce a Cross-Repo Contract "
                "(token=%s, contract_present=%s) — BLOCKED",
                bug_id,
                designer_result.completion_token,
                contract_present,
            )
            if not _has_section(sections_after_design, "Fix Designer Failure"):
                triage_parser.append(
                    triage_md_path,
                    "Fix Designer Failure",
                    (
                        "- **Completion token:** "
                        f"{designer_result.completion_token or '(none observed — crash)'}\n"
                        f"- **Exit code:** {designer_result.exit_code}\n"
                        f"- **Contract written:** {contract_present}\n"
                        f"- **Log:** {designer_result.log_path}\n"
                    ),
                )
            _designer_concerns_holder.pop(bug_id, None)
            await _transition(bug_id, BugState.BLOCKED)
            return

        # ---- FIXING_PARALLEL --------------------------------------------
        bug_snapshot = await _load_bug(bug_id)
        if bug_snapshot is None or _is_terminal(bug_snapshot["state"]):
            return
        await _transition(bug_id, BugState.FIXING_PARALLEL)
        bug_snapshot = await _load_bug(bug_id)
        if bug_snapshot is None or _is_terminal(bug_snapshot["state"]):
            return

        fe_result, be_result = await _run_fixers_in_parallel(
            bug_id=bug_id,
            project=project,
            fe_repo=fe_repo,
            be_repo=be_repo,
            fe_worktree=fe_worktree,
            be_worktree=be_worktree,
            triage_md_path=triage_md_path,
            escalation_context=escalation_context,
        )

        # Fail-fast on either fixer FAILED / crashed.
        if fe_result.completion_token != "FIXER_FRONTEND_COMPLETE":
            logger.info(
                "Fixer-Frontend for bug %d did not report success "
                "(token=%s) — BLOCKED",
                bug_id,
                fe_result.completion_token,
            )
            sections_now = triage_parser.parse(triage_md_path)
            if not _has_section(sections_now, "Fixer-Frontend Failure"):
                triage_parser.append(
                    triage_md_path,
                    "Fixer-Frontend Failure",
                    (
                        "- **Completion token:** "
                        f"{fe_result.completion_token or '(none observed — crash)'}\n"
                        f"- **Exit code:** {fe_result.exit_code}\n"
                        f"- **Log:** {fe_result.log_path}\n"
                    ),
                )
            _designer_concerns_holder.pop(bug_id, None)
            await _transition(bug_id, BugState.BLOCKED)
            return
        if be_result.completion_token != "FIXER_BACKEND_COMPLETE":
            logger.info(
                "Fixer-Backend for bug %d did not report success "
                "(token=%s) — BLOCKED",
                bug_id,
                be_result.completion_token,
            )
            sections_now = triage_parser.parse(triage_md_path)
            if not _has_section(sections_now, "Fixer-Backend Failure"):
                triage_parser.append(
                    triage_md_path,
                    "Fixer-Backend Failure",
                    (
                        "- **Completion token:** "
                        f"{be_result.completion_token or '(none observed — crash)'}\n"
                        f"- **Exit code:** {be_result.exit_code}\n"
                        f"- **Log:** {be_result.log_path}\n"
                    ),
                )
            _designer_concerns_holder.pop(bug_id, None)
            await _transition(bug_id, BugState.BLOCKED)
            return

        # Check for Fixer Contract Concerns. Both fixers may have flagged.
        sections_after_fix = triage_parser.parse(triage_md_path)
        fe_concern = sections_after_fix.get(
            "Fixer-Frontend Contract Concern", ""
        ).strip()
        be_concern = sections_after_fix.get(
            "Fixer-Backend Contract Concern", ""
        ).strip()
        if fe_concern or be_concern:
            current_designer_runs = await _count_agent_runs(
                bug_id, role="fix_designer"
            )
            if current_designer_runs >= _DESIGNER_CYCLE_CAP:
                logger.warning(
                    "Cross-repo bug %d hit a contract concern AFTER the "
                    "designer cycle cap — BLOCKED",
                    bug_id,
                )
                _designer_concerns_holder.pop(bug_id, None)
                await _transition(bug_id, BugState.BLOCKED)
                return
            # Loop back to DESIGNING with the concerns aggregated.
            concerns_blob = _format_designer_concerns(
                fe_concern=fe_concern, be_concern=be_concern
            )
            _designer_concerns_holder[bug_id] = concerns_blob
            await _transition(bug_id, BugState.DESIGNING)
            continue

        # ---- INTEGRATION_TESTING ----------------------------------------
        bug_snapshot = await _load_bug(bug_id)
        if bug_snapshot is None or _is_terminal(bug_snapshot["state"]):
            return
        await _transition(bug_id, BugState.INTEGRATION_TESTING)
        bug_snapshot = await _load_bug(bug_id)
        if bug_snapshot is None or _is_terminal(bug_snapshot["state"]):
            return

        integration_result = await _run_integration_tester(
            bug_id=bug_id,
            project=project,
            be_repo=be_repo,
            fe_repo=fe_repo,
            be_worktree=be_worktree,
            fe_worktree=fe_worktree,
            triage_md_path=triage_md_path,
        )

        # Best-effort post-run pkill mop-up for both integration ports.
        # Runs regardless of outcome — the agent's prompt says to tear
        # the servers down, but if it crashed mid-test we still want to
        # free the ports. Logged so operators can confirm what happened.
        await _pkill_integration_ports(
            be_port=be_repo.integration_port,
            fe_port=fe_repo.integration_port,
        )

        if integration_result.completion_token == "INTEGRATION_TESTER_COMPLETE":
            _designer_concerns_holder.pop(bug_id, None)
            await _transition(bug_id, BugState.READY_FOR_REVIEW)
            return

        # Integration FAILED. If we are still under the cycle cap, loop
        # back to DESIGNING with the failure context. Otherwise BLOCKED.
        sections_after_int = triage_parser.parse(triage_md_path)
        failure_body = sections_after_int.get(
            "Integration Tester Failure", ""
        ).strip()
        if not failure_body:
            triage_parser.append(
                triage_md_path,
                "Integration Tester Failure",
                (
                    "- **Completion token:** "
                    f"{integration_result.completion_token or '(none observed — crash)'}\n"
                    f"- **Exit code:** {integration_result.exit_code}\n"
                    f"- **Log:** {integration_result.log_path}\n"
                ),
            )
            failure_body = "(see Integration Tester Failure section)"

        current_designer_runs = await _count_agent_runs(
            bug_id, role="fix_designer"
        )
        if current_designer_runs >= _DESIGNER_CYCLE_CAP:
            logger.warning(
                "Integration Tester failed for bug %d after %d designer cycles — BLOCKED",
                bug_id,
                current_designer_runs,
            )
            _designer_concerns_holder.pop(bug_id, None)
            await _transition(bug_id, BugState.BLOCKED)
            return

        # Loop back to DESIGNING with the integration failure as the
        # concerns input.
        _designer_concerns_holder[bug_id] = (
            "Integration Tester reported failure on the prior cycle:\n"
            + failure_body
        )
        await _transition(bug_id, BugState.DESIGNING)
        # continue


#: Maximum number of Fix Designer runs allowed for one bug. The first
#: run is the initial design; the cap therefore allows ONE additional
#: re-design cycle on either a contract concern or an integration test
#: failure (per the Agent 10 brief).
_DESIGNER_CYCLE_CAP = 2

#: Per-bug holder for ``designer_concerns`` text rendered into the next
#: Fix Designer prompt cycle. Cleared on terminal transitions out of the
#: cross-repo loop. Module-level dict because the loop above and the
#: render helper below are in different call frames; tests can clear it
#: between bugs by calling :func:`_designer_concerns_clear`.
_designer_concerns_holder: dict[int, str] = {}


def _designer_concerns_clear(bug_id: int | None = None) -> None:
    """Test/admin hook to clear the designer-concerns cache.

    With ``bug_id`` clears that bug only; without it clears everything.
    Tests don't normally need this — the loop clears the holder on its
    own terminal exits — but it exists for parametrized re-runs.
    """
    if bug_id is None:
        _designer_concerns_holder.clear()
        return
    _designer_concerns_holder.pop(bug_id, None)


def _format_designer_concerns(*, fe_concern: str, be_concern: str) -> str:
    """Combine FE+BE Contract Concern bodies into a single prompt block."""
    parts: list[str] = []
    if fe_concern:
        parts.append("Frontend Fixer flagged the contract:\n" + fe_concern)
    if be_concern:
        parts.append("Backend Fixer flagged the contract:\n" + be_concern)
    return "\n\n".join(parts)


def _has_section(sections: dict[str, str], name: str) -> bool:
    return bool(sections.get(name, "").strip())


def _pick_repo_by_role(project: ProjectConfig, role: str) -> RepoConfig | None:
    for r in project.repos:
        if r.role == role:
            return r
    return None


async def _count_agent_runs(bug_id: int, *, role: str) -> int:
    """Count agent_runs rows for ``(bug_id, role)``."""
    return await asyncio.to_thread(_count_agent_runs_sync, bug_id, role)


def _count_agent_runs_sync(bug_id: int, role: str) -> int:
    session = engine_module.SessionLocal()
    try:
        runs = repo_module.list_agent_runs(session, bug_id)
        return sum(1 for r in runs if r.agent_role == role)
    finally:
        session.close()


async def _pkill_integration_ports(*, be_port: int, fe_port: int) -> None:
    """Best-effort ``pkill -f`` for both integration ports.

    The Integration Tester agent is responsible for tearing its dev
    servers down per the prompt; this is the orchestrator's backstop in
    case it crashed before the teardown step. We log how many processes
    pkill claims to have signaled (exit 0 = at least one match, exit 1 =
    no matches). We do NOT raise on either: this is housekeeping that
    must never fail the pipeline.
    """
    for port in (be_port, fe_port):
        pattern = f"port {port}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "pkill",
                "-f",
                pattern,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            rc = await asyncio.wait_for(proc.wait(), timeout=5.0)
        except FileNotFoundError:
            logger.debug(
                "pkill not found on PATH; skipping integration teardown for "
                "port %d",
                port,
            )
            continue
        except TimeoutError:
            logger.warning(
                "pkill -f %r timed out (port %d) — leaving any stragglers "
                "for manual cleanup",
                pattern,
                port,
            )
            continue
        if rc == 0:
            logger.info(
                "pkill -f %r (port %d) signaled at least one process",
                pattern,
                port,
            )
        else:
            logger.debug(
                "pkill -f %r (port %d) returned %d (no matches; expected "
                "if the agent already tore down)",
                pattern,
                port,
                rc,
            )


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


async def _persist_escalation_for_bug(
    *,
    bug_id: int,
    sections: dict[str, str],
    scope: dict[str, Any],
) -> None:
    """Record an ``escalations`` row + mirror the brief to Notion.

    Dispatched from ``_route_after_investigator`` when the Investigator
    produced a populated Escalation Brief. Best-effort:

      - DB insert uses a scoped session and swallows failure (the row is
        nice-to-have; if the DB is wedged the bug will still transition
        to BLOCKED and the brief lives in TRIAGE.md).
      - Notion mirror runs via ``append_block`` on the bug's page, so
        Megha sees the full brief in Notion too. Any Notion failure
        logs and is swallowed — product plan explicitly calls this out.
    """
    brief_markdown = sections.get("Escalation Brief", "")
    if not brief_markdown.strip():
        return
    parsed = triage_parser.parse_escalation_brief(sections)
    options_list: list[dict[str, Any]] = []
    if parsed is not None:
        options_list = [asdict(opt) for opt in parsed.options]
    # Trigger classification: deny-list hit vs. judgment call. Mirrors
    # the orchestrator brief's "triggered_rule" contract.
    triggered_rule = "deny_list" if scope.get("deny_list_hit") else "judgment"
    options_json = json.dumps(options_list)

    await asyncio.to_thread(
        _record_escalation_sync,
        bug_id,
        triggered_rule,
        brief_markdown,
        options_json,
    )
    await _mirror_escalation_to_notion(bug_id=bug_id, brief_markdown=brief_markdown)


def _record_escalation_sync(
    bug_id: int,
    triggered_rule: str,
    brief_markdown: str,
    options_json: str,
) -> None:
    session = engine_module.SessionLocal()
    try:
        repo_module.record_escalation(
            session,
            bug_id=bug_id,
            triggered_rule=triggered_rule,
            brief_markdown=brief_markdown,
            options=options_json,
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception(
            "record_escalation failed for bug_id=%s — swallowing so transition "
            "still fires",
            bug_id,
        )
    finally:
        session.close()


async def _mirror_escalation_to_notion(
    *, bug_id: int, brief_markdown: str
) -> None:
    """Append the full Escalation Brief to the bug's Notion page.

    Best-effort; Notion outages or unconfigured credentials never bubble
    out. The parent bug's ``notion_page_id`` drives which page receives
    the block.
    """
    try:
        page_id = await _load_notion_page_id(bug_id)
        if not page_id:
            return
        await asyncio.to_thread(
            _append_escalation_to_notion_sync, page_id, brief_markdown
        )
    except Exception:
        logger.exception(
            "Notion mirror for escalation failed (bug_id=%s) — swallowing",
            bug_id,
        )


def _append_escalation_to_notion_sync(page_id: str, brief_markdown: str) -> None:
    """Append ``## Escalation Brief`` + the body to a Notion page.

    Separate module imports are inside the function body so the pipeline
    module stays importable without notion-client installed / network
    access available (important for test environments).
    """
    try:
        from ra_triage.integrations import notion as notion_module
    except Exception:  # pragma: no cover - defensive
        return
    builder = notion_module._current_client_builder()
    try:
        client = builder()
    except Exception:
        logger.exception(
            "Notion mirror: builder raised for page_id=%s — swallowing", page_id
        )
        return
    if client is None:
        logger.debug(
            "Notion mirror: NotionClient unconfigured, skipping (page_id=%s)",
            page_id,
        )
        return
    markdown = "## Escalation Brief\n\n" + brief_markdown.rstrip() + "\n"
    try:
        client.append_block(page_id=page_id, markdown_text=markdown)
    except Exception:
        logger.exception(
            "Notion mirror: append_block failed (page_id=%s) — swallowing",
            page_id,
        )


async def _load_notion_page_id(bug_id: int) -> str | None:
    return await asyncio.to_thread(_load_notion_page_id_sync, bug_id)


def _load_notion_page_id_sync(bug_id: int) -> str | None:
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.get_bug(session, bug_id)
        if bug is None:
            return None
        return bug.notion_page_id or None
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


# --- Agent 8: Reinvestigate + retry entry points -------------------------


async def reinvestigate_with_feedback(
    bug_id: int, rejection_feedback: str
) -> None:
    """Rerun the Investigator after a human reject.

    Contract:
      - Caller has already transitioned the bug to INVESTIGATING and
        persisted the Rejection row (Agent 8's ``POST /bugs/:id/reject``
        handler does both before spawning this task).
      - We spawn the Investigator with ``rejection_feedback`` rendered
        into the prompt template's ``{% if rejection_feedback %}`` block.
      - On success we route forward exactly like the original Investigator
        run — single-repo bugs enter FIXING, cross-repo go through the
        stub cross-repo path until Agent 10 lands.
      - Never raises: all exceptions land as BLOCKED like
        :func:`run_pipeline`.
    """
    async with concurrency.PIPELINE_SEMAPHORE:
        try:
            await _reinvestigate_body(bug_id, rejection_feedback)
        except Exception:
            logger.exception(
                "reinvestigate_with_feedback crashed for bug_id=%s", bug_id
            )


async def _reinvestigate_body(
    bug_id: int, rejection_feedback: str
) -> None:
    bug_snapshot = await _load_bug(bug_id)
    if bug_snapshot is None:
        logger.warning(
            "reinvestigate: bug_id=%s not found; aborting", bug_id
        )
        return
    if _is_terminal(bug_snapshot["state"]):
        return
    if bug_snapshot["state"] != BugState.INVESTIGATING.value:
        logger.warning(
            "reinvestigate: bug %d not in INVESTIGATING (state=%s); aborting",
            bug_id,
            bug_snapshot["state"],
        )
        return

    project = get_project(bug_snapshot["project_id"])
    worktrees = wt_module.list_worktrees(bug_id)
    if not worktrees:
        # Worktrees were cleaned up (e.g. after a prior CANCELLED + re-open
        # edge case). Recreate them.
        try:
            worktrees = await wt_module.create_worktrees_for_bug(
                bug_id, project.repos
            )
        except wt_module.WorktreeError as exc:
            logger.exception(
                "reinvestigate: worktree recreate failed for bug %d", bug_id
            )
            await _block_with_escalation(
                bug_id,
                project=project,
                reason_title="Worktree recreate failed during reinvestigation",
                reason_body=str(exc),
            )
            return

    triage_md_path = _triage_md_worktree(project, worktrees) / "TRIAGE.md"
    triage_md_path.parent.mkdir(parents=True, exist_ok=True)

    result = await _run_investigator(
        bug_id=bug_id,
        project=project,
        worktrees=worktrees,
        triage_md_path=triage_md_path,
        rejection_feedback=rejection_feedback,
    )

    await _route_after_investigator(
        bug_id=bug_id,
        project=project,
        result=result,
        triage_md_path=triage_md_path,
        worktrees=worktrees,
    )


async def retry_last_agent(bug_id: int) -> None:
    """Re-run the most recent agent_runs row's role with attempt+1.

    Supported starting states (the "working" states + BLOCKED when the
    last run is a ``*_FAILED``):
      INVESTIGATING, FIXING, TESTING, DESIGNING, FIXING_PARALLEL,
      INTEGRATION_TESTING, BLOCKED.

    The caller (Agent 8's ``/retry`` endpoint) validates the state before
    spawning this task; here we re-verify and refuse with
    :class:`ValueError` if the pipeline has since moved on.
    """
    async with concurrency.PIPELINE_SEMAPHORE:
        await _retry_last_agent_body(bug_id)


async def _retry_last_agent_body(bug_id: int) -> None:
    bug_snapshot = await _load_bug(bug_id)
    if bug_snapshot is None:
        raise ValueError(f"bug {bug_id} not found")
    state = BugState(bug_snapshot["state"])
    if state not in _RETRY_ELIGIBLE_STATES:
        raise ValueError(
            f"bug {bug_id} is in {state.value}; retry is not allowed from this state"
        )
    latest = await _load_latest_agent_run(bug_id)
    if latest is None:
        raise ValueError(f"bug {bug_id} has no agent_runs to retry")

    role = latest["agent_role"]
    if state == BugState.BLOCKED:
        # BLOCKED is only eligible if the latest run is a *_FAILED token —
        # otherwise we'd accidentally re-run agents on escalation bugs.
        tok = latest.get("completion_token") or ""
        if not tok.endswith("_FAILED"):
            raise ValueError(
                f"bug {bug_id} is BLOCKED but latest agent_run "
                f"(role={role!r}, token={tok!r}) is not a *_FAILED — "
                "retry is only supported for explicit failures"
            )

    project = get_project(bug_snapshot["project_id"])
    worktrees = wt_module.list_worktrees(bug_id)
    if not worktrees:
        try:
            worktrees = await wt_module.create_worktrees_for_bug(
                bug_id, project.repos
            )
        except wt_module.WorktreeError as exc:
            await _block_with_escalation(
                bug_id,
                project=project,
                reason_title="Worktree recreate failed during retry",
                reason_body=str(exc),
            )
            return

    triage_md_path = _triage_md_worktree(project, worktrees) / "TRIAGE.md"

    if role == "investigator":
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
            worktrees=worktrees,
        )
        return

    if role == "fixer":
        # Ensure state is FIXING before re-running the Fixer. If we were
        # in BLOCKED, transition back via FIXING.
        await _transition_for_retry(bug_id, BugState.FIXING)
        sections = triage_parser.parse(triage_md_path)
        scope = triage_parser.get_scope(sections)
        picked = _pick_single_affected_repo(project, scope)
        if not isinstance(picked, RepoConfig):
            # Can't pick a single repo — bail back to BLOCKED with a brief.
            await _transition(bug_id, BugState.BLOCKED)
            return
        repo_config = picked
        worktree = wt_module.path_for(bug_id, repo_config.name)
        result = await _run_fixer(
            bug_id=bug_id,
            repo_config=repo_config,
            worktree=worktree,
            triage_md_path=triage_md_path,
        )
        if result.completion_token == "FIXER_COMPLETE":
            # Continue the single-repo pipeline forward: → TESTING → RFR.
            await _transition(bug_id, BugState.TESTING)
            return
        # Still failed — back to BLOCKED.
        await _transition(bug_id, BugState.BLOCKED)
        return

    if role == "tester":
        await _transition_for_retry(bug_id, BugState.TESTING)
        sections = triage_parser.parse(triage_md_path)
        scope = triage_parser.get_scope(sections)
        picked = _pick_single_affected_repo(project, scope)
        if not isinstance(picked, RepoConfig):
            await _transition(bug_id, BugState.BLOCKED)
            return
        repo_config = picked
        worktree = wt_module.path_for(bug_id, repo_config.name)
        result = await _run_tester(
            bug_id=bug_id,
            repo_config=repo_config,
            worktree=worktree,
            triage_md_path=triage_md_path,
        )
        if result.completion_token == "TESTER_COMPLETE":
            await _transition(bug_id, BugState.READY_FOR_REVIEW)
            return
        await _transition(bug_id, BugState.BLOCKED)
        return

    # Cross-repo roles land here until Agent 10 ships.
    raise ValueError(
        f"retry for agent role {role!r} is not yet implemented "
        "(Agent 10 fills cross-repo)"
    )


#: States from which :func:`retry_last_agent` will accept a retry request.
_RETRY_ELIGIBLE_STATES: frozenset[BugState] = frozenset(
    {
        BugState.INVESTIGATING,
        BugState.DESIGNING,
        BugState.FIXING,
        BugState.FIXING_PARALLEL,
        BugState.TESTING,
        BugState.INTEGRATION_TESTING,
        BugState.BLOCKED,
    }
)


async def _walk_to_designing_for_approve(bug_id: int) -> bool:
    """Walk a FIXING_PARALLEL bug to DESIGNING via legal transitions.

    Used by :func:`run_pipeline_with_escalation_context` when the API
    handler has already moved BLOCKED → FIXING_PARALLEL but the
    cross-repo driver wants to re-enter from DESIGNING (so the Designer
    can revise the contract with the reviewer's escalation context in
    mind). Returns ``True`` if we end up in DESIGNING, ``False`` if the
    bug was cancelled or the transition path was illegal.
    """
    bug_snapshot = await _load_bug(bug_id)
    if bug_snapshot is None or _is_terminal(bug_snapshot["state"]):
        return False
    current = BugState(bug_snapshot["state"])
    if current == BugState.DESIGNING:
        return True
    if current == BugState.FIXING_PARALLEL:
        # FIXING_PARALLEL → DESIGNING is a legal transition per the
        # state machine (TRANSITIONS in states.py).
        await _transition(bug_id, BugState.DESIGNING)
        bug_snapshot = await _load_bug(bug_id)
        return (
            bug_snapshot is not None
            and not _is_terminal(bug_snapshot["state"])
            and BugState(bug_snapshot["state"]) == BugState.DESIGNING
        )
    logger.warning(
        "_walk_to_designing_for_approve: unexpected starting state %s for "
        "bug %d",
        current.value,
        bug_id,
    )
    return False


async def _transition_for_retry(bug_id: int, target: BugState) -> None:
    """Walk the bug into ``target`` via a legal transition if possible.

    From BLOCKED we go BLOCKED → FIXING (or FIXING_PARALLEL); from any
    other state we only transition if the current state isn't already
    ``target`` (idempotent).
    """
    bug_snapshot = await _load_bug(bug_id)
    if bug_snapshot is None:
        return
    current = BugState(bug_snapshot["state"])
    if current == target:
        return
    if current == BugState.BLOCKED and target in (
        BugState.FIXING,
        BugState.FIXING_PARALLEL,
    ):
        await _transition(bug_id, target)
        return
    if current == BugState.FIXING and target == BugState.TESTING:
        await _transition(bug_id, target)
        return
    # Other paths: the caller guaranteed eligibility — we might not have a
    # direct legal edge (e.g. TESTING → TESTING). Nothing to do.
    return


async def _load_latest_agent_run(bug_id: int) -> dict[str, Any] | None:
    """Return the latest agent_runs row as a plain dict, or None if none exist."""
    return await asyncio.to_thread(_load_latest_agent_run_sync, bug_id)


def _load_latest_agent_run_sync(bug_id: int) -> dict[str, Any] | None:
    session = engine_module.SessionLocal()
    try:
        runs = repo_module.list_agent_runs(session, bug_id)
        if not runs:
            return None
        latest = runs[-1]
        return {
            "id": latest.id,
            "agent_role": latest.agent_role,
            "attempt": latest.attempt,
            "completion_token": latest.completion_token,
            "exit_code": latest.exit_code,
        }
    finally:
        session.close()


# --- Agent 9: Approve / Pick-alternative entry point ---------------------


async def run_pipeline_with_escalation_context(
    bug_id: int, escalation_context: str
) -> None:
    """Drive a BLOCKED bug forward after the reviewer picks an option.

    Contract (Agent 9):

      - Caller has already transitioned the bug FROM ``BLOCKED`` TO the
        appropriate working state (``FIXING`` for single-repo,
        ``FIXING_PARALLEL`` for cross-repo) and recorded the escalation
        resolution on the ``escalations`` row.
      - We spawn the Fixer with ``escalation_context`` rendered into the
        prompt template's ``{% if escalation_context %}`` block, then
        continue forward exactly like the Agent 6 / Agent 10 happy path
        (FIXING → TESTING → READY_FOR_REVIEW for single-repo).
      - Never raises: exceptions are caught and logged like
        :func:`run_pipeline`.

    The ``escalation_context`` payload is a human-readable prose summary
    of the brief + the reviewer's choice. The API handler assembles it
    from the parsed brief and any notes the reviewer attached.
    """
    async with concurrency.PIPELINE_SEMAPHORE:
        try:
            await _run_with_escalation_context_body(bug_id, escalation_context)
        except Exception:
            logger.exception(
                "run_pipeline_with_escalation_context crashed for bug_id=%s",
                bug_id,
            )


async def _run_with_escalation_context_body(
    bug_id: int, escalation_context: str
) -> None:
    bug_snapshot = await _load_bug(bug_id)
    if bug_snapshot is None:
        logger.warning(
            "run_pipeline_with_escalation_context: bug_id=%s not found", bug_id
        )
        return
    if _is_terminal(bug_snapshot["state"]):
        return

    state = BugState(bug_snapshot["state"])
    if state not in (BugState.FIXING, BugState.FIXING_PARALLEL):
        logger.warning(
            "run_pipeline_with_escalation_context: bug %d not in FIXING/"
            "FIXING_PARALLEL (state=%s); aborting",
            bug_id,
            state.value,
        )
        return

    project = get_project(bug_snapshot["project_id"])
    worktrees = wt_module.list_worktrees(bug_id)
    if not worktrees:
        # Worktrees may have been cleaned up if the bug previously
        # cycled through a terminal state; recreate them.
        try:
            worktrees = await wt_module.create_worktrees_for_bug(
                bug_id, project.repos
            )
        except wt_module.WorktreeError as exc:
            logger.exception(
                "run_pipeline_with_escalation_context: worktree recreate "
                "failed for bug %d",
                bug_id,
            )
            await _block_with_escalation(
                bug_id,
                project=project,
                reason_title="Worktree recreate failed during escalation approval",
                reason_body=str(exc),
            )
            return

    triage_md_path = _triage_md_worktree(project, worktrees) / "TRIAGE.md"

    if state == BugState.FIXING:
        # Single-repo approve flow: re-run the Fixer with escalation
        # context, then continue forward via the standard single-repo
        # happy path.
        sections = triage_parser.parse(triage_md_path)
        scope = triage_parser.get_scope(sections)
        await _run_single_repo_fix_and_test(
            bug_id=bug_id,
            project=project,
            scope=scope,
            triage_md_path=triage_md_path,
            escalation_context=escalation_context,
        )
        return

    # FIXING_PARALLEL (cross-repo approve flow). Re-run the cross-repo
    # driver but starting from the FIXING_PARALLEL transition the API
    # endpoint already made — the driver re-enters DESIGNING via the
    # transition sequence below to keep the contract aligned. We thread
    # ``escalation_context`` so both fixers see the reviewer's choice.
    #
    # Subtle: the API handler transitioned BLOCKED → FIXING_PARALLEL.
    # The cross-repo driver expects DESIGNING as its entry point, so we
    # walk FIXING_PARALLEL → DESIGNING via a legal transition (it is
    # legal per states.TRANSITIONS) before invoking the loop. This also
    # gives the Designer a chance to revise the contract in light of the
    # human's chosen alternative.
    if not await _walk_to_designing_for_approve(bug_id):
        return
    await _run_cross_repo_design_fix_test(
        bug_id=bug_id,
        project=project,
        triage_md_path=triage_md_path,
        worktrees=worktrees,
        escalation_context=escalation_context,
    )


__all__ = [
    "reinvestigate_with_feedback",
    "retry_last_agent",
    "run_pipeline",
    "run_pipeline_with_escalation_context",
]
