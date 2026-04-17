"""End-to-end tests for the Agent 8 review endpoints.

Covers:
  - POST /bugs/{bug_id}/confirm (202; 409 from wrong state).
  - POST /bugs/{bug_id}/reject — counts 1/2 → INVESTIGATING; count 3 → BLOCKED.
  - POST /bugs/{bug_id}/cancel — any non-terminal state → CANCELLED + cleanup.
  - POST /bugs/{bug_id}/retry — attempt increments; 409 eligibility rules.
  - POST /bugs/{bug_id}/take-over — → BLOCKED marker, no cleanup.

Strategy: build a real TestClient against the alembic-migrated DB from
conftest. Stub ``merge_bug`` and ``reinvestigate_with_feedback`` so the
endpoints schedule but don't actually spawn subprocesses. Stub
``workspaces.worktree.cleanup`` so we can assert it was called (or not).
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from ra_triage.api import bugs as bugs_api
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.db.models import Bug
from ra_triage.main import create_app
from ra_triage.orchestrator.states import BugState
from ra_triage.workspaces import worktree as wt_module

# --- helpers ---------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _walk_to(session: Session, bug: Bug, target: BugState) -> None:
    """Walk a bug from OPEN to ``target`` via a canonical happy-path sequence."""
    path: list[BugState] = [
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.FIXING,
        BugState.TESTING,
        BugState.READY_FOR_REVIEW,
    ]
    for nxt in path:
        repo_module.update_bug_state(session, bug, nxt)
        if nxt == target:
            session.commit()
            return
    session.commit()


def _make_bug(
    session: Session,
    *,
    notion_page_id: str,
    state: BugState = BugState.READY_FOR_REVIEW,
    affected: list[str] | None = None,
) -> int:
    bug = repo_module.create_bug(
        session,
        notion_page_id=notion_page_id,
        project_id="ra_avm",
        title="T",
        description="D",
    )
    bug.scope = "single-repo"
    bug.affected_repos = json.dumps(affected or ["avm-backend"])
    session.flush()
    _walk_to(session, bug, state)
    session.commit()
    return bug.id


@pytest.fixture()
def stub_merge(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[int]]:
    """Replace ``orchestrator.merge.merge_bug`` with a no-op; record calls."""
    calls: dict[str, list[int]] = {"merge_bug": []}

    async def _fake_merge(bug_id: int) -> None:
        calls["merge_bug"].append(bug_id)

    # Patch the symbol inside the api.bugs module (the endpoint imports it
    # by name at module load time).
    monkeypatch.setattr(bugs_api, "merge_bug", _fake_merge)
    return calls


@pytest.fixture()
def stub_reinvestigate(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[tuple[int, str]]]:
    """Replace ``reinvestigate_with_feedback`` with a recorder."""
    calls: dict[str, list[tuple[int, str]]] = {"reinvestigate": []}

    async def _fake(bug_id: int, rejection_feedback: str) -> None:
        calls["reinvestigate"].append((bug_id, rejection_feedback))

    monkeypatch.setattr(bugs_api, "reinvestigate_with_feedback", _fake)
    return calls


@pytest.fixture()
def stub_retry(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[int]]:
    calls: dict[str, list[int]] = {"retry": []}

    async def _fake(bug_id: int) -> None:
        calls["retry"].append(bug_id)

    monkeypatch.setattr(bugs_api, "retry_last_agent", _fake)
    return calls


@pytest.fixture()
def stub_cleanup(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[int]]:
    calls: dict[str, list[int]] = {"cleanup": []}

    async def _fake(bug_id: int, repos: list[Any]) -> None:
        calls["cleanup"].append(bug_id)

    monkeypatch.setattr(wt_module, "cleanup", _fake)
    return calls


# --- /confirm --------------------------------------------------------------


def test_confirm_from_rfr_returns_202_and_schedules_merge(
    alembic_db: Path, stub_merge: dict[str, list[int]]
) -> None:
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug_id = _make_bug(session, notion_page_id="conf-1")
    session.close()

    with TestClient(create_app()) as client:
        response = client.post(f"/bugs/{bug_id}/confirm")
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "merging_started"
        assert body["bug_id"] == bug_id

        # The endpoint schedules `merge_bug` on the app's event loop via
        # ``call_soon_threadsafe``. Give the loop a tick to pick it up.
        _wait_until(lambda: stub_merge["merge_bug"] == [bug_id])

    assert stub_merge["merge_bug"] == [bug_id]


def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.02) -> None:
    """Poll ``predicate`` until True or the deadline passes.

    Used for asserting on side effects of scheduled coroutines — the
    endpoint returns before the loop runs the scheduled task, so a tiny
    wait keeps the tests deterministic without a full async fixture.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)


