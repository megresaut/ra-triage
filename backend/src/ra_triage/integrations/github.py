"""GitHub CLI (`gh`) integration for RA Triage.

Thin, async-native wrapper around the `gh` CLI that Agent 8's merge driver
uses. We deliberately do NOT use the GitHub REST SDK — the product/tech plan
pins us on `gh` (technical plan §12.2) because Megha already has it
authenticated against the org.

All public functions run from `cwd=worktree` so `gh` picks up the right
remote without us having to pass `--repo` explicitly. We do still run an
explicit `git push -u origin <branch>` before `gh pr create` so the branch
exists on the remote — otherwise `gh pr create` would fail with a no-commits
error.

Tests stub the subprocess runner via :func:`set_runner` so we can assert
argv assembly and error handling without real `gh` / `git` invocations.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from ra_triage.config.registry import RepoConfig

logger = logging.getLogger(__name__)

# All gh subcommands get the same ceiling. 60s covers slow corporate VPN
# round-trips to github.com without letting a broken remote hang forever.
_GH_TIMEOUT_SECONDS = 60.0

#: Status values returned by :func:`gh_pr_status`. Normalized from
#: `gh pr view --json state,mergedAt` so callers don't duplicate the parsing.
PRStatus = Literal["open", "merged", "closed"]


# --- Dataclasses & exceptions ----------------------------------------------


@dataclass(frozen=True)
class PRInfo:
    """Minimal PR summary returned by :func:`gh_pr_create`."""

    repo_name: str
    branch: str
    number: int
    url: str
    status: PRStatus


class GitHubError(Exception):
    """Raised when a `gh` subprocess fails or its output can't be parsed.

    Wraps the non-zero exit code, argv, and stderr so callers can surface a
    useful diagnostic in TRIAGE.md's ``## Merge Failure`` section.
    """


# --- Subprocess runner test-seam -------------------------------------------


class _SubprocessResult:
    """Completed-process container used by :class:`_SubprocessRunner`.

    Fields mirror `asyncio.subprocess.Process.communicate`'s return shape but
    decoded to text. Tests substitute a plain dataclass-shaped object; the
    production runner constructs this directly.
    """

    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _SubprocessRunner(Protocol):
    """Callable signature of the pluggable subprocess runner.

    The default implementation shells out via ``asyncio.create_subprocess_exec``.
    Tests set their own runner via :func:`set_runner` to assert argv and
    inject canned stdout/stderr/returncodes.
    """

    async def __call__(
        self, argv: list[str], *, cwd: Path, timeout: float
    ) -> _SubprocessResult: ...


async def _default_runner(
    argv: list[str], *, cwd: Path, timeout: float
) -> _SubprocessResult:
    """Production runner: real subprocess with a timeout."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise GitHubError(
            f"{argv[0]} {' '.join(argv[1:])} in {cwd} timed out after "
            f"{timeout:.0f}s"
        ) from exc
    return _SubprocessResult(
        returncode=proc.returncode or 0,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
    )


#: Module-level runner hook. Default is ``_default_runner``; tests swap it.
_runner: _SubprocessRunner = _default_runner


def set_runner(runner: _SubprocessRunner | None) -> None:
    """Install a custom subprocess runner (tests only).

    Pass ``None`` to restore the default (real subprocess) runner.
    """
    global _runner
    _runner = runner if runner is not None else _default_runner


def _resolve_gh() -> str:
    """Locate the `gh` binary on PATH, or raise a clear error.

    Production callers hit this before invoking any gh command so failures
    surface with the same message regardless of which subcommand tripped it.
    Tests that install a runner skip this check (the runner is responsible
    for canned argv handling).
    """
    if _runner is not _default_runner:
        # Tests: runner intercepts every call, no real binary needed.
        return "gh"
    resolved = shutil.which("gh")
    if not resolved:
        raise GitHubError(
            "`gh` CLI not found on PATH. Install via `brew install gh` and run "
            "`gh auth login` before using Triage's Confirm/Merge flow."
        )
    return resolved


# --- git push helper --------------------------------------------------------


