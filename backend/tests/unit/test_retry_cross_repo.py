"""Tests for Agent 12's cross-repo retry handling.

Each cross-repo role (``fix_designer``, ``fixer_frontend``,
``fixer_backend``, ``integration_tester``) retried via
``retry_last_agent`` must:
  - re-run ONLY that role.
  - increment attempt for that role only (other sides untouched).
  - transition the bug back to an appropriate working state.
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
def seed_cross_repo_worktrees(
    triage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stub worktree listing + creation for a cross-repo bug."""
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


def _make_cross_repo_bug(
    *,
    session: Any,
    notion_page_id: str,
    walk_to: BugState,
    scope: str = "cross-repo",
    affected_repos: list[str] | None = None,
) -> int:
    bug = repo_module.create_bug(
        session,
        notion_page_id=notion_page_id,
        project_id="ra_avm",
        title="cross-repo bug",
        description="d",
    )
    bug.scope = scope
    bug.affected_repos = json.dumps(
        affected_repos or ["avm-backend", "avm-frontend"]
    )
    session.flush()
    # Walk OPEN → TRIAGED → INVESTIGATING → DESIGNING → … → walk_to
    walk_map: dict[BugState, list[BugState]] = {
        BugState.DESIGNING: [
            BugState.TRIAGED,
            BugState.INVESTIGATING,
            BugState.DESIGNING,
        ],
        BugState.FIXING_PARALLEL: [
            BugState.TRIAGED,
            BugState.INVESTIGATING,
            BugState.DESIGNING,
            BugState.FIXING_PARALLEL,
        ],
        BugState.INTEGRATION_TESTING: [
            BugState.TRIAGED,
            BugState.INVESTIGATING,
            BugState.DESIGNING,
            BugState.FIXING_PARALLEL,
            BugState.INTEGRATION_TESTING,
        ],
        BugState.BLOCKED: [
            BugState.TRIAGED,
            BugState.INVESTIGATING,
            BugState.DESIGNING,
            BugState.FIXING_PARALLEL,
            BugState.BLOCKED,
        ],
    }
    for nxt in walk_map[walk_to]:
        repo_module.update_bug_state(session, bug, nxt)
    session.commit()
    return int(bug.id)


def _seed_triage_md_with_contract(worktree: Path) -> None:
    (worktree / "TRIAGE.md").write_text(
        "## Scope\n\n"
        "scope: cross-repo\n"
        'affected_repos: ["avm-backend", "avm-frontend"]\n'
        "deny_list_hit: false\n\n"
        "## Cross-Repo Contract\n\n"
        "- backend exposes: GET /api/x\n"
        "- frontend consumes: /api/x\n",
        encoding="utf-8",
    )


def _fake_agent(
    *,
    role: str,
    completion_token: str | None,
    captured: dict[str, Any],
) -> Any:
    """Return a fake agent class that records its invocation + returns AgentResult."""

    class _FakeAgent:
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

    _FakeAgent.role = role  # type: ignore[attr-defined]
    return _FakeAgent


# --- fix_designer retry ----------------------------------------------------


