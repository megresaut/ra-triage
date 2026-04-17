"""Fake `claude` CLI used by the agent subprocess tests.

Runs under ``python tests/fixtures/fake_claude.py``; its argv mirrors the
real Claude Code CLI's stream-json output enough for the base class to
exercise its streaming + completion-token detection logic.

Behavior is scenario-driven via the ``FAKE_CLAUDE_SCENARIO`` env var:

  - ``complete`` (default): emits a few stream-json lines including an
    assistant ``text`` block containing ``INVESTIGATOR_COMPLETE``, and
    writes a TRIAGE.md with all four canonical sections.
  - ``escalation``: emits ``INVESTIGATOR_COMPLETE`` AND writes a TRIAGE.md
    that includes an ``## Escalation Brief`` section.
  - ``failed``: emits ``INVESTIGATOR_FAILED`` after writing an
    ``## Investigator Failure`` section.
  - ``timeout``: sleeps forever so the 30-minute timeout fires (tests
    shorten ``timeout_seconds`` to a few seconds to exercise this).
  - ``malformed_scope``: emits ``INVESTIGATOR_COMPLETE`` with a TRIAGE.md
    whose Scope section has no parseable ``scope:`` key.

The subprocess writes TRIAGE.md into ``os.getcwd()`` (which equals the
worktree path the base class passes in as ``cwd``). That exercises the
contract that the agent writes into the worktree, not the prompts dir.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

SCENARIO = os.environ.get("FAKE_CLAUDE_SCENARIO", "complete")


def emit(obj: dict[str, object]) -> None:
    """Print one JSON object on its own line + flush (stream-json format)."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def text_block(text: str) -> dict[str, object]:
    return {"type": "text", "text": text}


def tool_use_block(
    name: str, tool_input: dict[str, object]
) -> dict[str, object]:
    return {"type": "tool_use", "name": name, "input": tool_input}


def assistant_event(content: list[dict[str, object]]) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": content},
    }


def system_event(subtype: str) -> dict[str, object]:
    return {"type": "system", "subtype": subtype}


def write_triage_md(scenario: str, cwd: Path) -> None:
    """Write a TRIAGE.md under ``cwd`` matching the scenario."""
    path = cwd / "TRIAGE.md"

    if scenario == "complete":
        path.write_text(
            "## Root Cause\n\n"
            "src/handler.py:42 — missing nil check on user lookup\n\n"
            "## Scope\n\n"
            "scope: single-repo\n"
            'affected_repos: ["avm-backend"]\n'
            "deny_list_hit: false\n\n"
            "## Analysis\n\n"
            "The handler returns before the lookup completes.\n\n"
            "## Recommended Fix Approach\n\n"
            "Guard the lookup and return a 404 on missing user.\n",
            encoding="utf-8",
        )
    elif scenario == "escalation":
        path.write_text(
            "## Root Cause\n\n"
            "src/auth/login.py:12 — session token mishandled\n\n"
            "## Scope\n\n"
            "scope: single-repo\n"
            'affected_repos: ["avm-backend"]\n'
            "deny_list_hit: true\n\n"
            "## Analysis\n\n"
            "The bug lands in the deny-listed auth module.\n\n"
            "## Escalation Brief\n\n"
            "- **TL;DR:** Auth change required.\n"
            "- **Why blocked:** deny_list path auth/** matched.\n"
            "- **What I need from you:** Approve Recommended | Take Over Manually\n",
            encoding="utf-8",
        )
    elif scenario == "failed":
        path.write_text(
            "## Investigator Failure\n\nCould not reproduce the issue.\n",
            encoding="utf-8",
        )
    elif scenario == "cross_repo":
        path.write_text(
            "## Root Cause\n\n"
            "src/api.go:10 returns wrong shape; src/page.tsx:20 consumes old shape\n\n"
            "## Scope\n\n"
            "scope: cross-repo\n"
            'affected_repos: ["avm-backend", "builder-frontend"]\n'
            "deny_list_hit: false\n\n"
            "## Analysis\n\nShape drift between BE and FE.\n\n"
            "## Recommended Fix Approach\n\nAlign both sides on the contract.\n",
            encoding="utf-8",
        )
    elif scenario == "malformed_scope":
        path.write_text(
            "## Root Cause\n\nsomewhere\n\n"
            "## Scope\n\n(no parseable keys here)\n\n"
            "## Analysis\n\nn/a\n\n"
            "## Recommended Fix Approach\n\nn/a\n",
            encoding="utf-8",
        )
    elif scenario == "timeout":
        # No TRIAGE.md — the test checks timeout handling.
        pass


def main() -> int:
    scenario = SCENARIO

    emit(system_event("init"))
    emit(
        assistant_event(
            [
                text_block("I will investigate the bug now."),
                tool_use_block(
                    "Read", {"file_path": "/path/to/src/handler.py"}
                ),
            ]
        )
    )

    if scenario == "timeout":
        # Intentionally hang so the base class's wait_for() fires.
        time.sleep(3600)
        return 0

    cwd = Path(os.getcwd())
    write_triage_md(scenario, cwd)

    # Emit the completion token as part of an assistant text block —
    # mirrors how the real CLI produces it.
    if scenario == "failed":
        emit(
            assistant_event(
                [text_block("I could not complete. INVESTIGATOR_FAILED")]
            )
        )
    else:
        emit(
            assistant_event(
                [text_block("Investigation complete. INVESTIGATOR_COMPLETE")]
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
