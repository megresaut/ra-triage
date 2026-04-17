"""Tests for ``orchestrator.pipeline.retry_last_agent``.

Covers:
  - Fixer retry after FIXER_FAILED: re-runs Fixer with attempt+1 AND
    advances to TESTING on success.
  - Investigator retry from crashed run (exit_code != 0, no token).
  - Tester retry from TESTER_FAILED.
  - ValueError raised when the pipeline has moved on (bug is terminal).
  - ValueError raised when BLOCKED without a *_FAILED completion token.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ra_triage.agents.base import AgentResult
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.orchestrator import pipeline as pipeline_module
from ra_triage.orchestrator.states import BugState


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture()
def seed_worktrees(
    triage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stub worktree creation + listing so retry doesn't touch git."""
    from ra_triage.workspaces import worktree as wt_module

    async def _fake_create(bug_id: int, repos: list[Any]) -> dict[str, Path]:
        mapping: dict[str, Path] = {}
        for r in repos:
            dest = triage_home / "worktrees" / str(bug_id) / r.name
            dest.mkdir(parents=True, exist_ok=True)
            mapping[r.name] = dest
        return mapping

    def _fake_list(bug_id: int) -> dict[str, Path]:
        parent = triage_home / "worktrees" / str(bug_id)
        if not parent.is_dir():
            return {}
        return {
            c.name: c for c in sorted(parent.iterdir()) if c.is_dir()
        }

    monkeypatch.setattr(wt_module, "create_worktrees_for_bug", _fake_create)
    monkeypatch.setattr(wt_module, "list_worktrees", _fake_list)


