"""Belt-and-suspenders guard for the Tester's "test files only" rule.

The Tester prompt (tech plan §9.5) says the agent MUST NOT modify
production code. We enforce that constraint a second time at the
orchestrator level so a prompt-following lapse doesn't translate into a
prod-code diff sneaking into the bug's branch:

  1. After ``Tester.run`` returns we compute ``git diff --name-only
     HEAD`` in the worktree.
  2. Every changed path is classified as "test file" or "prod file" via
     :func:`is_test_path`.
  3. Prod-file paths are reverted with ``git checkout HEAD -- <path>``.
  4. A ``## Tester Safety Violation`` section is appended to TRIAGE.md
     listing what was reverted; the bug transitions to BLOCKED.

``TRIAGE.md`` itself is explicitly allow-listed (it's the agent
scratchpad; the Tester is expected to append ``## Tester Notes``).

The pattern list is a single module constant so it's reviewable in one
place. Matching is done with :mod:`fnmatch` using the full path (forward
slashes, relative to the worktree). Patterns use standard glob syntax;
``**`` segments match any number of path components.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# --- Test-path patterns -------------------------------------------------
#
# Order-independent set; a file is a TEST path iff any pattern matches.
# Adjust here (not scattered through callers) when new project
# conventions appear.
#
# Notes on coverage:
#   - Go: ``*_test.go`` files and any file under a ``/test/`` or
#     ``/tests/`` directory.
#   - JS/TS: ``*.test.{ts,tsx,js,jsx}``, ``*.spec.{ts,tsx,js,jsx}``, and
#     anything inside ``__tests__/`` or ``tests/``.
#   - Python: ``tests/`` directories and ``test_*.py`` / ``*_test.py``
#     filenames. Included for completeness even though AVM doesn't use
#     Python — we might host Python projects later.
#   - ``TRIAGE.md`` is explicitly whitelisted (scratchpad).
TEST_PATH_PATTERNS: tuple[str, ...] = (
    # Go
    "**/*_test.go",
    # JS/TS test/spec files
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/*.test.js",
    "**/*.test.jsx",
    "**/*.spec.ts",
    "**/*.spec.tsx",
    "**/*.spec.js",
    "**/*.spec.jsx",
    # Directory-based conventions (Go, JS, Python share these)
    "**/tests/**",
    "**/test/**",
    "**/__tests__/**",
    # Python test conventions
    "**/test_*.py",
    "**/*_test.py",
)

# Paths that are never "prod code" regardless of their extension — e.g.
# TRIAGE.md is the agent scratchpad, always expected to be edited by the
# Tester (it appends ``## Tester Notes``).
ALWAYS_ALLOWED_PATHS: frozenset[str] = frozenset({"TRIAGE.md"})


@dataclass(frozen=True)
class SafetyCheckResult:
    """Outcome of the post-run safety check.

    ``changed_paths`` — every path from ``git diff --name-only HEAD``.
    ``test_paths`` — paths classified as test files (kept).
    ``reverted_paths`` — prod paths we reverted with ``git checkout HEAD -- <p>``.
    ``revert_errors`` — any ``git checkout`` failures (paths we could not
      revert; the caller still transitions to BLOCKED but should surface
      these in the safety-violation section).
    ``had_violation`` — True iff any prod-file modification was detected.
    """

    changed_paths: list[str]
    test_paths: list[str]
    reverted_paths: list[str]
    revert_errors: list[tuple[str, str]]
    had_violation: bool


def is_test_path(relative_path: str) -> bool:
    """Return True if ``relative_path`` is a test file or explicitly allowed.

    ``relative_path`` is expected to be a forward-slash path relative to
    the worktree root (the shape ``git diff --name-only HEAD`` emits).
    """
    # Normalize any Windows-style separators defensively. In practice
    # ``git`` always emits forward slashes, but we don't want the
    # classifier to be platform-sensitive.
    normalized = relative_path.replace("\\", "/")

    # Explicit allow-list — applied before pattern matching so a user
    # config listing ``TRIAGE.md`` here wins even if someone adds a
    # pattern that would otherwise miss it.
    if normalized in ALWAYS_ALLOWED_PATHS:
        return True

    for pattern in TEST_PATH_PATTERNS:
        if _glob_match(pattern, normalized):
            return True
    return False


def _glob_match(pattern: str, path: str) -> bool:
    """Match ``path`` against a glob pattern with ``**`` segment support.

    ``fnmatch`` (stdlib) does NOT treat ``**`` as "any number of path
    components" — it's just a literal ``*`` twice. We translate the
    pattern to a regex that does the right thing for the directory-level
    wildcard.

    Semantics we implement:
      - ``**`` as a whole path segment matches ZERO or more path
        components (including empty). ``**/foo`` matches both ``foo``
        and ``a/b/foo``; ``foo/**`` matches both ``foo`` (bare) and
        ``foo/a/b``; ``**/foo/**`` matches ``foo``, ``a/foo``,
        ``foo/b``, and ``a/foo/b``.
      - ``*`` inside a segment matches any run of non-``/`` chars.
      - ``?`` inside a segment matches exactly one non-``/`` char.
    """
    parts = pattern.split("/")
    segments: list[str] = []
    for p in parts:
        if p == "**":
            # Sentinel; the regex builder below handles it.
            segments.append("**")
        else:
            segments.append(_glob_segment_to_regex(p))
    regex = _build_regex(segments)
    return re.fullmatch(regex, path) is not None


def _glob_segment_to_regex(segment: str) -> str:
    """Convert a single path segment's glob syntax to regex.

    ``*`` → ``[^/]*``; ``?`` → ``[^/]``; other regex metacharacters are
    escaped.
    """
    out: list[str] = []
    i = 0
    while i < len(segment):
        ch = segment[i]
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch in ".^$+(){}|\\":
            out.append("\\" + ch)
        else:
            out.append(re.escape(ch) if not ch.isalnum() else ch)
        i += 1
    return "".join(out)


def _build_regex(segments: list[str]) -> str:
    """Build an anchored regex from the compiled segment list.

    The ``**`` sentinel can appear at any position. Between two
    non-``**`` segments we emit a literal ``/``. A ``**`` absorbs the
    separator on one side depending on its neighbors:
      - ``**/foo`` → ``(?:.*/)?foo``    (zero-or-more dir components)
      - ``foo/**`` → ``foo(?:/.*)?``    (zero-or-more child components)
      - ``**/foo/**`` → ``(?:.*/)?foo(?:/.*)?``
    """
    pieces: list[str] = []
    n = len(segments)
    for i, seg in enumerate(segments):
        if seg == "**":
            has_prev = i > 0
            has_next = i < n - 1
            if not has_prev and not has_next:
                # Pattern is just "**" — match anything including empty.
                pieces.append(".*")
            elif has_prev and has_next:
                # Replace the separator with an optional "/anything"
                # block. The prev segment's trailing ``/`` (we always
                # emit one between adjacent non-** segments) is
                # absorbed by rolling it into the optional group.
                # We emit ``(?:/.*)?/`` but that double-separates; the
                # cleaner shape is ``(?:.*/)?`` preceded by NO slash.
                # Strategy: we DROP the normal leading ``/`` for this
                # segment position via a sentinel and emit ``(?:.*/)?``.
                # However, "drop preceding /" complicates the loop;
                # easier: emit ``(?:[^/]+/)*[^/]+/`` but that forces at
                # least one component.
                #
                # Safer and equivalent: join with "(?:/.*/|/)" meaning
                # "/ OR /X/Y.../" — but then next non-** segment will
                # add its own leading /, doubling up.
                #
                # Cleanest: emit ``(?:.*/)?`` WITHOUT a preceding /,
                # and have the caller NOT emit a leading / before the
                # next segment when the previous segment was ``**``.
                pieces.append("(?:.*/)?")
            elif has_prev and not has_next:
                # ``foo/**`` → ``foo(?:/.*)?``. We emitted ``foo`` as
                # the previous piece; strip our pending ``/`` (none
                # emitted yet) and append the optional tail.
                pieces.append("(?:/.*)?")
            elif not has_prev and has_next:
                # ``**/foo`` → ``(?:.*/)?foo``. Emit the optional dir
                # prefix; next segment is appended WITHOUT a leading /
                # because the prefix already includes its trailing /
                # (or is empty).
                pieces.append("(?:.*/)?")
            continue

        # Regular segment — emit, with a leading / unless this is the
        # first piece overall OR the previous emitted piece absorbed
        # the separator.
        if pieces:
            prev = pieces[-1]
            if prev.endswith("/") or prev == "(?:.*/)?" or prev == "(?:/.*)?":
                pieces.append(seg)
            else:
                pieces.append("/")
                pieces.append(seg)
        else:
            pieces.append(seg)
    return "".join(pieces)


async def check_and_revert_prod_changes(
    worktree: Path, *, baseline: dict[str, str] | None = None
) -> SafetyCheckResult:
    """Inspect the Tester's post-run diff; revert any prod-file mods.

    ``baseline`` — mapping of ``{path: content_hash}`` captured BEFORE
    the Tester ran (via :func:`snapshot_working_tree`). The classifier
    treats a file as "Tester-produced" iff its post-run content hash
    differs from the baseline entry for that path, OR the path is new
    (not in the baseline at all). Legitimate Fixer work — unchanged
    between baseline and post-Tester — is ignored.

    Pass ``baseline=None`` (default) to treat every changed-vs-HEAD
    path as Tester-produced. This is appropriate for pure unit tests
    where the worktree has no pre-existing state.

    This function is idempotent from the bug's perspective: if the
    Tester modified nothing (or only touched test files + TRIAGE.md),
    it returns ``had_violation=False`` and no ``git checkout`` calls
    run.

    Errors reverting individual files are captured in
    ``revert_errors`` rather than raised, so one stubborn file doesn't
    prevent us from reverting the rest.
    """
    changed = await _git_diff_name_only(worktree)

    if baseline is None:
        tester_changed = list(changed)
    else:
        tester_changed = []
        for path in changed:
            current_hash = _hash_file_or_missing(worktree / path)
            prev_hash = baseline.get(path)
            if current_hash != prev_hash:
                # Either the Tester modified a file the Fixer also
                # touched, or the Tester introduced a brand-new file.
                tester_changed.append(path)

    test_paths: list[str] = []
    prod_paths: list[str] = []
    for path in tester_changed:
        if is_test_path(path):
            test_paths.append(path)
        else:
            prod_paths.append(path)

    reverted: list[str] = []
    errors: list[tuple[str, str]] = []
    for path in prod_paths:
        try:
            await _revert_to_baseline(
                worktree, path, baseline_hash=(baseline or {}).get(path)
            )
            reverted.append(path)
        except _GitCommandError as exc:
            logger.warning(
                "Failed to revert prod file %s in %s: %s", path, worktree, exc
            )
            errors.append((path, str(exc)))

    return SafetyCheckResult(
        changed_paths=changed,
        test_paths=test_paths,
        reverted_paths=reverted,
        revert_errors=errors,
        had_violation=bool(prod_paths),
    )


async def snapshot_working_tree(worktree: Path) -> dict[str, str]:
    """Return ``{path: content_hash}`` for every changed-vs-HEAD path.

    Captured AFTER the Fixer runs; paired with
    :func:`check_and_revert_prod_changes` ``baseline=`` argument so the
    Tester safety check ignores files the Fixer legitimately modified.

    Hash is SHA256 of the file contents (hex-encoded). A path that's in
    the diff but not on disk (e.g. a Fixer-deleted file we can't read)
    maps to the sentinel string ``"<missing>"``.

    Returns an empty dict if the worktree isn't a git repo — the
    pipeline treats that as "no baseline available; compare against
    HEAD directly" (best-effort).
    """
    try:
        changed = await _git_diff_name_only(worktree)
    except _GitCommandError:
        return {}
    snapshot: dict[str, str] = {}
    for path in changed:
        snapshot[path] = _hash_file_or_missing(worktree / path)
    return snapshot


# Backwards-compatible alias kept because tests may have cached a
# reference. Prefer ``snapshot_working_tree``.
async def snapshot_changed_paths(worktree: Path) -> set[str]:
    """Compatibility wrapper: return just the path set (no hashes)."""
    return set((await snapshot_working_tree(worktree)).keys())


def _hash_file_or_missing(path: Path) -> str:
    """Return sha256(file contents) or ``"<missing>"`` if the file is gone."""
    import hashlib

    try:
        data = path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return "<missing>"
    return hashlib.sha256(data).hexdigest()


async def _revert_to_baseline(
    worktree: Path, path: str, *, baseline_hash: str | None
) -> None:
    """Revert ``path`` to the Fixer-baseline state (or HEAD if no baseline).

    Behavior:
      - ``baseline_hash == "<missing>"``: the baseline had no on-disk
        file at this path, so the Tester must have created it new →
        ``git checkout HEAD`` (tracked case) or delete (untracked).
      - otherwise (``None`` OR a real hash): try ``git checkout HEAD
        -- <path>`` to restore the HEAD version. If that fails, the
        file is untracked; delete it.

    Note: we can't easily restore a Fixer-modified-but-not-committed
    baseline without stashing, so a same-file Fixer+Tester double-edit
    loses the Fixer's change along with the Tester's. Documented
    trade-off in the Agent 6 brief: the bug is BLOCKED either way and
    the Fixer can re-run if needed.
    """
    # First attempt: git checkout HEAD.
    try:
        await _run_git(
            ["checkout", "HEAD", "--", path],
            cwd=worktree,
        )
        checkout_ok = True
    except _GitCommandError:
        checkout_ok = False

    target = worktree / path

    if baseline_hash == "<missing>":
        # Pre-Tester state had no file here; whatever git just restored
        # (if anything) is also wrong. Delete.
        if target.is_file():
            try:
                target.unlink()
            except OSError as exc:
                raise _GitCommandError(
                    f"Could not delete prod file {path}: {exc}"
                ) from exc
        return

    if checkout_ok:
        # File is tracked and has been restored to HEAD. Done.
        return

    # Untracked file (git checkout failed AND baseline isn't "<missing>").
    # Delete so the prod-code change doesn't linger.
    if target.is_file():
        try:
            target.unlink()
        except OSError as exc:
            raise _GitCommandError(
                f"Could not delete untracked prod file {path}: {exc}"
            ) from exc


# --- git helpers --------------------------------------------------------


class _GitCommandError(Exception):
    """Raised when a git subprocess fails inside this module."""


async def _git_diff_name_only(worktree: Path) -> list[str]:
    """Return the set of paths that differ from HEAD in ``worktree``.

    We include BOTH unstaged changes (``git diff --name-only HEAD``) and
    untracked files (``git ls-files --others --exclude-standard``) so a
    Tester that creates a brand-new prod file is not missed.

    Paths are forward-slash relative to the worktree root.
    """
    changed = set()

    diff_out = await _run_git(
        ["diff", "--name-only", "HEAD"],
        cwd=worktree,
    )
    for line in diff_out.splitlines():
        line = line.strip()
        if line:
            changed.add(line)

    untracked = await _run_git(
        ["ls-files", "--others", "--exclude-standard"],
        cwd=worktree,
    )
    for line in untracked.splitlines():
        line = line.strip()
        if line:
            changed.add(line)

    return sorted(changed)


async def _run_git(args: list[str], *, cwd: Path) -> str:
    """Run ``git <args>`` in ``cwd``; return stdout. Raise on non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        raise _GitCommandError(
            f"git {' '.join(args)} in {cwd} exited {proc.returncode}: "
            f"{stderr_bytes.decode('utf-8', errors='replace').strip()}"
        )
    return stdout_bytes.decode("utf-8", errors="replace")


__all__ = [
    "ALWAYS_ALLOWED_PATHS",
    "SafetyCheckResult",
    "TEST_PATH_PATTERNS",
    "check_and_revert_prod_changes",
    "is_test_path",
    "snapshot_changed_paths",
]