def test_confirm_from_wrong_state_returns_409(
    alembic_db: Path, stub_merge: dict[str, list[int]]
) -> None:
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug_id = _make_bug(session, notion_page_id="conf-bad", state=BugState.INVESTIGATING)
    session.close()

    client = TestClient(create_app())
    response = client.post(f"/bugs/{bug_id}/confirm")
    assert response.status_code == 409
    # merge_bug was NOT scheduled.
    assert stub_merge["merge_bug"] == []


def test_confirm_unknown_bug_returns_404(alembic_db: Path) -> None:
    _ = alembic_db
    client = TestClient(create_app())
    response = client.post("/bugs/999999/confirm")
    assert response.status_code == 404


# --- /reject ---------------------------------------------------------------


def test_reject_count_one_transitions_to_investigating(
    alembic_db: Path, stub_reinvestigate: dict[str, list[tuple[int, str]]]
) -> None:
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug_id = _make_bug(session, notion_page_id="rej-1")
    session.close()

    with TestClient(create_app()) as client:
        response = client.post(
            f"/bugs/{bug_id}/reject", json={"reason": "missed edge case"}
        )
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "reinvestigating"
        assert body["rejection_count"] == 1
        assert body["new_state"] == BugState.INVESTIGATING.value

        _wait_until(
            lambda: stub_reinvestigate["reinvestigate"]
            == [(bug_id, "missed edge case")]
        )

    # Scheduled coroutine got the reason.
    assert stub_reinvestigate["reinvestigate"] == [(bug_id, "missed edge case")]

    session = engine_module.SessionLocal()
    try:
        bug = repo_module.get_bug(session, bug_id)
        assert bug is not None
        assert bug.state == BugState.INVESTIGATING.value
        assert bug.rejection_count == 1
    finally:
        session.close()


def test_reject_third_time_lands_in_blocked(
    alembic_db: Path,
    triage_home: Path,
    stub_reinvestigate: dict[str, list[tuple[int, str]]],
) -> None:
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug_id = _make_bug(session, notion_page_id="rej-3")
    session.close()

    # Seed a worktree so the ## Rejection Limit Reached section can land
    # in a sensible TRIAGE.md path.
    worktree = triage_home / "worktrees" / str(bug_id) / "avm-backend"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "TRIAGE.md").write_text(
        "## Root Cause\n\nX\n", encoding="utf-8"
    )

    client = TestClient(create_app())
    # Reject #1 → INVESTIGATING.
    r1 = client.post(f"/bugs/{bug_id}/reject", json={"reason": "reason-1"})
    assert r1.status_code == 202
    # Walk back to RFR for reject #2.
    session = engine_module.SessionLocal()
    bug = repo_module.get_bug(session, bug_id)
    assert bug is not None
    for nxt in (BugState.FIXING, BugState.TESTING, BugState.READY_FOR_REVIEW):
        repo_module.update_bug_state(session, bug, nxt)
    session.commit()
    session.close()
    # Reject #2.
    r2 = client.post(f"/bugs/{bug_id}/reject", json={"reason": "reason-2"})
    assert r2.status_code == 202
    assert r2.json()["rejection_count"] == 2
    # Walk back to RFR for reject #3.
    session = engine_module.SessionLocal()
    bug = repo_module.get_bug(session, bug_id)
    assert bug is not None
    for nxt in (BugState.FIXING, BugState.TESTING, BugState.READY_FOR_REVIEW):
        repo_module.update_bug_state(session, bug, nxt)
    session.commit()
    session.close()
    # Reject #3 → BLOCKED.
    r3 = client.post(f"/bugs/{bug_id}/reject", json={"reason": "reason-3"})
    assert r3.status_code == 202
    body = r3.json()
    assert body["status"] == "blocked"
    assert body["rejection_count"] == 3
    assert body["new_state"] == BugState.BLOCKED.value

    session = engine_module.SessionLocal()
    try:
        bug = repo_module.get_bug(session, bug_id)
        assert bug is not None
        assert bug.state == BugState.BLOCKED.value
    finally:
        session.close()

    # TRIAGE.md got the Rejection Limit Reached section.
    from ra_triage.triage_md import parser as triage_parser

    sections = triage_parser.parse(worktree / "TRIAGE.md")
    assert "Rejection Limit Reached" in sections
    body_text = sections["Rejection Limit Reached"]
    assert "reason-1" in body_text
    assert "reason-2" in body_text
    assert "reason-3" in body_text