async def _git_push(
    *, worktree: Path, branch: str
) -> _SubprocessResult:
    """Push ``branch`` with ``-u origin`` from ``worktree``.

    We swallow "already up-to-date"-style noise on success; real errors (no
    upstream access, non-fast-forward) surface via :class:`GitHubError`.
    """
    argv = ["git", "push", "-u", "origin", branch]
    result = await _runner(argv, cwd=worktree, timeout=_GH_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise GitHubError(
            f"`git push -u origin {branch}` failed in {worktree} "
            f"(exit {result.returncode}). stderr: {result.stderr.strip()}"
        )
    return result


# --- Public API -------------------------------------------------------------


_PR_URL_RE = re.compile(r"https?://github\.com/[^/]+/[^/]+/pull/(\d+)")


async def gh_pr_create(
    *,
    worktree: Path,
    repo: RepoConfig,
    bug_id: int,
    title: str,
    body_path: Path,
    cross_ref_body_note: str | None = None,
    cross_ref_url: str | None = None,
) -> PRInfo:
    """Create a pull request from ``triage/<bug_id>`` to ``repo.main_branch``.

    Runs ``git push -u origin <branch>`` before ``gh pr create`` so the
    branch exists on the remote. When ``cross_ref_body_note`` is provided
    (Agent 10's cross-repo case, first pass) we concatenate ``body_path``'s
    contents with the note into a temp file and pass that to ``gh`` via
    ``--body-file``.

    When ``cross_ref_url`` is provided (Agent 10 second-pass or single-pass
    when the other-side URL is already known) we PREPEND a single
    cross-reference line to the PR body so the link appears at the top of
    the description for easy discovery:

        > Related PR: <cross_ref_url>

    ``cross_ref_url`` takes precedence over ``cross_ref_body_note`` when
    both are supplied; they are not expected to be used together but the
    former's top-placement is the specced behavior for the cross-repo flow
    (merge.py's two-pass edit relies on ``gh_pr_edit_body`` below).

    Returns a :class:`PRInfo` with ``status='open'`` on success. Raises
    :class:`GitHubError` on any failure; the caller turns that into a
    BLOCKED transition with a ``## Merge Failure`` section.
    """
    _resolve_gh()
    branch = f"{repo.branch_prefix}{bug_id}"

    if not body_path.exists():
        raise GitHubError(
            f"PR body file {body_path} does not exist — the Investigator may "
            f"not have written TRIAGE.md yet for bug {bug_id}."
        )

    # Push first so the remote has the branch — `gh pr create` will otherwise
    # fail with "must first push the current branch to a remote".
    await _git_push(worktree=worktree, branch=branch)

    # Assemble the body file. If there's a cross-ref URL (top-placement) or
    # a cross-ref note (bottom-placement), copy body_path's contents + the
    # decoration into a temp file and point --body-file at it. We
    # deliberately don't mutate the original TRIAGE.md.
    effective_body_path = body_path
    temp_body: Path | None = None
    if cross_ref_url is not None and cross_ref_url.strip():
        original = body_path.read_text(encoding="utf-8")
        temp_body = Path(
            tempfile.mkstemp(prefix=f"triage-pr-body-{bug_id}-", suffix=".md")[1]
        )
        header = f"> Related PR: {cross_ref_url.strip()}\n\n"
        temp_body.write_text(
            header + original.rstrip("\n") + "\n",
            encoding="utf-8",
        )
        effective_body_path = temp_body
    elif cross_ref_body_note is not None and cross_ref_body_note.strip():
        original = body_path.read_text(encoding="utf-8")
        temp_body = Path(
            tempfile.mkstemp(prefix=f"triage-pr-body-{bug_id}-", suffix=".md")[1]
        )
        temp_body.write_text(
            original.rstrip("\n") + "\n\n" + cross_ref_body_note.rstrip("\n") + "\n",
            encoding="utf-8",
        )
        effective_body_path = temp_body

    try:
        argv = [
            "gh",
            "pr",
            "create",
            "--title",
            title,
            "--body-file",
            str(effective_body_path),
            "--base",
            repo.main_branch,
            "--head",
            branch,
        ]
        result = await _runner(argv, cwd=worktree, timeout=_GH_TIMEOUT_SECONDS)
    finally:
        if temp_body is not None:
            with contextlib.suppress(OSError):
                temp_body.unlink()

    if result.returncode != 0:
        raise GitHubError(
            f"`gh pr create` failed in {worktree} "
            f"(exit {result.returncode}). stderr: {result.stderr.strip()}"
        )

    url = _extract_pr_url(result.stdout) or _extract_pr_url(result.stderr)
    if url is None:
        raise GitHubError(
            f"`gh pr create` succeeded but no PR URL was found in output. "
            f"stdout: {result.stdout.strip()!r} stderr: {result.stderr.strip()!r}"
        )
    number = _extract_pr_number(url)
    return PRInfo(
        repo_name=repo.name,
        branch=branch,
        number=number,
        url=url,
        status="open",
    )


async def gh_pr_merge(
    *,
    worktree: Path,
    pr_number: int,
    squash: bool = True,
    delete_branch: bool = True,
) -> None:
    """Merge a PR via ``gh pr merge``. Raises :class:`GitHubError` on failure.

    Defaults match tech plan §12.2: ``--squash`` + ``--delete-branch``. The
    subprocess runs from ``worktree`` so `gh` picks up the matching remote.
    """
    _resolve_gh()
    argv = ["gh", "pr", "merge", str(pr_number)]
    if squash:
        argv.append("--squash")
    if delete_branch:
        argv.append("--delete-branch")
    result = await _runner(argv, cwd=worktree, timeout=_GH_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise GitHubError(
            f"`gh pr merge {pr_number}` failed in {worktree} "
            f"(exit {result.returncode}). stderr: {result.stderr.strip()}"
        )


async def gh_pr_edit_body(
    *,
    worktree: Path,
    pr_number: int,
    body_path: Path,
    cross_ref_url: str | None = None,
) -> None:
    """Update an existing PR's body via ``gh pr edit --body-file``.

    Used by the cross-repo merge flow: both PRs are created first (each
    referencing "related PR will be linked here"), then this helper runs
    on each PR with the OTHER PR's URL so both bodies end up with a
    top-placed cross-reference line::

        > Related PR: <cross_ref_url>

        <original TRIAGE.md body>

    ``body_path`` must exist; ``cross_ref_url`` is optional — when absent,
    the PR body is rewritten verbatim from ``body_path``. Raises
    :class:`GitHubError` on failure.
    """
    _resolve_gh()
    if not body_path.exists():
        raise GitHubError(
            f"PR body file {body_path} does not exist — cannot edit PR #{pr_number}."
        )

    # Build the effective body file (either a decorated temp file, or the
    # original if no URL). Temp file is cleaned up after gh finishes.
    temp_body: Path | None = None
    effective_body_path = body_path
    if cross_ref_url is not None and cross_ref_url.strip():
        original = body_path.read_text(encoding="utf-8")
        temp_body = Path(
            tempfile.mkstemp(
                prefix=f"triage-pr-edit-{pr_number}-", suffix=".md"
            )[1]
        )
        header = f"> Related PR: {cross_ref_url.strip()}\n\n"
        temp_body.write_text(
            header + original.rstrip("\n") + "\n",
            encoding="utf-8",
        )
        effective_body_path = temp_body

    try:
        argv = [
            "gh",
            "pr",
            "edit",
            str(pr_number),
            "--body-file",
            str(effective_body_path),
        ]
        result = await _runner(argv, cwd=worktree, timeout=_GH_TIMEOUT_SECONDS)
    finally:
        if temp_body is not None:
            with contextlib.suppress(OSError):
                temp_body.unlink()

    if result.returncode != 0:
        raise GitHubError(
            f"`gh pr edit {pr_number}` failed in {worktree} "
            f"(exit {result.returncode}). stderr: {result.stderr.strip()}"
        )


async def gh_pr_status(
    *, worktree: Path, pr_number: int
) -> PRStatus:
    """Return the normalized status of a PR via ``gh pr view --json``.

    The raw ``state`` field from gh is one of ``OPEN``, ``MERGED``, ``CLOSED``.
    We normalize to the lowercase literal set :data:`PRStatus`.
    """
    _resolve_gh()
    argv = [
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--json",
        "state,mergedAt",
    ]
    result = await _runner(argv, cwd=worktree, timeout=_GH_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise GitHubError(
            f"`gh pr view {pr_number}` failed in {worktree} "
            f"(exit {result.returncode}). stderr: {result.stderr.strip()}"
        )
    # Parse minimally — we only need the `state` field; avoid a full JSON
    # schema for forward compatibility.
    import json

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubError(
            f"`gh pr view` returned unparseable JSON: {result.stdout!r}"
        ) from exc

    raw_state = str(payload.get("state", "")).upper()
    if raw_state == "MERGED":
        return "merged"
    if raw_state == "CLOSED":
        return "closed"
    if raw_state == "OPEN":
        return "open"
    raise GitHubError(
        f"`gh pr view` returned unexpected state {raw_state!r} (payload={payload!r})"
    )


# --- Parsing helpers --------------------------------------------------------


def _extract_pr_url(text: str) -> str | None:
    """Return the first ``https://github.com/.../pull/<n>`` URL in ``text``."""
    if not text:
        return None
    match = _PR_URL_RE.search(text)
    if match is None:
        return None
    return match.group(0)


def _extract_pr_number(url: str) -> int:
    """Extract the trailing integer PR number from a github.com pull URL."""
    match = _PR_URL_RE.search(url)
    if match is None:
        raise GitHubError(f"Could not parse PR number from URL: {url!r}")
    return int(match.group(1))


__all__ = [
    "GitHubError",
    "PRInfo",
    "PRStatus",
    "gh_pr_create",
    "gh_pr_edit_body",
    "gh_pr_merge",
    "gh_pr_status",
    "set_runner",
]