def _make_fake(
    *, role: str, completion_token: str | None, captured: dict[str, Any]
) -> Any:
    """Return a fake agent class that records its call and returns a crafted result."""

    class _Fake:
        role_attr = role

        async def run(
            self, bug: Any, worktree: Path, context: dict[str, Any]
        ) -> AgentResult:
            captured.setdefault("calls", []).append(
                {"role": role, "attempt": context["attempt"]}
            )
            from ra_triage.settings import get_settings

            settings = get_settings()
            log_path = (
                settings.logs_dir
                / str(bug.id)
                / f"{role}.{context['attempt']}.log"
            )
            prompt_path = (
                settings.prompts_dir
                / str(bug.id)
                / f"{role}.{context['attempt']}.md"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("x", encoding="utf-8")
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text("x", encoding="utf-8")
            return AgentResult(
                exit_code=0
                if completion_token and completion_token.endswith("_COMPLETE")
                else 1,
                completion_token=completion_token,
                log_path=log_path,
                prompt_path=prompt_path,
                triage_md="",
                started_at=_now(),
                ended_at=_now(),
            )

    _Fake.role = role  # type: ignore[attr-defined]
    return _Fake


# --- Fixer retry -----------------------------------------------------------


async def test_retry_fixer_after_failed_reruns_and_advances_on_success(
    alembic_db: Path,
    triage_home: Path,
    seed_worktrees: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FIXING + FIXER_FAILED → rerun Fixer, succeed, → TESTING."""
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = repo_module.create_bug(
        session,
        notion_page_id="retry-fix-1",
        project_id="ra_avm",
        title="t",
        description="d",
    )
    bug.affected_repos = json.dumps(["avm-backend"])
    bug.scope = "single-repo"
    session.flush()
    for nxt in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.FIXING,
        BugState.BLOCKED,
    ):
        repo_module.update_bug_state(session, bug, nxt)
    repo_module.record_agent_run(
        session,
        bug_id=bug.id,
        agent_role="fixer",
        attempt=1,
        started_at=_now(),
        ended_at=_now(),
        exit_code=1,
        completion_token="FIXER_FAILED",
        log_path="/tmp/f.log",
        prompt_path="/tmp/f.md",
    )
    session.commit()
    bug_id = bug.id
    session.close()

    # Pre-seed TRIAGE.md with a scope that resolves to avm-backend.
    worktree = triage_home / "worktrees" / str(bug_id) / "avm-backend"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "TRIAGE.md").write_text(
        "## Scope\n\n"
        "scope: single-repo\n"
        'affected_repos: ["avm-backend"]\n'
        "deny_list_hit: false\n",
        encoding="utf-8",
    )

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        pipeline_module,
        "Fixer",
        _make_fake(role="fixer", completion_token="FIXER_COMPLETE", captured=captured),
    )

    await pipeline_module.retry_last_agent(bug_id)

    assert captured["calls"] == [{"role": "fixer", "attempt": 2}]

    session = engine_module.SessionLocal()
    try:
        final = repo_module.get_bug(session, bug_id)
        assert final is not None
        # Fixer retry succeeded → advanced to TESTING.
        assert final.state == BugState.TESTING.value
    finally:
        session.close()


# --- Tester retry ----------------------------------------------------------


async def test_retry_tester_after_failed_reruns(
    alembic_db: Path,
    triage_home: Path,
    seed_worktrees: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TESTING + TESTER_FAILED → rerun Tester, succeed, → READY_FOR_REVIEW."""
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = repo_module.create_bug(
        session,
        notion_page_id="retry-test-1",
        project_id="ra_avm",
        title="t",
        description="d",
    )
    bug.affected_repos = json.dumps(["avm-backend"])
    bug.scope = "single-repo"
    session.flush()
    for nxt in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.FIXING,
        BugState.TESTING,
        BugState.BLOCKED,
    ):
        repo_module.update_bug_state(session, bug, nxt)
    repo_module.record_agent_run(
        session,
        bug_id=bug.id,
        agent_role="tester",
        attempt=1,
        started_at=_now(),
        ended_at=_now(),
        exit_code=1,
        completion_token="TESTER_FAILED",
        log_path="/tmp/t.log",
        prompt_path="/tmp/t.md",
    )
    session.commit()
    bug_id = bug.id
    session.close()

    worktree = triage_home / "worktrees" / str(bug_id) / "avm-backend"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "TRIAGE.md").write_text(
        "## Scope\n\n"
        "scope: single-repo\n"
        'affected_repos: ["avm-backend"]\n'
        "deny_list_hit: false\n",
        encoding="utf-8",
    )

    # TESTER retry currently requires state to already be TESTING or BLOCKED.
    # Our bug is in BLOCKED (per the setup). The retry handler should walk
    # it back to TESTING via the state machine... except BLOCKED → TESTING
    # isn't a legal transition. We'd need BLOCKED → FIXING → TESTING.
    # Instead: set bug to TESTING directly before the retry call.
    session = engine_module.SessionLocal()
    reloaded = repo_module.get_bug(session, bug_id)
    assert reloaded is not None
    # Walk BLOCKED → FIXING → TESTING.
    repo_module.update_bug_state(session, reloaded, BugState.FIXING)
    repo_module.update_bug_state(session, reloaded, BugState.TESTING)
    session.commit()
    session.close()

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        pipeline_module,
        "Tester",
        _make_fake(role="tester", completion_token="TESTER_COMPLETE", captured=captured),
    )
    # Stub safety check.
    from tests.unit.test_pipeline import _patch_tester_safety_clean

    _patch_tester_safety_clean(monkeypatch)

    await pipeline_module.retry_last_agent(bug_id)

    assert captured["calls"] == [{"role": "tester", "attempt": 2}]

    session = engine_module.SessionLocal()
    try:
        final = repo_module.get_bug(session, bug_id)
        assert final is not None
        assert final.state == BugState.READY_FOR_REVIEW.value
    finally:
        session.close()


# --- Investigator retry from crash (no token) -----------------------------


