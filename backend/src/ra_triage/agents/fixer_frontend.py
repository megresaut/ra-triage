"""Fixer-Frontend agent — cross-repo parallel fix, frontend slice.

Role: implements the frontend portion of a cross-repo fix against the
Cross-Repo Contract the Fix Designer wrote into TRIAGE.md. Runs
concurrently with :class:`FixerBackend` in the FIXING_PARALLEL step.

Output contract (tech plan §9.4):
  - Reads TRIAGE.md's Cross-Repo Contract (Shared Data Shape + Frontend
    Responsibilities).
  - Modifies source files inside the frontend worktree only.
  - Appends ``## Fixer-Frontend Notes`` summarizing files changed,
    decisions, and contract-mismatch concerns (if any).
  - If a mismatch is found, appends ``## Fixer-Frontend Contract Concern``
    AND still emits ``FIXER_FRONTEND_COMPLETE`` — the orchestrator uses
    the presence of the concern section to route back to DESIGNING.
  - On failure emits ``FIXER_FRONTEND_FAILED`` after writing
    ``## Fixer-Frontend Failure``.

Completion tokens:
  - ``FIXER_FRONTEND_COMPLETE`` — agent finished its slice (possibly with
    a contract concern flagged).
  - ``FIXER_FRONTEND_FAILED`` — agent could not complete.

Tool allowlist (tech plan §5.2, per-side tightening):
  Frontend gets Node/npm toolchain only — deliberately no Go tools. The
  ``cwd`` constraint from the orchestrator already scopes filesystem
  access to the frontend worktree; the allowlist tightening adds defense
  in depth against an agent that tries to reach outside its repo via
  ``go build ../other-repo/...``-style commands.
"""

from __future__ import annotations

from typing import ClassVar

from ra_triage.agents.base import ClaudeCodeAgent


class FixerFrontend(ClaudeCodeAgent):
    """Fixer-Frontend agent (cross-repo, frontend worktree only)."""

    role: ClassVar[str] = "fixer_frontend"

    completion_tokens: ClassVar[tuple[str, ...]] = (
        "FIXER_FRONTEND_COMPLETE",
        "FIXER_FRONTEND_FAILED",
    )

    allowed_tools: ClassVar[list[str]] = [
        "Read",
        "Edit",
        "Write",
        "Grep",
        "Glob",
        # Node / JS toolchain only — no Go. ``cd:*`` covers the
        # project's ``cd <subdir> && ...`` build wrappers where present.
        "Bash(cd:*)",
        "Bash(bash:*)",
        "Bash(npm:*)",
        "Bash(npx:*)",
        "Bash(node:*)",
    ]

    prompt_template: ClassVar[str] = "fixer_frontend.md.j2"


__all__ = ["FixerFrontend"]
