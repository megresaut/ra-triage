"""Fixer agent — single-repo implementation pass.

Role: applies the Investigator's Recommended Fix Approach to source files
in a single repo's worktree. Write access is gated by the tool allowlist
plus the prompt's deny-list + test-file prohibitions.

Output contract (tech plan §9.3):
  - Modifies source files inside the worktree (not test files, not
    deny-list paths, not env/config files).
  - Appends ``## Fixer Notes`` to TRIAGE.md summarizing files changed,
    key decisions, and a heads-up for the Tester. On failure, appends
    ``## Fixer Failure`` instead.
  - If the fix is blocked because it would require a deny-list change,
    appends ``## Fixer Escalation`` and stops (orchestrator routes to
    BLOCKED in that case).

Completion tokens (tech plan Appendix A):
  - ``FIXER_COMPLETE`` — fix implemented, build command green.
  - ``FIXER_FAILED`` — agent could not complete; a ``## Fixer Failure``
    section explains why.

Tool allowlist (tech plan §5.2 + Agent 6 deviation):
  The tech plan says ``Bash({project.build_command})``, but a literal
  ``cd api && go build ./... && go vet ./...`` in ``--allowed-tools``
  parses poorly because Claude's allowlist splits on commas. We
  deliberately relax to coarse-grained shell prefixes (``cd:*``,
  ``bash:*``, ``go:*``, ``gofmt:*``, ``npm:*``, ``npx:*``, ``node:*``)
  so the same Fixer class handles both Go (AVM backend) and
  JS/TS (AVM frontend) repos without per-project allowlist rendering.

  The prompt still constrains behavior: source-only modifications,
  no test files, no deny-list paths, follow project conventions.
"""

from __future__ import annotations

from typing import ClassVar

from ra_triage.agents.base import ClaudeCodeAgent


class Fixer(ClaudeCodeAgent):
    """Fixer agent (single-repo write access)."""

    role: ClassVar[str] = "fixer"

    completion_tokens: ClassVar[tuple[str, ...]] = (
        "FIXER_COMPLETE",
        "FIXER_FAILED",
    )

    allowed_tools: ClassVar[list[str]] = [
        "Read",
        "Edit",
        "Write",
        "Grep",
        "Glob",
        # Coarse-grained Bash allowlist: covers the Go toolchain (AVM
        # backend), the Node/TS toolchain (AVM frontend), and the
        # ``cd api && ...`` wrapper the AVM backend's build_command
        # uses. Documented deviation from tech plan §5.2's per-project
        # literal; see module docstring.
        "Bash(cd:*)",
        "Bash(bash:*)",
        "Bash(go:*)",
        "Bash(gofmt:*)",
        "Bash(npm:*)",
        "Bash(npx:*)",
        "Bash(node:*)",
    ]

    prompt_template: ClassVar[str] = "fixer.md.j2"


__all__ = ["Fixer"]