async def test_retry_fix_designer_reruns_only_designer_with_attempt_plus_one(
    alembic_db: Path,
    triage_home: Path,
    seed_cross_repo_worktrees: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLOCKED + fix_designer FAILED → rerun Designer with attempt=2."""
    session = engine_module.SessionLocal()
    bug_id = _make_cross_repo_bug(
        session=session, notion_page_id="retry-design", walk_to=BugState.BLOCKED
    )
    repo_module.record_agent_run(
        session,
        bug_id=bug_id,
        agent_role="fix_designer",
        attempt=1,
        started_at=_now(),
        ended_at=_now(),
        exit_code=1,
        completion_token="FIX_DESIGNER_FAILED",
        log_path="/tmp/d.log",
        prompt_path="/tmp/d.md",
    )
    session.commit()
    session.close()

    be_worktree = triage_home / "worktrees" / str(bug_id) / "avm-backend"
    be_worktree.mkdir(parents=True, exist_ok=True)
    (triage_home / "worktrees" / str(bug_id) / "avm-frontend").mkdir(
        parents=True, exist_ok=True
    )
    _seed_triage_md_with_contract(be_worktree)

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        pipeline_module,
        "FixDesigner",
        _fake_agent(
            role="fix_designer",
            completion_token="FIX_DESIGNER_COMPLETE",
            captured=captured,
        ),
    )

    await pipeline_module.retry_last_agent(bug_id)

    assert captured["calls"] == [{"role": "fix_designer", "attempt": 2}]


# --- fixer_frontend retry (re-runs ONLY that side) -------------------------


async def test_retry_fixer_frontend_reruns_only_fe_and_leaves_be_attempt_alone(
    alembic_db: Path,
    triage_home: Path,
    seed_cross_repo_worktrees: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry fixer_frontend after FAILED: re-run ONLY FE, BE attempts untouched."""
    session = engine_module.SessionLocal()
    bug_id = _make_cross_repo_bug(
        session=session,
        notion_page_id="retry-fe",
        walk_to=BugState.BLOCKED,
    )
    # Record initial runs: FE failed at attempt 1; BE succeeded at attempt 1.
    repo_module.record_agent_run(
        session,
        bug_id=bug_id,
        agent_role="fixer_backend",
        attempt=1,
        started_at=_now(),
        ended_at=_now(),
        exit_code=0,
        completion_token="FIXER_BACKEND_COMPLETE",
        log_path="/tmp/be.log",
        prompt_path="/tmp/be.md",
    )
    repo_module.record_agent_run(
        session,
        bug_id=bug_id,
        agent_role="fixer_frontend",
        attempt=1,
        started_at=_now(),
        ended_at=_now(),
        exit_code=1,
        completion_token="FIXER_FRONTEND_FAILED",
        log_path="/tmp/fe.log",
        prompt_path="/tmp/fe.md",
    )
    session.commit()
    session.close()

    be_worktree = triage_home / "worktrees" / str(bug_id) / "avm-backend"
    fe_worktree = triage_home / "worktrees" / str(bug_id) / "avm-frontend"
    be_worktree.mkdir(parents=True, exist_ok=True)
    fe_worktree.mkdir(parents=True, exist_ok=True)
    _seed_triage_md_with_contract(be_worktree)

    fe_captured: dict[str, Any] = {}
    be_captured: dict[str, Any] = {}
    monkeypatch.setattr(
        pipeline_module,
        "FixerFrontend",
        _fake_agent(
            role="fixer_frontend",
            completion_token="FIXER_FRONTEND_COMPLETE",
            captured=fe_captured,
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "FixerBackend",
        _fake_agent(
            role="fixer_backend",
            completion_token="FIXER_BACKEND_COMPLETE",
            captured=be_captured,
        ),
    )

    await pipeline_module.retry_last_agent(bug_id)

    # Only FE re-ran with attempt=2. BE was NOT re-run.
    assert fe_captured.get("calls") == [
        {"role": "fixer_frontend", "attempt": 2}
    ]
    assert be_captured.get("calls", []) == [], (
        "Retry fixer_frontend must not re-spawn fixer_backend"
    )

    # BE's attempt counter stays at 1 in agent_runs.
    session = engine_module.SessionLocal()
    try:
        runs = repo_module.list_agent_runs(session, bug_id)
        be_attempts = [r.attempt for r in runs if r.agent_role == "fixer_backend"]
        fe_attempts = [r.attempt for r in runs if r.agent_role == "fixer_frontend"]
        assert be_attempts == [1]
        assert fe_attempts == [1, 2]
    finally:
        session.close()


# --- fixer_backend retry ---------------------------------------------------


async def test_retry_fixer_backend_reruns_only_be(
    alembic_db: Path,
    triage_home: Path,
    seed_cross_repo_worktrees: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry fixer_backend after FAILED: re-run ONLY BE."""
    session = engine_module.SessionLocal()
    bug_id = _make_cross_repo_bug(
        session=session,
        notion_page_id="retry-be",
        walk_to=BugState.BLOCKED,
    )
    repo_module.record_agent_run(
        session,
        bug_id=bug_id,
        agent_role="fixer_frontend",
        attempt=1,
        started_at=_now(),
        ended_at=_now(),
        exit_code=0,
        completion_token="FIXER_FRONTEND_COMPLETE",
        log_path="/tmp/fe.log",
        prompt_path="/tmp/fe.md",
    )
    repo_module.record_agent_run(
        session,
        bug_id=bug_id,
        agent_role="fixer_backend",
        attempt=1,
        started_at=_now(),
        ended_at=_now(),
        exit_code=1,
        completion_token="FIXER_BACKEND_FAILED",
        log_path="/tmp/be.log",
        prompt_path="/tmp/be.md",
    )
    session.commit()
    session.close()

    be_worktree = triage_home / "worktrees" / str(bug_id) / "avm-backend"
    fe_worktree = triage_home / "worktrees" / str(bug_id) / "avm-frontend"
    be_worktree.mkdir(parents=True, exist_ok=True)
    fe_worktree.mkdir(parents=True, exist_ok=True)
    _seed_triage_md_with_contract(be_worktree)

    fe_captured: dict[str, Any] = {}
    be_captured: dict[str, Any] = {}
    monkeypatch.setattr(
        pipeline_module,
        "FixerFrontend",
        _fake_agent(
            role="fixer_frontend",
            completion_token="FIXER_FRONTEND_COMPLETE",
            captured=fe_captured,
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "FixerBackend",
        _fake_agent(
            role="fixer_backend",
            completion_token="FIXER_BACKEND_COMPLETE",
            captured=be_captured,
        ),
    )

    await pipeline_module.retry_last_agent(bug_id)

    assert be_captured.get("calls") == [
        {"role": "fixer_backend", "attempt": 2}
    ]
    assert fe_captured.get("calls", []) == []


# --- integration_tester retry ---------------------------------------------


async def test_retry_integration_tester_reruns_only_integration(
    alembic_db: Path,
    triage_home: Path,
    seed_cross_repo_worktrees: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry integration_tester after FAILED: re-run only the IT."""
    session = engine_module.SessionLocal()
    bug_id = _make_cross_repo_bug(
        session=session,
        notion_page_id="retry-it",
        walk_to=BugState.BLOCKED,
    )
    for role, tok in [
        ("fix_designer", "FIX_DESIGNER_COMPLETE"),
        ("fixer_frontend", "FIXER_FRONTEND_COMPLETE"),
        ("fixer_backend", "FIXER_BACKEND_COMPLETE"),
    ]:
        repo_module.record_agent_run(
            session,
            bug_id=bug_id,
            agent_role=role,
            attempt=1,
            started_at=_now(),
            ended_at=_now(),
            exit_code=0,
            completion_token=tok,
            log_path=f"/tmp/{role}.log",
            prompt_path=f"/tmp/{role}.md",
        )
    repo_module.record_agent_run(
        session,
        bug_id=bug_id,
        agent_role="integration_tester",
        attempt=1,
        started_at=_now(),
        ended_at=_now(),
        exit_code=1,
        completion_token="INTEGRATION_TESTER_FAILED",
        log_path="/tmp/it.log",
        prompt_path="/tmp/it.md",
    )
    session.commit()
    session.close()

    be_worktree = triage_home / "worktrees" / str(bug_id) / "avm-backend"
    fe_worktree = triage_home / "worktrees" / str(bug_id) / "avm-frontend"
    be_worktree.mkdir(parents=True, exist_ok=True)
    fe_worktree.mkdir(parents=True, exist_ok=True)
    _seed_triage_md_with_contract(be_worktree)

    it_captured: dict[str, Any] = {}
    monkeypatch.setattr(
        pipeline_module,
        "IntegrationTester",
        _fake_agent(
            role="integration_tester",
            completion_token="INTEGRATION_TESTER_COMPLETE",
            captured=it_captured,
        ),
    )
    # Stub pkill so the test doesn't actually shell out.
    async def _fake_pkill(*, be_port: int, fe_port: int) -> None:
        return None

    monkeypatch.setattr(
        pipeline_module, "_pkill_integration_ports", _fake_pkill
    )

    await pipeline_module.retry_last_agent(bug_id)

    assert it_captured.get("calls") == [
        {"role": "integration_tester", "attempt": 2}
    ]

    session = engine_module.SessionLocal()
    try:
        reloaded = repo_module.get_bug(session, bug_id)
        assert reloaded is not None
        # IT succeeded → advanced to READY_FOR_REVIEW.
        assert reloaded.state == BugState.READY_FOR_REVIEW.value
    finally:
        session.close()