def test_reject_from_wrong_state_returns_409(alembic_db: Path) -> None:
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug_id = _make_bug(
        session, notion_page_id="rej-bad", state=BugState.INVESTIGATING
    )
    session.close()

    client = TestClient(create_app())
    response = client.post(f"/bugs/{bug_id}/reject", json={"reason": "no"})
    assert response.status_code == 409


def test_reject_requires_non_empty_reason(alembic_db: Path) -> None:
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug_id = _make_bug(session, notion_page_id="rej-empty")
    session.close()

    client = TestClient(create_app())
    response = client.post(f"/bugs/{bug_id}/reject", json={"reason": ""})
    # Pydantic min_length=1 → 422.
    assert response.status_code == 422


# --- /cancel ---------------------------------------------------------------


def test_cancel_from_any_non_terminal_transitions_and_cleans_up(
    alembic_db: Path, stub_cleanup: dict[str, list[int]]
) -> None:
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug_id = _make_bug(
        session, notion_page_id="cancel-1", state=BugState.INVESTIGATING
    )
    session.close()

    with TestClient(create_app()) as client:
        response = client.post(f"/bugs/{bug_id}/cancel")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "cancelled"

        session = engine_module.SessionLocal()
        try:
            bug = repo_module.get_bug(session, bug_id)
            assert bug is not None
            assert bug.state == BugState.CANCELLED.value
        finally:
            session.close()

        _wait_until(lambda: stub_cleanup["cleanup"] == [bug_id])

    # cleanup was scheduled — when TestClient's lifespan closes the loop,
    # scheduled tasks get a chance to run.
    assert stub_cleanup["cleanup"] == [bug_id]


def test_cancel_from_terminal_returns_409(
    alembic_db: Path, stub_cleanup: dict[str, list[int]]
) -> None:
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug_id = _make_bug(
        session, notion_page_id="cancel-term", state=BugState.READY_FOR_REVIEW
    )
    bug = repo_module.get_bug(session, bug_id)
    assert bug is not None
    repo_module.update_bug_state(session, bug, BugState.CANCELLED)
    session.commit()
    session.close()
    stub_cleanup["cleanup"].clear()

    client = TestClient(create_app())
    response = client.post(f"/bugs/{bug_id}/cancel")
    assert response.status_code == 409


# --- /retry ----------------------------------------------------------------


def test_retry_on_fixer_failed_schedules_retry_and_returns_attempt_plus_one(
    alembic_db: Path, stub_retry: dict[str, list[int]]
) -> None:
    _ = alembic_db
    session = engine_module.SessionLocal()
    # Bug in BLOCKED with the latest agent run being FIXER_FAILED.
    bug = repo_module.create_bug(
        session,
        notion_page_id="retry-fix-fail",
        project_id="ra_avm",
        title="t",
        description="d",
    )
    # Walk: OPEN → TRIAGED → INVESTIGATING → FIXING → BLOCKED (failure).
    for nxt in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.FIXING,
        BugState.BLOCKED,
    ):
        repo_module.update_bug_state(session, bug, nxt)
    # Insert an agent_runs row: fixer attempt 1, FIXER_FAILED.
    repo_module.record_agent_run(
        session,
        bug_id=bug.id,
        agent_role="fixer",
        attempt=1,
        started_at=_now(),
        ended_at=_now(),
        exit_code=1,
        completion_token="FIXER_FAILED",
        log_path="/tmp/fix.log",
        prompt_path="/tmp/fix.md",
        worktree_repo="avm-backend",
    )
    session.commit()
    bug_id = bug.id
    session.close()

    with TestClient(create_app()) as client:
        response = client.post(f"/bugs/{bug_id}/retry")
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "retrying"
        assert body["agent_role"] == "fixer"
        assert body["attempt"] == 2
        _wait_until(lambda: stub_retry["retry"] == [bug_id])
    assert stub_retry["retry"] == [bug_id]


