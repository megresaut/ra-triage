"""Tests for ``ra_triage.agents.base.ClaudeCodeAgent``.

Strategy: override the CLI argv to invoke ``python tests/fixtures/fake_claude.py``
so the base class's subprocess streaming, log writes, event publishing,
and completion-token detection can be exercised end-to-end without
spawning the real Claude CLI.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest

from ra_triage.agents import base as base_module
from ra_triage.agents.base import ClaudeCodeAgent
from ra_triage.utils.events import bus, topic_bug_logs

FAKE_CLI = (
    Path(__file__).resolve().parents[1] / "fixtures" / "fake_claude.py"
)


class _FakeBug:
    def __init__(self, bug_id: int = 42) -> None:
        self.id = bug_id


class _InvestigatorForTest(ClaudeCodeAgent):
    """Investigator clone that uses a template written inline — keeps the
    test independent of tweaks to the real prompt file."""

    role: ClassVar[str] = "investigator"
    completion_tokens: ClassVar[tuple[str, ...]] = (
        "INVESTIGATOR_COMPLETE",
        "INVESTIGATOR_FAILED",
    )
    allowed_tools: ClassVar[list[str]] = ["Read", "Grep", "Glob"]
    prompt_template: ClassVar[str] = "investigator.md.j2"
    max_turns: ClassVar[int] = 5
    timeout_seconds: ClassVar[int] = 15  # short for tests


def _jinja_context(bug_id: int) -> dict[str, Any]:
    """Minimal context matching the real investigator template."""
    return {
        "attempt": 1,
        "bug": {
            "id": bug_id,
            "title": "Test bug",
            "description": "Thing broke",
            "repro_steps": "1. do x",
            "severity": "low",
            "reporter": "tester",
        },
        "project": {"display_name": "TestProj"},
        "repos": [
            {"name": "demo", "role": "backend", "worktree_path": "/tmp/wt"}
        ],
        "deny_list": {
            "paths": ["**/auth/**"],
            "categories": ["auth"],
        },
        "rejection_feedback": None,
        "triage_md_path": "/tmp/wt/TRIAGE.md",
    }


@pytest.fixture(autouse=True)
def _override_claude(monkeypatch: pytest.MonkeyPatch):
    """Route the subprocess spawn to the fake CLI for every test here."""
    # Route argv to python <fake_claude.py>; the real agent argv shape (with
    # -p prompt, --output-format stream-json, etc.) is still appended by
    # the base class, but the fake script ignores its argv completely.
    base_module.set_claude_cli_override([sys.executable, str(FAKE_CLI)])
    yield
    base_module.set_claude_cli_override(None)


async def test_successful_run_records_completion_token(
    triage_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: fake CLI emits INVESTIGATOR_COMPLETE and writes TRIAGE.md."""
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "complete")

    worktree = tmp_path / "wt"
    worktree.mkdir()

    agent = _InvestigatorForTest()
    result = await agent.run(
        bug=_FakeBug(),
        worktree=worktree,
        context=_jinja_context(42),
    )

    assert result.exit_code == 0
    assert result.completion_token == "INVESTIGATOR_COMPLETE"
    assert result.prompt_path.exists()
    assert result.log_path.exists()
    # TRIAGE.md was written by the fake CLI into worktree; the base class
    # should have read it into result.triage_md.
    assert "## Root Cause" in result.triage_md
    assert "## Scope" in result.triage_md

    # Log file should include the human-readable completion text.
    log_text = result.log_path.read_text(encoding="utf-8")
    assert "INVESTIGATOR_COMPLETE" in log_text


async def test_run_publishes_agent_stdout_on_bus(
    triage_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every human-readable stdout line fans out on ``bugs:{id}:logs``."""
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "complete")

    worktree = tmp_path / "wt"
    worktree.mkdir()

    queue = await bus.subscribe(topic_bug_logs(42))

    agent = _InvestigatorForTest()
    result = await agent.run(
        bug=_FakeBug(),
        worktree=worktree,
        context=_jinja_context(42),
    )
    assert result.completion_token == "INVESTIGATOR_COMPLETE"

    # Drain events with a timeout — we expect multiple stdout payloads.
    events: list[dict[str, Any]] = []
    for _ in range(20):
        try:
            ev = await asyncio.wait_for(queue.get(), timeout=0.2)
        except TimeoutError:
            break
        events.append(ev)

    assert events, "Expected at least one agent_stdout event"
    assert all(e["type"] == "agent_stdout" for e in events)
    assert all(e["bug_id"] == 42 for e in events)
    assert any("INVESTIGATOR_COMPLETE" in e["text"] for e in events)

    await bus.unsubscribe(topic_bug_logs(42), queue)


async def test_failed_completion_token_detected(
    triage_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fake CLI's ``failed`` scenario → completion_token == INVESTIGATOR_FAILED."""
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "failed")

    worktree = tmp_path / "wt"
    worktree.mkdir()

    agent = _InvestigatorForTest()
    result = await agent.run(
        bug=_FakeBug(),
        worktree=worktree,
        context=_jinja_context(42),
    )
    assert result.completion_token == "INVESTIGATOR_FAILED"


async def test_prompt_is_persisted_under_triage_home(
    triage_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rendered prompts land at ``<TRIAGE_HOME>/prompts/<bug_id>/<role>.<attempt>.md``."""
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "complete")

    worktree = tmp_path / "wt"
    worktree.mkdir()

    agent = _InvestigatorForTest()
    result = await agent.run(
        bug=_FakeBug(99),
        worktree=worktree,
        context=_jinja_context(99),
    )

    expected = triage_home / "prompts" / "99" / "investigator.1.md"
    assert result.prompt_path == expected
    assert expected.read_text(encoding="utf-8").startswith("ROLE: Investigator")


async def test_timeout_triggers_clean_termination(
    triage_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent with a tiny timeout + hanging CLI → exit_code=-1, token=None."""
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "timeout")

    worktree = tmp_path / "wt"
    worktree.mkdir()

    class _ShortTimeoutAgent(_InvestigatorForTest):
        timeout_seconds: ClassVar[int] = 1  # force timeout quickly

    agent = _ShortTimeoutAgent()
    result = await agent.run(
        bug=_FakeBug(7),
        worktree=worktree,
        context=_jinja_context(7),
    )

    assert result.completion_token is None
    assert result.exit_code == -1
    # The log file should mention the timeout.
    assert "TIMEOUT" in result.log_path.read_text(encoding="utf-8")


async def test_run_rejects_missing_attempt(tmp_path: Path) -> None:
    """context['attempt'] is required; absence raises before any subprocess spawn."""
    worktree = tmp_path / "wt"
    worktree.mkdir()

    agent = _InvestigatorForTest()
    bad_context = _jinja_context(1)
    del bad_context["attempt"]
    with pytest.raises(ValueError):
        await agent.run(bug=_FakeBug(1), worktree=worktree, context=bad_context)


def test_class_config_is_validated_at_construction() -> None:
    """Subclass missing a role / tokens / template → ValueError on __init__."""

    class _Broken(ClaudeCodeAgent):
        role = ""
        completion_tokens = ("X",)
        prompt_template = "x.md.j2"

    with pytest.raises(ValueError):
        _Broken()
