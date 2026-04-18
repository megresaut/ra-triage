"""Integration Tester agent — cross-repo end-to-end verification.

Role: spawns both services from their worktrees on their
integration ports (28080 backend / 25173 frontend for AVM), waits for
readiness (TCP-open + <500 HTTP GET — AVM has no /health endpoint),
executes the Integration Test Scenario the Fix Designer wrote, authors
an automated integration test, and tears the services down.

Output contract (tech plan §9.6 + AVM readiness adaptation):
  - Reads TRIAGE.md's Cross-Repo Contract and both Fixer notes.
  - May create integration test files in either worktree's test tree.
  - MUST NOT modify production/source code.
  - MUST tear down both dev servers even on failure. The orchestrator
    also runs a best-effort ``pkill -f "port 28080"`` / ``pkill -f
    "port 25173"`` after the agent exits as a backstop.
  - Appends ``## Integration Tester Notes`` on success or
    ``## Integration Tester Failure`` on failure.

Completion tokens:
  - ``INTEGRATION_TESTER_COMPLETE`` — scenario verified, test written,
    services stopped.
  - ``INTEGRATION_TESTER_FAILED`` — scenario failed or the agent couldn't
    complete.

Tool allowlist (tech plan §5.2):
  Both toolchains here — the agent has to start both services. ``curl``
  is allowed for readiness + manual scenario execution.
"""

from __future__ import annotations

from typing import ClassVar

from ra_triage.agents.base import ClaudeCodeAgent


class IntegrationTester(ClaudeCodeAgent):
    """Integration Tester agent (cross-repo, starts/stops dev servers)."""

    role: ClassVar[str] = "integration_tester"

    completion_tokens: ClassVar[tuple[str, ...]] = (
        "INTEGRATION_TESTER_COMPLETE",
        "INTEGRATION_TESTER_FAILED",
    )

    allowed_tools: ClassVar[list[str]] = [
        "Read",
        "Edit",
        "Write",
        "Grep",
        "Glob",
        "Bash(cd:*)",
        "Bash(bash:*)",
        "Bash(go:*)",
        "Bash(npm:*)",
        "Bash(npx:*)",
        "Bash(node:*)",
        "Bash(curl:*)",
    ]

    prompt_template: ClassVar[str] = "integration_tester.md.j2"


__all__ = ["IntegrationTester"]
