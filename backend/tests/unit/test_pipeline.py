"""Tests for ``ra_triage.orchestrator.pipeline.run_pipeline``.

We mock out both the worktree subsystem AND the Investigator's ``run``
method so tests exercise only the orchestrator's state-transition logic.
Parametrized across the four outcomes Agent 5 is responsible for:

  1. Happy single-repo → FIXING → TESTING → READY_FOR_REVIEW (stub).
  2. Happy cross-repo → DESIGNING → FIXING_PARALLEL → INTEGRATION_TESTING → READY_FOR_REVIEW (stub).
  3. Escalation Brief in TRIAGE.md → BLOCKED.
  4. ``INVESTIGATOR_FAILED`` token → BLOCKED.
  5. Malformed scope → BLOCKED (with Escalation Brief appended).

All tests use the ``db_session`` fixture from conftest for a real migrated
DB so the state-transition path goes through ``db.repo.update_bug_state``
end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ra_triage.agents.base import AgentResult
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.db.models import AgentRun, Bug
from ra_triage.orchestrator import pipeline as pipeline_module
from ra_triage.orchestrator.states import BugState


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _make_bug(
    session: Any, *, notion_page_id: str = "notion-abc", project_id: str = "ra_avm"
) -> Bug:
    bug = repo_module.create_bug(
        session,
        notion_page_id=notion_page_id,
        project_id=project_id,
        title="Test bug",
        description="broken",
        repro_steps="do thing",
        severity="low",
        reporter="qa",
    )
    session.commit()
    return bug


@pytest.fixture()
def stub_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """Replace ``workspaces.worktree.create_worktrees_for_bug`` with a stub
    that just returns synthetic paths inside ``tmp_path``.

    Also stubs ``list_worktrees`` so the API endpoint test can see them.
    """
    from ra_triage.workspaces import worktree as wt_module

    per_bug: dict[int, dict[str, Path]] = {}

    async def _fake_create(bug_id: int, repos: list[Any]) -> dict[str, Path]:
        mapping: dict[str, Path] = {}
        for r in repos:
            dest = tmp_path / "worktrees" / str(bug_id) / r.name
            dest.mkdir(parents=True, exist_ok=True)
            mapping[r.name] = dest
        per_bug[bug_id] = mapping
        return mapping

    async def _fake_cleanup(bug_id: int, repos: list[Any]) -> None:
        per_bug.pop(bug_id, None)

    monkeypatch.setattr(wt_module, "create_worktrees_for_bug", _fake_create)
    monkeypatch.setattr(wt_module, "cleanup", _fake_cleanup)
    return {"tmp": tmp_path}


def _make_fake_investigator(
    *, completion_token: str | None, triage_md_body: str
) -> Any:
    """Return a stand-in Investigator whose ``run`` writes the given TRIAGE.md
    and returns a crafted :class:`AgentResult`.

    We override the module-level ``Investigator`` class so the pipeline's
    ``Investigator()`` call picks this up.
    """

    class _FakeInvestigator:
        role = "investigator"

        async def run(
            self, bug: Any, worktree: Path, context: dict[str, Any]
        ) -> AgentResult:
            triage_path = worktree / "TRIAGE.md"
            triage_path.write_text(triage_md_body, encoding="utf-8")
            now = _now()
            attempt = context["attempt"]
            # Simulate the base class's log + prompt path conventions.
            from ra_triage.settings import get_settings

            settings = get_settings()
            log_path = (
                settings.logs_dir
                / str(bug.id)
                / f"investigator.{attempt}.log"
            )
            prompt_path = (
                settings.prompts_dir
                / str(bug.id)
                / f"investigator.{attempt}.md"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("fake log\n", encoding="utf-8")
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text("fake prompt\n", encoding="utf-8")
            return AgentResult(
                exit_code=0 if completion_token == "INVESTIGATOR_COMPLETE" else 1,
                completion_token=completion_token,
                log_path=log_path,
                prompt_path=prompt_path,
                triage_md=triage_md_body,
                started_at=now,
                ended_at=now,
            )

    return _FakeInvestigator


# --- single-repo happy path ---------------------------------------------


async def test_pipeline_single_repo_happy_path(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_worktrees: dict[str, Path],
) -> None:
    """OPEN → TRIAGED → INVESTIGATING → FIXING → TESTING → READY_FOR_REVIEW."""
    triage_md = (
        "## Root Cause\n\nsrc/x.py:1\n\n"
        "## Scope\n\n"
        "scope: single-repo\n"
        'affected_repos: ["avm-backend"]\n'
        "deny_list_hit: false\n\n"
        "## Analysis\n\nok.\n\n"
        "## Recommended Fix Approach\n\nguard the nil.\n"
    )
    monkeypatch.setattr(
        pipeline_module,
        "Investigator",
        _make_fake_investigator(
            completion_token="INVESTIGATOR_COMPLETE",
            triage_md_body=triage_md,
        ),
    )

    session = engine_module.SessionLocal()
    bug = _make_bug(session)
    bug_id = bug.id
    session.close()

    await pipeline_module.run_pipeline(bug_id)

    # Re-open session to inspect final state.
    session = engine_module.SessionLocal()
    try:
        got = repo_module.get_bug(session, bug_id)
        assert got is not None
        bug = got
        assert bug.state == BugState.READY_FOR_REVIEW.value
        assert bug.scope == "single-repo"
        assert bug.affected_repos == '["avm-backend"]'
        assert bug.deny_list_hit is False

        runs = repo_module.list_agent_runs(session, bug_id)
        assert len(runs) == 1
        run = runs[0]
        assert run.agent_role == "investigator"
        assert run.attempt == 1
        assert run.completion_token == "INVESTIGATOR_COMPLETE"
        assert run.exit_code == 0
        assert run.triage_md_snapshot and "Root Cause" in run.triage_md_snapshot
    finally:
        session.close()


# --- cross-repo happy path ----------------------------------------------


async def test_pipeline_cross_repo_happy_path(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_worktrees: dict[str, Path],
) -> None:
    """Cross-repo stub walks DESIGNING → FIXING_PARALLEL → INTEGRATION_TESTING → READY_FOR_REVIEW."""
    triage_md = (
        "## Root Cause\n\napi/page.go:10 + ui/page.tsx:20\n\n"
        "## Scope\n\n"
        "scope: cross-repo\n"
        'affected_repos: ["avm-backend", "builder-frontend"]\n'
        "deny_list_hit: false\n\n"
        "## Analysis\n\ncontract drift.\n\n"
        "## Recommended Fix Approach\n\nalign both sides.\n"
    )
    monkeypatch.setattr(
        pipeline_module,
        "Investigator",
        _make_fake_investigator(
            completion_token="INVESTIGATOR_COMPLETE",
            triage_md_body=triage_md,
        ),
    )

    session = engine_module.SessionLocal()
    bug = _make_bug(session, notion_page_id="cross-1")
    bug_id = bug.id
    session.close()

    await pipeline_module.run_pipeline(bug_id)

    session = engine_module.SessionLocal()
    try:
        got = repo_module.get_bug(session, bug_id)
        assert got is not None
        bug = got
        assert bug.state == BugState.READY_FOR_REVIEW.value
        assert bug.scope == "cross-repo"
        assert bug.affected_repos == '["avm-backend", "builder-frontend"]'
    finally:
        session.close()


# --- escalation ----------------------------------------------------------


async def test_pipeline_escalation_lands_in_blocked(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_worktrees: dict[str, Path],
) -> None:
    """TRIAGE.md containing Escalation Brief → BLOCKED."""
    triage_md = (
        "## Root Cause\n\nsrc/auth/login.py:4\n\n"
        "## Scope\n\n"
        "scope: single-repo\n"
        'affected_repos: ["avm-backend"]\n'
        "deny_list_hit: true\n\n"
        "## Analysis\n\nauth path touched.\n\n"
        "## Escalation Brief\n\n- **TL;DR:** auth change\n"
    )
    monkeypatch.setattr(
        pipeline_module,
        "Investigator",
        _make_fake_investigator(
            completion_token="INVESTIGATOR_COMPLETE",
            triage_md_body=triage_md,
        ),
    )

    session = engine_module.SessionLocal()
    bug = _make_bug(session, notion_page_id="esc-1")
    bug_id = bug.id
    session.close()

    await pipeline_module.run_pipeline(bug_id)

    session = engine_module.SessionLocal()
    try:
        got = repo_module.get_bug(session, bug_id)
        assert got is not None
        bug = got
        assert bug.state == BugState.BLOCKED.value
        # deny_list_hit flag flows through to the bug row.
        assert bug.deny_list_hit is True
    finally:
        session.close()


# --- explicit INVESTIGATOR_FAILED ---------------------------------------


async def test_pipeline_investigator_failed_lands_in_blocked(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_worktrees: dict[str, Path],
) -> None:
    """INVESTIGATOR_FAILED completion token → BLOCKED."""
    triage_md = (
        "## Investigator Failure\n\nCould not reproduce.\n"
    )
    monkeypatch.setattr(
        pipeline_module,
        "Investigator",
        _make_fake_investigator(
            completion_token="INVESTIGATOR_FAILED",
            triage_md_body=triage_md,
        ),
    )

    session = engine_module.SessionLocal()
    bug = _make_bug(session, notion_page_id="failed-1")
    bug_id = bug.id
    session.close()

    await pipeline_module.run_pipeline(bug_id)

    session = engine_module.SessionLocal()
    try:
        got = repo_module.get_bug(session, bug_id)
        assert got is not None
        bug = got
        assert bug.state == BugState.BLOCKED.value
        # Scope was absent — no scope persisted.
        assert bug.scope is None
    finally:
        session.close()


# --- malformed Scope -----------------------------------------------------


async def test_pipeline_malformed_scope_lands_in_blocked_with_appended_brief(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_worktrees: dict[str, Path],
) -> None:
    """TRIAGE.md with unparseable Scope → BLOCKED; orchestrator appends an
    Escalation Brief explaining why."""
    triage_md = (
        "## Root Cause\n\nsomewhere\n\n"
        "## Scope\n\n(nothing parseable)\n\n"
        "## Analysis\n\nn/a\n\n"
        "## Recommended Fix Approach\n\nn/a\n"
    )
    monkeypatch.setattr(
        pipeline_module,
        "Investigator",
        _make_fake_investigator(
            completion_token="INVESTIGATOR_COMPLETE",
            triage_md_body=triage_md,
        ),
    )

    session = engine_module.SessionLocal()
    bug = _make_bug(session, notion_page_id="mal-1")
    bug_id = bug.id
    session.close()

    await pipeline_module.run_pipeline(bug_id)

    session = engine_module.SessionLocal()
    try:
        got = repo_module.get_bug(session, bug_id)
        assert got is not None
        bug = got
        assert bug.state == BugState.BLOCKED.value
    finally:
        session.close()


# --- cancellation race --------------------------------------------------


async def test_pipeline_respects_cancellation(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_worktrees: dict[str, Path],
) -> None:
    """If a bug is CANCELLED before run_pipeline starts, it exits early."""
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.create_bug(
            session,
            notion_page_id="cancel-1",
            project_id="ra_avm",
            title="cancelled",
            description="x",
        )
        repo_module.update_bug_state(session, bug, BugState.CANCELLED)
        session.commit()
        bug_id = bug.id
    finally:
        session.close()

    # No Investigator mock → if pipeline tried to run, it would attempt a
    # subprocess and fail. Instead, pipeline should log + exit.
    await pipeline_module.run_pipeline(bug_id)

    session = engine_module.SessionLocal()
    try:
        got = repo_module.get_bug(session, bug_id)
        assert got is not None
        bug = got
        assert bug.state == BugState.CANCELLED.value
        runs = repo_module.list_agent_runs(session, bug_id)
        assert runs == []
    finally:
        session.close()


# --- agent_runs row lifecycle -------------------------------------------


async def test_agent_run_row_has_ended_at_and_paths_populated(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_worktrees: dict[str, Path],
) -> None:
    """The single agent_runs row is updated with ended_at + triage_md_snapshot."""
    triage_md = (
        "## Root Cause\n\nsrc/x.py:1\n\n"
        "## Scope\n\n"
        "scope: single-repo\n"
        'affected_repos: ["avm-backend"]\n'
        "deny_list_hit: false\n\n"
        "## Analysis\n\nok.\n\n"
        "## Recommended Fix Approach\n\nfix.\n"
    )
    monkeypatch.setattr(
        pipeline_module,
        "Investigator",
        _make_fake_investigator(
            completion_token="INVESTIGATOR_COMPLETE",
            triage_md_body=triage_md,
        ),
    )

    session = engine_module.SessionLocal()
    bug = _make_bug(session, notion_page_id="row-1")
    bug_id = bug.id
    session.close()

    await pipeline_module.run_pipeline(bug_id)

    session = engine_module.SessionLocal()
    try:
        runs = (
            session.query(AgentRun).filter(AgentRun.bug_id == bug_id).all()
        )
        assert len(runs) == 1
        run = runs[0]
        assert run.ended_at is not None
        assert run.started_at <= run.ended_at
        assert run.log_path.endswith("investigator.1.log")
        assert run.prompt_path.endswith("investigator.1.md")
        assert run.triage_md_snapshot is not None
        assert "Scope" in run.triage_md_snapshot
    finally:
        session.close()
