"""Tester agent — regression-test pass after the Fixer.

Role: verifies the Fixer's work by (a) running the project's test suite
and (b) adding a regression test that would have caught the bug on main.
Tester's writes are constrained to TEST files — the prompt says so and
the orchestrator post-run review reverts any prod-file modifications
(tech plan §5.2 / §9.5 / Agent 6 brief).

Output contract (tech plan §9.5):
  - Reads "Recommended Fix Approach" and "Fixer Notes" from TRIAGE.md.
  - Runs the test command; records baseline.
  - Writes a regression test that would fail on main and passes here.
  - Runs the test command again — must be green.
  - Appends ``## Tester Notes`` with tests added + suite result. On
    failure, appends ``## Tester Failure`` instead and stops.

Completion tokens (tech plan Appendix A):
  - ``TESTER_COMPLETE`` — tests added, suite green.
  - ``TESTER_FAILED`` — legitimate test failure reveals a real bug in the
    Fixer's work, OR the agent could not complete for another reason.
    The orchestrator treats this as "Fixer output is not acceptable" and
    routes the bug to BLOCKED with the Tester Failure context visible.

Tool allowlist (tech plan §5.2):
  Same coarse-grained Bash shape as the Fixer — the Tester needs to run
  ``go test`` or ``npm test`` plus surrounding ``cd`` wrappers. Tester's
  "test files only" constraint is enforced in the prompt AND by the
  orchestrator's post-run safety check (see
  ``orchestrator/tester_safety.py``).
"""

from __future__ import annotations

from typing import ClassVar

from ra_triage.agents.base import ClaudeCodeAgent


class Tester(ClaudeCodeAgent):
    """Tester agent (test-file writes only)."""

    role: ClassVar[str] = "tester"

    completion_tokens: ClassVar[tuple[str, ...]] = (
        "TESTER_COMPLETE",
        "TESTER_FAILED",
    )

    allowed_tools: ClassVar[list[str]] = [
        "Read",
        "Edit",
        "Write",
        "Grep",
        "Glob",
        # Coarse-grained Bash allowlist, matching the Fixer. See the
        # Fixer module docstring for the rationale.
        "Bash(cd:*)",
        "Bash(bash:*)",
        "Bash(go:*)",
        "Bash(npm:*)",
        "Bash(npx:*)",
        "Bash(node:*)",
    ]

    prompt_template: ClassVar[str] = "tester.md.j2"


__all__ = ["Tester"]
