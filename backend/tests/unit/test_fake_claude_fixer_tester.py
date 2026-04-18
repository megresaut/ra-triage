"""Smoke tests for the Fixer/Tester scenarios in ``tests/fixtures/fake_claude.py``.

Exercises the real ``ClaudeCodeAgent`` subprocess path end-to-end
(via the test CLI override), confirming:

  - ``fixer_complete`` scenario → ``FIXER_COMPLETE`` token, TRIAGE.md
    gains ``## Fixer Notes``, the marker file ``fixer_fixed.txt`` is
    created in the worktree.
  - ``fixer_failed`` scenario → ``FIXER_FAILED`` token, TRIAGE.md
    gains ``## Fixer Failure``.
  - ``tester_complete`` scenario → ``TESTER_COMPLETE`` token, TRIAGE.md
    gains ``## Tester Notes``, test file created.
  - ``tester_failed`` scenario → ``TESTER_FAILED`` token.
  - ``tester_writes_prod_code`` scenario → ``TESTER_COMPLETE`` token
    plus a prod-file mutation that the orchestrator would revert.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest

from ra_triage.agents import base as base_module
from ra_triage.agents.base import ClaudeCodeAgent

FAKE_CLI = Path(__file__).resolve().parents[1] / "fixtures" / "fake_claude.py"


class _FakeBug:
    def __init__(self, bug_id: int = 42) -> None:
        self.id = bug_id


class _FixerForTest(ClaudeCodeAgent):
    role: ClassVar[str] = "fixer"
    completion_tokens: ClassVar[tuple[str, ...]] = (
        "FIXER_COMPLETE",
        "FIXER_FAILED",
    )
    allowed_tools: ClassVar[list[str]] = ["Read", "Write"]
    prompt_template: ClassVar[str] = "fixer.md.j2"
    max_turns: ClassVar[int] = 5
    timeout_seconds: ClassVar[int] = 15


class _TesterForTest(ClaudeCodeAgent):
    role: ClassVar[str] = "tester"
    completion_tokens: ClassVar[tuple[str, ...]] = (
        "TESTER_COMPLETE",
        "TESTER_FAILED",
    )
    allowed_tools: ClassVar[list[str]] = ["Read", "Write"]
    prompt_template: ClassVar[str] = "tester.md.j2"
    max_turns: ClassVar[int] = 5
    timeout_seconds: ClassVar[int] = 15


def _fixer_context(bug_id: int) -> dict[str, Any]:
    return {
        "attempt": 1,
        "bug": {"id": bug_id, "title": "test bug"},
        "worktree_path": "/tmp/wt",
        "build_command": "go build ./...",
        "deny_list": {"paths": ["**/auth/**"], "categories": ["auth"]},
        "escalation_context": None,
        "conflict_context": None,
    }


def _tester_context(bug_id: int) -> dict[str, Any]:
    return {
        "attempt": 1,
        "bug": {"id": bug_id, "title": "test bug"},
        "worktree_path": "/tmp/wt",
        "test_command": "go test ./...",
        "test_database_url": "postgres://localhost/testdb",
        "triage_md_path": "/tmp/wt/TRIAGE.md",
    }


@pytest.fixture(autouse=True)
def _override_claude(monkeypatch: pytest.MonkeyPatch):
    base_module.set_claude_cli_override([sys.executable, str(FAKE_CLI)])
    yield
    base_module.set_claude_cli_override(None)


async def test_fake_claude_fixer_complete_scenario(
    triage_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "fixer_complete")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    result = await _FixerForTest().run(
        bug=_FakeBug(1), worktree=worktree, context=_fixer_context(1)
    )

    assert result.completion_token == "FIXER_COMPLETE"
    assert (worktree / "fixer_fixed.txt").exists()
    triage = (worktree / "TRIAGE.md").read_text(encoding="utf-8")
    assert "## Fixer Notes" in triage


async def test_fake_claude_fixer_failed_scenario(
    triage_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "fixer_failed")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    result = await _FixerForTest().run(
        bug=_FakeBug(2), worktree=worktree, context=_fixer_context(2)
    )

    assert result.completion_token == "FIXER_FAILED"
    triage = (worktree / "TRIAGE.md").read_text(encoding="utf-8")
    assert "## Fixer Failure" in triage


async def test_fake_claude_tester_complete_scenario(
    triage_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "tester_complete")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    result = await _TesterForTest().run(
        bug=_FakeBug(3), worktree=worktree, context=_tester_context(3)
    )

    assert result.completion_token == "TESTER_COMPLETE"
    assert (worktree / "tests" / "regression_test.go").exists()
    triage = (worktree / "TRIAGE.md").read_text(encoding="utf-8")
    assert "## Tester Notes" in triage


async def test_fake_claude_tester_failed_scenario(
    triage_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "tester_failed")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    result = await _TesterForTest().run(
        bug=_FakeBug(4), worktree=worktree, context=_tester_context(4)
    )

    assert result.completion_token == "TESTER_FAILED"
    triage = (worktree / "TRIAGE.md").read_text(encoding="utf-8")
    assert "## Tester Failure" in triage


async def test_fake_claude_tester_writes_prod_code_scenario(
    triage_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fake Tester scenario that mutates a prod file — used by the
    orchestrator safety tests."""
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "tester_writes_prod_code")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    result = await _TesterForTest().run(
        bug=_FakeBug(5), worktree=worktree, context=_tester_context(5)
    )

    assert result.completion_token == "TESTER_COMPLETE"
    # Both a test file AND a prod file got written.
    assert (worktree / "tests" / "regression_test.go").exists()
    assert (worktree / "src" / "handler.go").exists()
    prod = (worktree / "src" / "handler.go").read_text(encoding="utf-8")
    assert "Tester edit" in prod
