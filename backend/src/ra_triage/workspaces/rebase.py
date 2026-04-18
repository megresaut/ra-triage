"""On-merge rebase sweeper.

When a bug transitions to FIXED (its PR has merged), every other bug
currently in-flight against the same repo is holding a worktree that
predates the newly-merged commit. This module automatically:

  1. ``git fetch origin <main_branch>`` in the in-flight worktree, then
  2. ``git rebase origin/<main_branch>``.

Clean rebase → the bug continues with zero state change (its branch now
sits on top of the new main). Conflicting rebase → the rebase is
aborted, the conflict file list is parsed out of ``git status
--porcelain``, the bug flips to BLOCKED, and ``bugs.conflict_files`` is
populated so the frontend's conflict panel surfaces the files + offers
the reviewer two actions (retry Fixer with conflict as context, or take
over manually).

Every attempt — clean, conflict, or error — writes a row into
``rebase_attempts`` for audit. Network or git failures (``result='error'``)
do NOT block the bug; the next merge event will retry.

Edge cases handled:
  - Worktree path gone (bug cancelled mid-flight): log + skip.
  - ``git fetch`` failure (network): record ``result='error'`` + continue,
    do NOT transition BLOCKED.
  - Canonical clone path invalid: caught by the fetch step.
  - Simultaneous sweeps (two bugs FIX in rapid succession) are safe —
    each sweep operates on a different trigger_bug_id and attempts are
    all recorded; the first sweep to reach BLOCKED wins the state
    transition.

Subprocess strategy mirrors ``workspaces.worktree``: ``asyncio.create_subprocess_exec``
with a bounded per-command timeout. We duplicate a lightweight ``_run_git``
helper here rather than export worktree.py's internal — it's tuned for
``check=False`` / conflict-aware return codes, which the worktree helper
raises on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ra_triage.config.registry import ProjectConfig, RepoConfig
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.triage_md import parser as triage_parser
from ra_triage.workspaces import worktree as wt_module

logger = logging.getLogger(__name__)

# Bounded timeout per git command — fetch against a local canonical clone
# should be near-instant, and a rebase that's still running after 2
# minutes on a worktree this small is almost certainly hung.
_GIT_TIMEOUT_SECONDS = 120.0


# --- public dataclasses -----------------------------------------------------


RebaseOutcome = Literal["clean", "conflict", "error"]


@dataclass(frozen=True)
class RebaseResult:
    """Outcome of one ``rebase_in_flight`` attempt on one bug.

    Returned by :func:`rebase_in_flight` (one per bug per repo). Callers
    typically only care about the audit row + state transition the
    sweeper already performed; the dataclass is primarily for tests and
    logging.
    """

    bug_id: int
    repo_name: str
    result: RebaseOutcome
    conflict_files: list[str]  # empty unless result == "conflict"
    error_message: str | None  # set on "error"


# --- public entry points ----------------------------------------------------


async def rebase_in_flight(
    trigger_bug_id: int,
    repo_name: str,
    project: ProjectConfig,
) -> list[RebaseResult]:
    """For every in-flight bug touching ``repo_name`` (excluding ``trigger_bug_id``):

    - Resolve the bug's worktree via :func:`workspaces.worktree.path_for`.
    - ``git fetch origin <main_branch>`` in the worktree.
    - ``git rebase origin/<main_branch>``.
      - exit 0 → record ``result='clean'``.
      - non-zero → parse ``UU`` lines from ``git status --porcelain`` for
        the conflict file list, ``git rebase --abort``, record
        ``result='conflict'``, flip the bug to BLOCKED via
        :func:`db.repo.mark_blocked_with_conflict`, append a
        ``## Merge Conflict`` section to TRIAGE.md.
    - A fetch failure (network) or any other pre-rebase failure records
      ``result='error'`` and leaves the bug's state alone — the next
      merge event will retry.

    Returns the list of :class:`RebaseResult` — one per in-flight bug
    touching ``repo_name``, in the order we processed them.

    Never raises: every per-bug failure is caught and converted into a
    result entry so the orchestrator's top-level ``asyncio.create_task``
    never observes an exception.
    """
    repo_cfg = _repo_config(project, repo_name)
    if repo_cfg is None:
        logger.warning(
            "rebase_in_flight: repo %r not in project %r registry — skipping",
            repo_name,
            project.id,
        )
        return []

    try:
        bugs = await asyncio.to_thread(
            _list_inflight_sync, repo_name, trigger_bug_id
        )
    except Exception:
        logger.exception(
            "rebase_in_flight: failed to load in-flight bugs for repo %r "
            "(trigger=%d) — skipping sweep",
            repo_name,
            trigger_bug_id,
        )
        return []

    results: list[RebaseResult] = []
    for bug_view in bugs:
        result = await _rebase_one_bug(
            bug_id=bug_view.id,
            repo_cfg=repo_cfg,
            trigger_bug_id=trigger_bug_id,
        )
        results.append(result)
    return results


async def fetch_conflict_diff(
    bug_id: int,
    repo_name: str,
    project: ProjectConfig,
) -> str:
    """Return a unified diff snapshot for a BLOCKED-with-conflict bug.

    Runs ``git diff origin/<main_branch>...HEAD`` in the bug's worktree
    for ``repo_name``. The ``...`` (three-dot) form gives the diff of
    changes introduced on the bug's branch since it diverged from main,
    which is exactly the context the Fixer needs to see when re-engaging.

    Returns an empty string if the worktree is gone or git fails — the
    frontend degrades to "no diff snapshot available" without raising.
    """
    repo_cfg = _repo_config(project, repo_name)
    if repo_cfg is None:
        return ""
    worktree = wt_module.path_for(bug_id, repo_name)
    if not worktree.is_dir():
        logger.info(
            "fetch_conflict_diff: worktree gone for bug=%d repo=%s",
            bug_id,
            repo_name,
        )
        return ""

    # Fetch first so origin/<main_branch> reflects the post-merge state.
    # Best-effort: a failed fetch just means we diff against a possibly
    # stale ref, which is better than returning nothing.
    await _run_git(
        ["fetch", "origin", repo_cfg.main_branch],
        cwd=worktree,
        check=False,
    )
    diff = await _run_git(
        [
            "diff",
            "--no-color",
            f"origin/{repo_cfg.main_branch}...HEAD",
        ],
        cwd=worktree,
        check=False,
    )
    if diff.returncode != 0:
        logger.info(
            "fetch_conflict_diff: git diff exited %d for bug=%d repo=%s: %s",
            diff.returncode,
            bug_id,
            repo_name,
            diff.stderr.strip(),
        )
        return ""
    return diff.stdout


# --- per-bug orchestration --------------------------------------------------


async def _rebase_one_bug(
    *,
    bug_id: int,
    repo_cfg: RepoConfig,
    trigger_bug_id: int,
) -> RebaseResult:
    """Execute the rebase protocol for a single bug; record the audit row.

    Catch-all try/except so a single failure doesn't abort the sweep.
    """
    try:
        return await _rebase_one_bug_body(
            bug_id=bug_id,
            repo_cfg=repo_cfg,
            trigger_bug_id=trigger_bug_id,
        )
    except Exception as exc:
        logger.exception(
            "rebase: unexpected failure for bug=%d repo=%s (trigger=%d); "
            "recording as 'error'",
            bug_id,
            repo_cfg.name,
            trigger_bug_id,
        )
        await asyncio.to_thread(
            _record_attempt_sync,
            bug_id,
            repo_cfg.name,
            trigger_bug_id,
            "error",
            None,
        )
        return RebaseResult(
            bug_id=bug_id,
            repo_name=repo_cfg.name,
            result="error",
            conflict_files=[],
            error_message=str(exc),
        )


async def _rebase_one_bug_body(
    *,
    bug_id: int,
    repo_cfg: RepoConfig,
    trigger_bug_id: int,
) -> RebaseResult:
    worktree = wt_module.path_for(bug_id, repo_cfg.name)
    if not worktree.is_dir():
        logger.info(
            "rebase: worktree gone for bug=%d repo=%s; skipping",
            bug_id,
            repo_cfg.name,
        )
        await asyncio.to_thread(
            _record_attempt_sync,
            bug_id,
            repo_cfg.name,
            trigger_bug_id,
            "error",
            None,
        )
        return RebaseResult(
            bug_id=bug_id,
            repo_name=repo_cfg.name,
            result="error",
            conflict_files=[],
            error_message=f"worktree {worktree} does not exist",
        )

    main_ref = f"origin/{repo_cfg.main_branch}"

    # --- fetch ---------------------------------------------------------
    fetch_res = await _run_git(
        ["fetch", "origin", repo_cfg.main_branch],
        cwd=worktree,
        check=False,
    )
    if fetch_res.returncode != 0:
        msg = fetch_res.stderr.strip() or "git fetch failed"
        logger.warning(
            "rebase: git fetch failed for bug=%d repo=%s (trigger=%d): %s",
            bug_id,
            repo_cfg.name,
            trigger_bug_id,
            msg,
        )
        await asyncio.to_thread(
            _record_attempt_sync,
            bug_id,
            repo_cfg.name,
            trigger_bug_id,
            "error",
            None,
        )
        return RebaseResult(
            bug_id=bug_id,
            repo_name=repo_cfg.name,
            result="error",
            conflict_files=[],
            error_message=msg,
        )

    # --- rebase --------------------------------------------------------
    rebase_res = await _run_git(
        ["rebase", main_ref],
        cwd=worktree,
        check=False,
    )
    if rebase_res.returncode == 0:
        logger.info(
            "rebase: clean for bug=%d repo=%s (trigger=%d)",
            bug_id,
            repo_cfg.name,
            trigger_bug_id,
        )
        await asyncio.to_thread(
            _record_attempt_sync,
            bug_id,
            repo_cfg.name,
            trigger_bug_id,
            "clean",
            None,
        )
        return RebaseResult(
            bug_id=bug_id,
            repo_name=repo_cfg.name,
            result="clean",
            conflict_files=[],
            error_message=None,
        )

    # Non-zero → likely conflict. Collect the conflicted file list before
    # aborting (abort clears the index so we must parse --porcelain
    # first).
    conflict_files = await _collect_conflict_files(worktree)

    # Abort the rebase so the worktree returns to a clean state. Best-effort.
    abort_res = await _run_git(
        ["rebase", "--abort"],
        cwd=worktree,
        check=False,
    )
    if abort_res.returncode != 0:
        logger.warning(
            "rebase: --abort failed for bug=%d repo=%s: %s",
            bug_id,
            repo_cfg.name,
            abort_res.stderr.strip(),
        )

    if not conflict_files:
        # Rebase failed but --porcelain gave us nothing — treat as error.
        # This can happen on e.g. corrupt index or permissions issues. We
        # don't want to BLOCK a bug spuriously without a file list.
        msg = (
            rebase_res.stderr.strip()
            or rebase_res.stdout.strip()
            or "git rebase failed without conflict files"
        )
        logger.warning(
            "rebase: non-zero with no UU files for bug=%d repo=%s: %s",
            bug_id,
            repo_cfg.name,
            msg,
        )
        await asyncio.to_thread(
            _record_attempt_sync,
            bug_id,
            repo_cfg.name,
            trigger_bug_id,
            "error",
            None,
        )
        return RebaseResult(
            bug_id=bug_id,
            repo_name=repo_cfg.name,
            result="error",
            conflict_files=[],
            error_message=msg,
        )

    # --- conflict ------------------------------------------------------
    logger.info(
        "rebase: CONFLICT for bug=%d repo=%s (trigger=%d) files=%s",
        bug_id,
        repo_cfg.name,
        trigger_bug_id,
        conflict_files,
    )
    await asyncio.to_thread(
        _record_attempt_sync,
        bug_id,
        repo_cfg.name,
        trigger_bug_id,
        "conflict",
        conflict_files,
    )
    # Append a ## Merge Conflict section to TRIAGE.md BEFORE we flip the
    # bug to BLOCKED so WS subscribers that refetch TRIAGE on the state
    # change observe the new section already written.
    _append_conflict_section(
        bug_id=bug_id,
        repo_name=repo_cfg.name,
        trigger_bug_id=trigger_bug_id,
        conflict_files=conflict_files,
    )
    await asyncio.to_thread(
        _mark_blocked_sync,
        bug_id,
        repo_cfg.name,
        conflict_files,
    )
    return RebaseResult(
        bug_id=bug_id,
        repo_name=repo_cfg.name,
        result="conflict",
        conflict_files=conflict_files,
        error_message=None,
    )


# --- helpers ---------------------------------------------------------------


async def _collect_conflict_files(worktree: Path) -> list[str]:
    """Parse ``git status --porcelain`` for ``UU`` lines.

    ``UU <path>`` marks an unmerged path with both sides modified during
    a rebase/merge. We ALSO accept ``DD``/``AU``/``UA``/``DU``/``UD``/``AA``
    (any line whose first two status chars contain a ``U``) because a
    cross-file rename or add/add conflict surfaces those codes too.
    """
    res = await _run_git(
        ["status", "--porcelain"],
        cwd=worktree,
        check=False,
    )
    if res.returncode != 0:
        return []
    out: list[str] = []
    for raw_line in res.stdout.splitlines():
        # Porcelain v1 format: XY <space> path. XY is 2 chars.
        if len(raw_line) < 4:
            continue
        x, y = raw_line[0], raw_line[1]
        if "U" not in (x, y):
            continue
        # Trim the 3-char prefix (XY + space) and strip any quoting from
        # git for paths with spaces.
        path = raw_line[3:].strip()
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if path:
            out.append(path)
    # Deterministic ordering so tests don't flake on filesystem iteration.
    return sorted(set(out))


def _append_conflict_section(
    *,
    bug_id: int,
    repo_name: str,
    trigger_bug_id: int,
    conflict_files: list[str],
) -> None:
    """Append a ``## Merge Conflict`` section to the bug's TRIAGE.md.

    Best-effort; logs on failure. The section lists the conflicting files
    + tells the reviewer their options (retry with conflict context, or
    take over manually). We pick the TRIAGE.md path in the repo whose
    rebase conflicted — that's the worktree where the conflict physically
    lives, so the reviewer reads it alongside the conflicted files.
    """
    worktree = wt_module.path_for(bug_id, repo_name)
    triage_md = worktree / "TRIAGE.md"
    body_lines = [
        f"Bug #{trigger_bug_id} merged first and the automatic rebase of this "
        f"bug's `{repo_name}` worktree hit a conflict. The rebase has been "
        "aborted; the worktree is back to its pre-rebase state.",
        "",
        "**Conflicting files:**",
    ]
    for p in conflict_files:
        body_lines.append(f"- `{p}`")
    body_lines.extend(
        [
            "",
            "**Options from here:**",
            "- Retry Fixer with conflict as context — re-runs the Fixer with "
            "the new main + the conflict files as additional context.",
            "- Take over manually — keep the bug in BLOCKED; resolve the "
            "conflict out-of-band in the worktree.",
        ]
    )
    try:
        triage_parser.append(triage_md, "Merge Conflict", "\n".join(body_lines))
    except OSError:
        logger.exception(
            "rebase: could not append Merge Conflict section to %s", triage_md
        )


def _repo_config(project: ProjectConfig, repo_name: str) -> RepoConfig | None:
    for r in project.repos:
        if r.name == repo_name:
            return r
    return None


# --- DB helpers (sync, dispatched via asyncio.to_thread) --------------------


@dataclass(frozen=True)
class _BugView:
    """Minimal detached view of a Bug used by the sweeper loop.

    Kept as a plain dataclass so we don't hold an ORM session across
    thread boundaries.
    """

    id: int
    state: str


def _list_inflight_sync(repo_name: str, exclude_bug_id: int) -> list[_BugView]:
    session = engine_module.SessionLocal()
    try:
        bugs = repo_module.list_inflight_for_repo(
            session, repo_name, exclude_bug_id=exclude_bug_id
        )
        return [_BugView(id=int(b.id), state=str(b.state)) for b in bugs]
    finally:
        session.close()


def _record_attempt_sync(
    bug_id: int,
    repo_name: str,
    trigger_bug_id: int,
    result: str,
    conflict_files: list[str] | None,
) -> None:
    session = engine_module.SessionLocal()
    try:
        repo_module.record_rebase_attempt(
            session,
            bug_id=bug_id,
            repo_name=repo_name,
            trigger_bug_id=trigger_bug_id,
            result=result,
            conflict_files=conflict_files,
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception(
            "rebase: record_rebase_attempt failed for bug=%d repo=%s",
            bug_id,
            repo_name,
        )
    finally:
        session.close()


def _mark_blocked_sync(
    bug_id: int,
    repo_name: str,
    conflict_files: list[str],
) -> None:
    session = engine_module.SessionLocal()
    try:
        repo_module.mark_blocked_with_conflict(
            session, bug_id, repo_name, conflict_files
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception(
            "rebase: mark_blocked_with_conflict failed for bug=%d repo=%s",
            bug_id,
            repo_name,
        )
    finally:
        session.close()


# --- subprocess helper ------------------------------------------------------


@dataclass(frozen=True)
class _GitResult:
    returncode: int
    stdout: str
    stderr: str


async def _run_git(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> _GitResult:
    """Run ``git <args>`` in ``cwd`` with a bounded timeout.

    Mirrors :func:`workspaces.worktree._run_git` in spirit but tuned for
    rebase semantics: non-zero exits are NOT raised by default here, so
    the caller can branch on conflict (non-zero + UU files) vs. error
    (non-zero + no files). If ``check=True`` we raise :class:`OSError`
    on non-zero exits so unexpected failures surface loudly.
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
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise OSError(
            f"git {' '.join(args)} in {cwd} timed out after "
            f"{_GIT_TIMEOUT_SECONDS:.0f}s"
        ) from exc

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    result = _GitResult(proc.returncode or 0, stdout, stderr)

    if check and result.returncode != 0:
        raise OSError(
            f"git {' '.join(args)} failed in {cwd} (exit "
            f"{result.returncode}); stdout={stdout.strip()!r}; "
            f"stderr={stderr.strip()!r}"
        )
    return result


__all__ = [
    "RebaseOutcome",
    "RebaseResult",
    "fetch_conflict_diff",
    "rebase_in_flight",
]
