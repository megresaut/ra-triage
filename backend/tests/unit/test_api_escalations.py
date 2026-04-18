"""Tests for :mod:`ra_triage.api.escalations` endpoints.

These tests drive the approve / pick-alternative / take-over flow
through FastAPI's :class:`TestClient` so we exercise dispatch, Pydantic
validation, and the persisted-escalation resolution path together.

The Fixer is NOT spawned for real — we stub
``run_pipeline_with_escalation_context`` to a no-op coroutine that just
renders the Fixer's prompt to disk so the test can grep for
``escalation_context`` in the persisted prompt file (acceptance
criterion #4).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.db.models import Escalation
from ra_triage.main import create_app
from ra_triage.orchestrator.states import BugState


@pytest.fixture()
def client(alembic_db: Path) -> TestClient:  # noqa: ARG001 — fixture activation
    """A FastAPI ``TestClient`` bound to the migrated test DB."""
    app = create_app()
    return TestClient(app)


def _seed_bug_with_escalation(
    *,
    scope: str = "single-repo",
    options: list[dict[str, Any]] | None = None,
    brief: str | None = None,
) -> tuple[int, int]:
    """Create a BLOCKED bug with an unresolved escalation. Returns (bug_id, escalation_id)."""
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.create_bug(
            session,
            notion_page_id="esc-api-1",
            project_id="ra_avm",
            title="Escalation API bug",
            description="Needs human.",
        )
        session.flush()
        # Walk to BLOCKED via legal transitions.
        for next_state in (
            BugState.TRIAGED,
            BugState.INVESTIGATING,
            BugState.BLOCKED,
        ):
            repo_module.update_bug_state(session, bug, next_state)
        bug.scope = scope
        session.flush()

        if options is None:
            options = [
                {
                    "id": "approve_recommended",
                    "label": "Approve Recommended",
                    "kind": "approve",
                },
                {
                    "id": "alternative_1",
                    "label": "Pick Alternative 1",
                    "kind": "alternative",
                },
                {
                    "id": "alternative_2",
                    "label": "Pick Alternative 2",
                    "kind": "alternative",
                },
                {
                    "id": "take_over",
                    "label": "Take Over Manually",
                    "kind": "take_over",
                },
            ]
        if brief is None:
            brief = (
                "- **TL;DR:** Auth change required; deny-list hit.\n"
                "- **Why blocked:** deny_list path auth/** matched.\n"
                "- **Proposed approach:** Additive migration.\n"
                "- **Ranked alternatives:**\n"
                "  - **Alternative 1:** Session rehydrate via headers only.\n"
                "  - **Alternative 2:** Deprecate legacy tokens entirely.\n"
                "- **Files that would be touched:**\n"
                "  - backend/auth/token.py\n"
            )
        row = repo_module.record_escalation(
            session,
            bug_id=bug.id,
            triggered_rule="deny_list",
            brief_markdown=brief,
            options=json.dumps(options),
        )
        session.commit()
        return int(bug.id), int(row.id)
    finally:
        session.close()


# --- GET /bugs/:id/escalation --------------------------------------------


def test_get_escalation_returns_parsed_brief_and_options(
    client: TestClient,
) -> None:
    bug_id, _esc_id = _seed_bug_with_escalation()
    response = client.get(f"/bugs/{bug_id}/escalation")
    assert response.status_code == 200
    body = response.json()
    assert body["bug_id"] == bug_id
    parsed = body["parsed"]
    assert parsed["tldr"].startswith("Auth change required")
    assert parsed["why_blocked"] is not None
    assert len(parsed["ranked_alternatives"]) == 2
    assert parsed["files_that_would_be_touched"] == [
        "backend/auth/token.py",
    ]
    option_ids = [o["id"] for o in parsed["options"]]
    assert option_ids == [
        "approve_recommended",
        "alternative_1",
        "alternative_2",
        "take_over",
    ]


def test_get_escalation_404_when_bug_missing(client: TestClient) -> None:
    response = client.get("/bugs/99999/escalation")
    assert response.status_code == 404


def test_get_escalation_404_when_no_unresolved_row(client: TestClient) -> None:
    # Seed a bug with a resolved escalation.
    bug_id, esc_id = _seed_bug_with_escalation()
    session = engine_module.SessionLocal()
    try:
        repo_module.update_escalation_resolution(
            session,
            escalation_id=esc_id,
            resolution="taken_over",
            resolved_option="take_over",
        )
        session.commit()
    finally:
        session.close()
    response = client.get(f"/bugs/{bug_id}/escalation")
    assert response.status_code == 404


# --- POST /bugs/:id/escalation/approve -----------------------------------


def test_approve_transitions_to_fixing_and_resolves_escalation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    bug_id, esc_id = _seed_bug_with_escalation(scope="single-repo")

    captured: dict[str, Any] = {}

    # Replace the scheduler with a sync spy so we observe what the handler
    # would have dispatched without needing a live event loop under the
    # TestClient threadpool.
    def _spy(bug_id_arg: int, escalation_context: str) -> None:
        captured["bug_id"] = bug_id_arg
        captured["escalation_context"] = escalation_context

    from ra_triage.api import escalations as escalations_mod

    monkeypatch.setattr(
        escalations_mod, "_schedule_pipeline_with_context", _spy
    )

    response = client.post(
        f"/bugs/{bug_id}/escalation/approve",
        json={"option_id": "approve_recommended"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "approved"
    assert body["new_state"] == BugState.FIXING.value
    assert body["resolved_option"] == "approve_recommended"

    # DB: state moved + row resolved.
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.get_bug(session, bug_id)
        assert bug is not None
        assert bug.state == BugState.FIXING.value
        row = session.get(Escalation, esc_id)
        assert row is not None
        assert row.resolution == "approved"
        assert row.resolved_option == "approve_recommended"
        assert row.resolved_at is not None
    finally:
        session.close()

    # Pipeline was scheduled with a context that includes the TL;DR text.
    assert captured.get("bug_id") == bug_id
    assert "Auth change required" in captured["escalation_context"]
    assert "APPROVED" in captured["escalation_context"]


def test_approve_transitions_to_fixing_parallel_for_cross_repo(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    bug_id, _esc_id = _seed_bug_with_escalation(scope="cross-repo")

    def _spy(bug_id_arg: int, escalation_context: str) -> None:
        return None

    from ra_triage.api import escalations as escalations_mod

    monkeypatch.setattr(
        escalations_mod, "_schedule_pipeline_with_context", _spy
    )

    response = client.post(
        f"/bugs/{bug_id}/escalation/approve",
        json={"option_id": "approve_recommended"},
    )
    assert response.status_code == 202
    assert response.json()["new_state"] == BugState.FIXING_PARALLEL.value


def test_approve_rejects_wrong_option_id(client: TestClient) -> None:
    bug_id, _esc_id = _seed_bug_with_escalation()
    response = client.post(
        f"/bugs/{bug_id}/escalation/approve",
        json={"option_id": "alternative_1"},
    )
    assert response.status_code == 400


def test_approve_404_when_no_active_escalation(client: TestClient) -> None:
    # Create a BLOCKED bug without an escalation row.
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.create_bug(
            session,
            notion_page_id="no-esc-1",
            project_id="ra_avm",
            title="No escalation",
            description="x",
        )
        for next_state in (
            BugState.TRIAGED,
            BugState.INVESTIGATING,
            BugState.BLOCKED,
        ):
            repo_module.update_bug_state(session, bug, next_state)
        session.commit()
        bug_id = int(bug.id)
    finally:
        session.close()

    response = client.post(
        f"/bugs/{bug_id}/escalation/approve",
        json={"option_id": "approve_recommended"},
    )
    assert response.status_code == 404


def test_approve_409_when_bug_not_blocked(client: TestClient) -> None:
    # Seed then manually move off BLOCKED so the precondition fails.
    bug_id, _esc_id = _seed_bug_with_escalation()
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.get_bug(session, bug_id)
        assert bug is not None
        repo_module.update_bug_state(session, bug, BugState.FIXING)
        session.commit()
    finally:
        session.close()

    response = client.post(
        f"/bugs/{bug_id}/escalation/approve",
        json={"option_id": "approve_recommended"},
    )
    assert response.status_code == 409


# --- POST /bugs/:id/escalation/pick-alternative --------------------------


def test_pick_alternative_threads_notes_and_body_into_context(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    bug_id, esc_id = _seed_bug_with_escalation()

    captured: dict[str, Any] = {}

    def _spy(bug_id_arg: int, escalation_context: str) -> None:
        captured["bug_id"] = bug_id_arg
        captured["escalation_context"] = escalation_context

    from ra_triage.api import escalations as escalations_mod

    monkeypatch.setattr(
        escalations_mod, "_schedule_pipeline_with_context", _spy
    )

    response = client.post(
        f"/bugs/{bug_id}/escalation/pick-alternative",
        json={
            "option_id": "alternative_2",
            "notes": "Prefer the deprecation path; comms handled separately.",
        },
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "picked_alternative"
    assert body["resolved_option"] == "alternative_2"

    # Context includes alternative 2 body + the notes.
    ctx = captured["escalation_context"]
    assert "Deprecate legacy tokens" in ctx
    assert "Prefer the deprecation path" in ctx
    assert "alternative_2" in ctx

    # DB resolved with the picked option id.
    session = engine_module.SessionLocal()
    try:
        row = session.get(Escalation, esc_id)
        assert row is not None
        assert row.resolution == "picked_alt"
        assert row.resolved_option == "alternative_2"
    finally:
        session.close()


def test_pick_alternative_rejects_unknown_option_id(client: TestClient) -> None:
    bug_id, _esc_id = _seed_bug_with_escalation()
    response = client.post(
        f"/bugs/{bug_id}/escalation/pick-alternative",
        json={"option_id": "alternative_99"},
    )
    assert response.status_code == 400


def test_pick_alternative_rejects_non_alternative_prefix(client: TestClient) -> None:
    bug_id, _esc_id = _seed_bug_with_escalation()
    response = client.post(
        f"/bugs/{bug_id}/escalation/pick-alternative",
        json={"option_id": "approve_recommended"},
    )
    assert response.status_code == 400


# --- POST /bugs/:id/escalation/take-over ---------------------------------


def test_take_over_resolves_row_and_leaves_bug_in_blocked(
    client: TestClient,
) -> None:
    bug_id, esc_id = _seed_bug_with_escalation()
    response = client.post(f"/bugs/{bug_id}/escalation/take-over")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "taken_over"
    assert body["resolved_option"] == "take_over"

    session = engine_module.SessionLocal()
    try:
        bug = repo_module.get_bug(session, bug_id)
        assert bug is not None
        assert bug.state == BugState.BLOCKED.value  # unchanged
        row = session.get(Escalation, esc_id)
        assert row is not None
        assert row.resolution == "taken_over"
    finally:
        session.close()


def test_take_over_404_when_no_active_escalation(client: TestClient) -> None:
    bug_id, esc_id = _seed_bug_with_escalation()
    # Resolve once, then try again.
    session = engine_module.SessionLocal()
    try:
        repo_module.update_escalation_resolution(
            session,
            escalation_id=esc_id,
            resolution="taken_over",
            resolved_option="take_over",
        )
        session.commit()
    finally:
        session.close()

    response = client.post(f"/bugs/{bug_id}/escalation/take-over")
    assert response.status_code == 404


# --- Fixer prompt rendering (acceptance criterion #4) --------------------


async def test_approve_renders_escalation_context_into_fixer_prompt(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The approve flow should thread ``escalation_context`` into the Fixer's
    rendered prompt file at ``~/ra-triage/prompts/<bug>/fixer.<next>.md``.

    We exercise ``run_pipeline_with_escalation_context`` directly so we
    can stub the Fixer subprocess with an inline fake and then grep the
    persisted Jinja render.
    """
    from ra_triage.agents.base import AgentResult
    from ra_triage.orchestrator import pipeline as pipeline_module

    # Stub worktrees: return a synthetic worktree dir inside tmp triage_home.
    from ra_triage.workspaces import worktree as wt_module

    bug_id_holder: dict[str, int] = {}

    async def _fake_create(bug_id: int, repos: list[Any]) -> dict[str, Path]:
        mapping: dict[str, Path] = {}
        for r in repos:
            dest = triage_home / "worktrees" / str(bug_id) / r.name
            dest.mkdir(parents=True, exist_ok=True)
            mapping[r.name] = dest
        return mapping

    monkeypatch.setattr(wt_module, "create_worktrees_for_bug", _fake_create)

    # Stub the Fixer class so it records context and persists a prompt
    # through the real base-class rendering pipeline.
    from ra_triage.agents.base import _jinja_env
    from ra_triage.settings import get_settings

    class _FakeFixer:
        role = "fixer"

        async def run(
            self, bug: Any, worktree: Path, context: dict[str, Any]
        ) -> AgentResult:
            # Render the real Fixer template with the provided context,
            # then persist under the standard path.
            template = _jinja_env.get_template("fixer.md.j2")
            rendered = template.render(**context)
            settings = get_settings()
            attempt = int(context["attempt"])
            prompt_path = (
                settings.prompts_dir
                / str(bug.id)
                / f"fixer.{attempt}.md"
            )
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(rendered, encoding="utf-8")
            log_path = (
                settings.logs_dir / str(bug.id) / f"fixer.{attempt}.log"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("fake", encoding="utf-8")
            from datetime import UTC, datetime

            now = datetime.now(UTC).replace(tzinfo=None)
            bug_id_holder["attempt"] = attempt
            return AgentResult(
                exit_code=0,
                completion_token="FIXER_COMPLETE",
                log_path=log_path,
                prompt_path=prompt_path,
                triage_md="",
                started_at=now,
                ended_at=now,
            )

    class _FakeTester:
        role = "tester"

        async def run(
            self, bug: Any, worktree: Path, context: dict[str, Any]
        ) -> AgentResult:
            from datetime import UTC, datetime

            settings = get_settings()
            attempt = int(context["attempt"])
            log_path = (
                settings.logs_dir / str(bug.id) / f"tester.{attempt}.log"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("fake", encoding="utf-8")
            prompt_path = (
                settings.prompts_dir
                / str(bug.id)
                / f"tester.{attempt}.md"
            )
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text("fake", encoding="utf-8")
            now = datetime.now(UTC).replace(tzinfo=None)
            return AgentResult(
                exit_code=0,
                completion_token="TESTER_COMPLETE",
                log_path=log_path,
                prompt_path=prompt_path,
                triage_md="",
                started_at=now,
                ended_at=now,
            )

    monkeypatch.setattr(pipeline_module, "Fixer", _FakeFixer)
    monkeypatch.setattr(pipeline_module, "Tester", _FakeTester)

    # Stub safety check so the test worktree doesn't need to be a real git repo.
    from ra_triage.orchestrator import tester_safety

    async def _fake_check(
        worktree: Path, *, baseline: dict[str, str] | None = None
    ) -> tester_safety.SafetyCheckResult:
        return tester_safety.SafetyCheckResult(
            changed_paths=[],
            test_paths=[],
            reverted_paths=[],
            revert_errors=[],
            had_violation=False,
        )

    async def _fake_snapshot(worktree: Path) -> dict[str, str]:
        return {}

    monkeypatch.setattr(
        tester_safety, "check_and_revert_prod_changes", _fake_check
    )
    monkeypatch.setattr(
        tester_safety, "snapshot_working_tree", _fake_snapshot
    )

    # Seed the bug in FIXING (post-approve state) and write TRIAGE.md
    # with a single-repo scope that the pipeline will use to pick the repo.
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.create_bug(
            session,
            notion_page_id="approve-ctx-1",
            project_id="ra_avm",
            title="Approve context bug",
            description="x",
        )
        session.flush()
        for next_state in (
            BugState.TRIAGED,
            BugState.INVESTIGATING,
            BugState.BLOCKED,
            BugState.FIXING,
        ):
            repo_module.update_bug_state(session, bug, next_state)
        bug.scope = "single-repo"
        bug.affected_repos = '["avm-backend"]'
        session.commit()
        bug_id = int(bug.id)
    finally:
        session.close()

    # Pre-create the worktree + TRIAGE.md with scope.
    worktrees = await _fake_create(
        bug_id, list(pipeline_module.get_project("ra_avm").repos)
    )
    triage_md_path = (
        pipeline_module._triage_md_worktree(
            pipeline_module.get_project("ra_avm"), worktrees
        )
        / "TRIAGE.md"
    )
    triage_md_path.write_text(
        "## Root Cause\n\nsrc/auth/token.py\n\n"
        "## Scope\n\n"
        "scope: single-repo\n"
        'affected_repos: ["avm-backend"]\n'
        "deny_list_hit: true\n\n"
        "## Analysis\n\nn/a\n\n"
        "## Escalation Brief\n\n- **TL;DR:** auth change\n",
        encoding="utf-8",
    )

    # Run the escalation-context pipeline directly.
    await pipeline_module.run_pipeline_with_escalation_context(
        bug_id,
        "The reviewer APPROVED the recommended approach. Focus on src/auth/token.py.",
    )

    # Find the rendered Fixer prompt and confirm the escalation text is present.

    settings = get_settings()
    attempt = bug_id_holder.get("attempt", 1)
    prompt_path = (
        settings.prompts_dir / str(bug_id) / f"fixer.{attempt}.md"
    )
    assert prompt_path.exists()
    contents = prompt_path.read_text(encoding="utf-8")
    assert "HUMAN-APPROVED ESCALATION CONTEXT" in contents
    assert "reviewer APPROVED" in contents
