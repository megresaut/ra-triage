"""Investigator agent — first run of the pipeline per bug.

Role: read-only analysis. The Investigator greps, reads files, runs
read-only project commands (pytest --collect-only, git log/diff/show,
rg), and writes its conclusions into the bug's TRIAGE.md. It never edits
source files.

Output contract (tech plan §5.4, §9.1):
  - ``## Root Cause`` — file:line references for the specific fault.
  - ``## Scope`` — machine-parseable ``key: value`` block (scope,
    affected_repos, deny_list_hit) that the orchestrator uses to route
    the bug into the single-repo or cross-repo branch of the state
    machine.
  - ``## Analysis`` — human-readable reasoning.
  - ``## Recommended Fix Approach`` — concrete steps for the Fixer.
  - ``## Escalation Brief`` — ONLY if ``deny_list_hit=true`` or human
    judgment is required.

Completion tokens (tech plan Appendix A):
  - ``INVESTIGATOR_COMPLETE`` — successful analysis; TRIAGE.md is ready.
  - ``INVESTIGATOR_FAILED`` — the agent could not complete; it must have
    written an ``## Investigator Failure`` section explaining why. The
    orchestrator treats this identically to "no token observed" for
    routing (→ BLOCKED).

Tool allowlist (tech plan §5.2):
  - ``Read``, ``Grep``, ``Glob`` — browsing.
  - ``Bash(git log:*)``, ``Bash(git diff:*)``, ``Bash(git show:*)`` — read-only
    git operations scoped by subcommand.
  - ``Bash(rg:*)`` — ripgrep search.
  - ``Bash(pytest --collect-only)``, ``Bash(npm test -- --listTests)`` —
    test-discovery-only escape hatches. Listed here as exact commands so
    the allowlist acts as a strict filter.
"""

from __future__ import annotations

from typing import ClassVar

from ra_triage.agents.base import ClaudeCodeAgent


class Investigator(ClaudeCodeAgent):
    """Investigator agent (read-only analysis)."""

    role: ClassVar[str] = "investigator"

    completion_tokens: ClassVar[tuple[str, ...]] = (
        "INVESTIGATOR_COMPLETE",
        "INVESTIGATOR_FAILED",
    )

    allowed_tools: ClassVar[list[str]] = [
        "Read",
        "Grep",
        "Glob",
        "Bash(git log:*)",
        "Bash(git diff:*)",
        "Bash(git show:*)",
        "Bash(rg:*)",
        "Bash(pytest --collect-only)",
        "Bash(npm test -- --listTests)",
    ]

    prompt_template: ClassVar[str] = "investigator.md.j2"


__all__ = ["Investigator"]
