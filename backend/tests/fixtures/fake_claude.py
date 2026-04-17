"""Fake `claude` CLI used by the agent subprocess tests.

Runs under ``python tests/fixtures/fake_claude.py``; its argv mirrors the
real Claude Code CLI's stream-json output enough for the base class to
exercise its streaming + completion-token detection logic.

Behavior is scenario-driven via the ``FAKE_CLAUDE_SCENARIO`` env var:

  Investigator scenarios:
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
  - ``cross_repo``: emits ``INVESTIGATOR_COMPLETE`` with cross-repo scope.

  Fixer scenarios (Agent 6):
  - ``fixer_complete``: appends ``## Fixer Notes`` to TRIAGE.md, creates
    a marker file ``fixer_fixed.txt``, prints ``FIXER_COMPLETE``.
  - ``fixer_failed``: appends ``## Fixer Failure`` to TRIAGE.md, prints
    ``FIXER_FAILED``.
  - ``fixer_escalation``: appends ``## Fixer Escalation`` to TRIAGE.md,
    does NOT print a completion token (simulates the Fixer stopping on
    deny-list contact).

  Tester scenarios (Agent 6):
  - ``tester_complete``: creates a test file, appends ``## Tester Notes``,
    prints ``TESTER_COMPLETE``.
  - ``tester_failed``: appends ``## Tester Failure``, prints ``TESTER_FAILED``.
  - ``tester_writes_prod_code``: creates a TEST file AND modifies a
    non-test file (``src/handler.go``) to exercise the orchestrator's
    post-run safety check. Appends ``## Tester Notes`` and prints
    ``TESTER_COMPLETE``.

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


def _append_section(path: Path, name: str, body: str) -> None:
    """Minimal TRIAGE.md ``## name`` appender (fixture-local duplicate of
    ``ra_triage.triage_md.parser.append`` — keeping the fixture free of
    cross-package imports).
    """
    block = f"## {name}\n\n{body.rstrip()}\n"
    if path.exists() and path.stat().st_size > 0:
        existing = path.read_text(encoding="utf-8")
        path.write_text(existing.rstrip("\n") + "\n\n" + block, encoding="utf-8")
    else:
        path.write_text(block, encoding="utf-8")


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

    # ---- Fixer scenarios ---------------------------------------------
    elif scenario == "fixer_complete":
        _append_section(
            path,
            "Fixer Notes",
            "- Files changed: fixer_fixed.txt (touched)\n"
            "- Key decisions: minimal reproducer patch\n"
            "- Heads-up for Tester: regression test should assert file presence\n",
        )
        # Touch a file so the orchestrator's git diff shows work was done.
        (cwd / "fixer_fixed.txt").write_text(
            "stubbed fix by fake Fixer\n", encoding="utf-8"
        )
    elif scenario == "fixer_failed":
        _append_section(
            path,
            "Fixer Failure",
            "- Diagnosis: could not locate referenced function.\n"
            "- What I tried: grep, glob on the described path.\n",
        )
    elif scenario == "fixer_escalation":
        _append_section(
            path,
            "Fixer Escalation",
            "- Reason: fix would require modifying a deny-list path.\n"
            "- What I need: human approval to proceed.\n",
        )

    # ---- Tester scenarios --------------------------------------------
    elif scenario == "tester_complete":
        # Ensure the test file location looks like a test (matches the
        # safety allowlist).
        test_dir = cwd / "tests"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "regression_test.go").write_text(
            "// stubbed regression test\n", encoding="utf-8"
        )
        _append_section(
            path,
            "Tester Notes",
            "- Tests added (file:path): tests/regression_test.go\n"
            "- Suite result: 1 passed\n"
            "- Any flakes observed: none\n",
        )
    elif scenario == "tester_failed":
        _append_section(
            path,
            "Tester Failure",
            "- Diagnosis: fix handles nil but returns 500 instead of 404.\n"
            "- Evidence: test_name output.\n",
        )
    elif scenario == "tester_writes_prod_code":
        test_dir = cwd / "tests"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "regression_test.go").write_text(
            "// stubbed regression test\n", encoding="utf-8"
        )
        # Prod-code modification — the orchestrator should revert this.
        src_dir = cwd / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        prod_file = src_dir / "handler.go"
        # Append (or create) something the Tester shouldn't be touching.
        existing = (
            prod_file.read_text(encoding="utf-8") if prod_file.exists() else ""
        )
        prod_file.write_text(existing + "// Tester edit\n", encoding="utf-8")
        _append_section(
            path,
            "Tester Notes",
            "- Tests added (file:path): tests/regression_test.go\n"
            "- Also edited src/handler.go (should be reverted by orchestrator)\n",
        )


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
    # mirrors how the real CLI produces it. The token is scenario-keyed;
    # ``fixer_escalation`` deliberately emits no token so the base class
    # treats the run as a crash (pipeline then routes to BLOCKED on the
    # ``## Fixer Escalation`` section regardless).
    token_by_scenario: dict[str, str | None] = {
        "complete": "INVESTIGATOR_COMPLETE",
        "escalation": "INVESTIGATOR_COMPLETE",
        "cross_repo": "INVESTIGATOR_COMPLETE",
        "malformed_scope": "INVESTIGATOR_COMPLETE",
        "failed": "INVESTIGATOR_FAILED",
        "fixer_complete": "FIXER_COMPLETE",
        "fixer_failed": "FIXER_FAILED",
        "fixer_escalation": None,
        "tester_complete": "TESTER_COMPLETE",
        "tester_failed": "TESTER_FAILED",
        "tester_writes_prod_code": "TESTER_COMPLETE",
    }
    token = token_by_scenario.get(scenario, "INVESTIGATOR_COMPLETE")
    if token is not None:
        emit(
            assistant_event(
                [text_block(f"Run complete. {token}")]
            )
        )
    else:
        emit(
            assistant_event(
                [text_block("Stopping before completion.")]
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
