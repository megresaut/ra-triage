"""Fix Designer agent — produces the Cross-Repo Contract (tech plan §9.2).

Role: read-only, writes nothing but TRIAGE.md. The Fix Designer reads the
Investigator's Root Cause / Scope / Recommended Fix Approach and writes
an explicit contract that the parallel Fixer-Frontend and Fixer-Backend
will implement against.

Output contract (tech plan §9.2):
  - Reads TRIAGE.md's Investigator output.
  - Appends ``## Cross-Repo Contract`` with sub-sections:
    Shared Data Shape / Backend Responsibilities / Frontend Responsibilities
    / Shared Assumptions / Integration Test Scenario.
  - On failure appends ``## Fix Designer Failure`` and prints
    ``FIX_DESIGNER_FAILED``.

Completion tokens:
  - ``FIX_DESIGNER_COMPLETE`` — contract written, both Fixers can proceed.
  - ``FIX_DESIGNER_FAILED`` — agent could not complete.

Tool allowlist (tech plan §5.2):
  Read / Grep / Glob — contract only, no source edits. No Bash.
"""

from __future__ import annotations

from typing import ClassVar

from ra_triage.agents.base import ClaudeCodeAgent


class FixDesigner(ClaudeCodeAgent):
    """Fix Designer agent (read-only; writes Cross-Repo Contract to TRIAGE.md)."""

    role: ClassVar[str] = "fix_designer"

    completion_tokens: ClassVar[tuple[str, ...]] = (
        "FIX_DESIGNER_COMPLETE",
        "FIX_DESIGNER_FAILED",
    )

    allowed_tools: ClassVar[list[str]] = [
        "Read",
        "Grep",
        "Glob",
    ]

    prompt_template: ClassVar[str] = "fix_designer.md.j2"


__all__ = ["FixDesigner"]