def test_retry_with_no_agent_runs_returns_409(alembic_db: Path) -> None:
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug_id = _make_bug(session, notion_page_id="retry-none")
    session.close()

    client = TestClient(create_app())
    response = client.post(f"/bugs/{bug_id}/retry")
    assert response.status_code == 409


def test_retry_from_blocked_without_failed_token_returns_409(
    alembic_db: Path,
) -> None:
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = repo_module.create_bug(
        session,
        notion_page_id="retry-blocked-notok",
        project_id="ra_avm",
        title="t",
        description="d",
    )
    for nxt in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.BLOCKED,
    ):
        repo_module.update_bug_state(session, bug, nxt)
    # Latest run completed normally — not a FAILED variant.
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

    client = TestClient(create_app())
    response = client.post(f"/bugs/{bug_id}/retry")
    assert response.status_code == 409


def test_retry_on_terminal_bug_returns_409(alembic_db: Path) -> None:
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

    client = TestClient(create_app())
    response = client.post(f"/bugs/{bug_id}/retry")
    assert response.status_code == 409


# --- /take-over ------------------------------------------------------------


def test_take_over_from_non_terminal_transitions_and_no_cleanup(
    alembic_db: Path,
    triage_home: Path,
    stub_cleanup: dict[str, list[int]],
) -> None:
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug_id = _make_bug(
        session, notion_page_id="take-1", state=BugState.INVESTIGATING
    )
    session.close()

    # Seed a worktree so the Taken Over marker has a file to land in.
    worktree = triage_home / "worktrees" / str(bug_id) / "avm-backend"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "TRIAGE.md").write_text("## Root Cause\n\nX\n", encoding="utf-8")

    client = TestClient(create_app())
    response = client.post(f"/bugs/{bug_id}/take-over")
    assert response.status_code == 200

    session = engine_module.SessionLocal()
    try:
        bug = repo_module.get_bug(session, bug_id)
        assert bug is not None
        assert bug.state == BugState.BLOCKED.value
    finally:
        session.close()

    # Cleanup was NOT scheduled — take-over retains the worktree.
    assert stub_cleanup["cleanup"] == []

    # TRIAGE.md has the marker.
    from ra_triage.triage_md import parser as triage_parser

    sections = triage_parser.parse(worktree / "TRIAGE.md")
    assert "Taken Over Manually" in sections


def test_take_over_from_blocked_is_idempotent(
    alembic_db: Path, triage_home: Path
) -> None:
    """Take-over from BLOCKED leaves state BLOCKED and still appends the marker."""
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = repo_module.create_bug(
        session,
        notion_page_id="take-blocked",
        project_id="ra_avm",
        title="t",
        description="d",
    )
    for nxt in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.BLOCKED,
    ):
        repo_module.update_bug_state(session, bug, nxt)
    session.commit()
    bug_id = bug.id
    session.close()

    worktree = triage_home / "worktrees" / str(bug_id) / "avm-backend"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "TRIAGE.md").write_text("# existing\n", encoding="utf-8")

    client = TestClient(create_app())
    response = client.post(f"/bugs/{bug_id}/take-over")
    assert response.status_code == 200

    session = engine_module.SessionLocal()
    try:
        reloaded = repo_module.get_bug(session, bug_id)
        assert reloaded is not None
        assert reloaded.state == BugState.BLOCKED.value
    finally:
        session.close()

    from ra_triage.triage_md import parser as triage_parser

    sections = triage_parser.parse(worktree / "TRIAGE.md")
    assert "Taken Over Manually" in sections


def test_take_over_on_terminal_returns_409(alembic_db: Path) -> None:
    _ = alembic_db
    session = engine_module.SessionLocal()
    bug = repo_module.create_bug(
        session,
        notion_page_id="take-term",
        project_id="ra_avm",
        title="t",
        description="d",
    )
    repo_module.update_bug_state(session, bug, BugState.CANCELLED)
    session.commit()
    bug_id = bug.id
    session.close()

    client = TestClient(create_app())
    response = client.post(f"/bugs/{bug_id}/take-over")
    assert response.status_code == 409