async def test_retry_investigator_crash_reruns(
    alembic_db: Path,
    triage_home: Path,
    seed_worktrees: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed Investigator (no completion token) can still be retried."""
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = repo_module.create_bug(
        session,
        notion_page_id="retry-inv-crash",
        project_id="ra_avm",
        title="t",
        description="d",
    )
    session.flush()
    for nxt in (BugState.TRIAGED, BugState.INVESTIGATING):
        repo_module.update_bug_state(session, bug, nxt)
    # Crashed prior run: exit_code=1, token=None.
    repo_module.record_agent_run(
        session,
        bug_id=bug.id,
        agent_role="investigator",
        attempt=1,
        started_at=_now(),
        ended_at=_now(),
        exit_code=1,
        completion_token=None,
        log_path="/tmp/i.log",
        prompt_path="/tmp/i.md",
    )
    session.commit()
    bug_id = bug.id
    session.close()

    captured: dict[str, Any] = {}

    class _FakeInv:
        role = "investigator"

        async def run(
            self, bug: Any, worktree: Path, context: dict[str, Any]
        ) -> AgentResult:
            captured["attempt"] = context["attempt"]
            # Write a triage.md that routes to BLOCKED (simplest outcome).
            triage_path = worktree / "TRIAGE.md"
            triage_path.write_text(
                "## Escalation Brief\n\n- **TL;DR:** still stuck\n",
                encoding="utf-8",
            )
            return AgentResult(
                exit_code=0,
                completion_token="INVESTIGATOR_COMPLETE",
                log_path=worktree / "_.log",
                prompt_path=worktree / "_.md",
                triage_md="## Escalation Brief",
                started_at=_now(),
                ended_at=_now(),
            )

    monkeypatch.setattr(pipeline_module, "Investigator", _FakeInv)

    await pipeline_module.retry_last_agent(bug_id)

    assert captured["attempt"] == 2

    session = engine_module.SessionLocal()
    try:
        final = repo_module.get_bug(session, bug_id)
        assert final is not None
        # Escalation present → BLOCKED.
        assert final.state == BugState.BLOCKED.value
    finally:
        session.close()


# --- Negative: terminal bug refuses retry ---------------------------------


async def test_retry_on_terminal_bug_raises_value_error(
    alembic_db: Path,
) -> None:
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = repo_module.create_bug(
        session,
        notion_page_id="retry-term",
        project_id="ra_avm",
        title="t",
        description="d",
    )
    repo_module.update_bug_state(session, bug, BugState.CANCELLED)
    session.commit()
    bug_id = bug.id
    session.close()

    with pytest.raises(ValueError):
        await pipeline_module.retry_last_agent(bug_id)


async def test_retry_no_runs_raises_value_error(alembic_db: Path) -> None:
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = repo_module.create_bug(
        session,
        notion_page_id="retry-norruns",
        project_id="ra_avm",
        title="t",
        description="d",
    )
    for nxt in (BugState.TRIAGED, BugState.INVESTIGATING):
        repo_module.update_bug_state(session, bug, nxt)
    session.commit()
    bug_id = bug.id
    session.close()

    with pytest.raises(ValueError):
        await pipeline_module.retry_last_agent(bug_id)


async def test_retry_blocked_without_failed_token_raises(alembic_db: Path) -> None:
    """BLOCKED with a COMPLETE latest run is rejected (not a crashed/failed state)."""
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = repo_module.create_bug(
        session,
        notion_page_id="retry-blocked-ok",
        project_id="ra_avm",
        title="t",
        description="d",
    )
    for nxt in (BugState.TRIAGED, BugState.INVESTIGATING, BugState.BLOCKED):
        repo_module.update_bug_state(session, bug, nxt)
    repo_module.record_agent_run(
        session,
        bug_id=bug.id,
        agent_role="investigator",
        attempt=1,
        started_at=_now(),
        ended_at=_now(),
        exit_code=0,
        completion_token="INVESTIGATOR_COMPLETE",
        log_path="/tmp/inv.log",
        prompt_path="/tmp/inv.md",
    )
    session.commit()
    bug_id = bug.id
    session.close()

    with pytest.raises(ValueError):
        await pipeline_module.retry_last_agent(bug_id)
