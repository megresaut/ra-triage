"""Fixer-Backend agent — cross-repo parallel fix, backend slice.

Role: implements the backend portion of a cross-repo fix against the
Cross-Repo Contract the Fix Designer wrote into TRIAGE.md. Runs
concurrently with :class:`FixerFrontend` in the FIXING_PARALLEL step.

Output contract (tech plan §9.4):
  - Reads TRIAGE.md's Cross-Repo Contract (Shared Data Shape + Backend
    Responsibilities).
  - Modifies source files inside the backend worktree only.
  - Appends ``## Fixer-Backend Notes`` summarizing files changed,
    decisions, and contract-mismatch concerns (if any).
  - If a mismatch is found, appends ``## Fixer-Backend Contract Concern``
    AND still emits ``FIXER_BACKEND_COMPLETE`` — the orchestrator uses
    the presence of the concern section to route back to DESIGNING.
  - On failure emits ``FIXER_BACKEND_FAILED`` after writing
    ``## Fixer-Backend Failure``.

Completion tokens:
  - ``FIXER_BACKEND_COMPLETE`` — agent finished its slice (possibly with
    a contract concern flagged).
  - ``FIXER_BACKEND_FAILED`` — agent could not complete.

Tool allowlist (tech plan §5.2, per-side tightening):
  Backend gets Go toolchain only — deliberately no npm/npx. The ``cwd``
  constraint from the orchestrator already scopes filesystem access to
  the backend worktree; the allowlist tightening adds defense in depth
  against an agent that tries to reach outside its repo via an
  ``npm install`` in an adjacent frontend directory.
"""

from __future__ import annotations

from typing import ClassVar

from ra_triage.agents.base import ClaudeCodeAgent


class FixerBackend(ClaudeCodeAgent):
    """Fixer-Backend agent (cross-repo, backend worktree only)."""

    role: ClassVar[str] = "fixer_backend"

    completion_tokens: ClassVar[tuple[str, ...]] = (
        "FIXER_BACKEND_COMPLETE",
        "FIXER_BACKEND_FAILED",
    )

    allowed_tools: ClassVar[list[str]] = [
        "Read",
        "Edit",
        "Write",
        "Grep",
        "Glob",
        # Go toolchain only — no Node. ``cd:*`` covers the AVM
        # backend's ``cd api && ...`` build wrapper.
        "Bash(cd:*)",
        "Bash(bash:*)",
        "Bash(go:*)",
        "Bash(gofmt:*)",
    ]

    prompt_template: ClassVar[str] = "fixer_backend.md.j2"


__all__ = ["FixerBackend"]
