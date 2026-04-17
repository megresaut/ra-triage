"""Repository helper unit tests.

Uses the alembic-migrated SQLite fixture from conftest so we exercise the
real schema (and catch ORM-vs-migration drift).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from ra_triage.db import repo
from ra_triage.db.models import AgentRun, TestInstance
from ra_triage.orchestrator.states import BugState, IllegalBugStateTransition


def _make_bug(session: Session, *, notion_page_id: str = "notion-1", title: str = "t") -> int:
    bug = repo.create_bug(
        session,
        notion_page_id=notion_page_id,
        project_id="ra_avm",
        title=title,
        description="d",
    )
    session.commit()
    return bug.id


def test_create_bug_starts_in_open_state(db_session: Session) -> None:
    bug = repo.create_bug(
        db_session,
        notion_page_id="n-1",
        project_id="ra_avm",
        title="t",
        description="d",
    )
    db_session.commit()
    assert bug.state == BugState.OPEN.value
    assert bug.rejection_count == 0
    assert bug.deny_list_hit is False
    assert bug.terminated_at is None


def test_create_bug_is_idempotent_on_notion_page_id(db_session: Session) -> None:
    first = repo.create_bug(
        db_session,
        notion_page_id="dup-1",
        project_id="ra_avm",
        title="first",
        description="first-d",
    )
    db_session.commit()
    second = repo.create_bug(
        db_session,
        notion_page_id="dup-1",
        project_id="ra_avm",
        title="second (ignored)",
        description="second-d (ignored)",
    )
    db_session.commit()
    assert first.id == second.id
    assert second.title == "first"
    assert second.description == "first-d"


def test_list_bugs_excludes_terminal_by_default(db_session: Session) -> None:
    id_a = _make_bug(db_session, notion_page_id="a")
    id_b = _make_bug(db_session, notion_page_id="b")
    # Drive id_a all the way to FIXED.
    bug_a = repo.get_bug(db_session, id_a)
    assert bug_a is not None
    repo.update_bug_state(db_session, bug_a, BugState.TRIAGED)
    repo.update_bug_state(db_session, bug_a, BugState.INVESTIGATING)
    repo.update_bug_state(db_session, bug_a, BugState.FIXING)
    repo.update_bug_state(db_session, bug_a, BugState.TESTING)
    repo.update_bug_state(db_session, bug_a, BugState.READY_FOR_REVIEW)
    repo.update_bug_state(db_session, bug_a, BugState.MERGING)
    repo.update_bug_state(db_session, bug_a, BugState.FIXED)
    db_session.commit()

    non_terminal = repo.list_bugs(db_session)
    ids = {b.id for b in non_terminal}
    assert id_b in ids
    assert id_a not in ids


def test_list_bugs_include_terminal_returns_everything(db_session: Session) -> None:
    id_a = _make_bug(db_session, notion_page_id="a")
    _make_bug(db_session, notion_page_id="b")
    bug_a = repo.get_bug(db_session, id_a)
    assert bug_a is not None
    # Cancel id_a.
    repo.update_bug_state(db_session, bug_a, BugState.CANCELLED)
    db_session.commit()
    all_bugs = repo.list_bugs(db_session, include_terminal=True)
    assert len(all_bugs) == 2


def test_list_bugs_filters_by_project(db_session: Session) -> None:
    repo.create_bug(
        db_session,
        notion_page_id="p1",
        project_id="ra_avm",
        title="t",
        description="d",
    )
    repo.create_bug(
        db_session,
        notion_page_id="p2",
        project_id="other",
        title="t",
        description="d",
    )
    db_session.commit()
    avm = repo.list_bugs(db_session, project_id="ra_avm")
    assert len(avm) == 1
    assert avm[0].project_id == "ra_avm"


def test_list_bugs_filters_by_state(db_session: Session) -> None:
    id_a = _make_bug(db_session, notion_page_id="a")
    _make_bug(db_session, notion_page_id="b")
    bug_a = repo.get_bug(db_session, id_a)
    assert bug_a is not None
    repo.update_bug_state(db_session, bug_a, BugState.TRIAGED)
    db_session.commit()

    triaged = repo.list_bugs(db_session, state=BugState.TRIAGED)
    assert [b.id for b in triaged] == [id_a]

    open_only = repo.list_bugs(db_session, state=BugState.OPEN)
    assert len(open_only) == 1


def test_update_bug_state_rejects_illegal_transition(db_session: Session) -> None:
    bug_id = _make_bug(db_session, notion_page_id="illegal")
    bug = repo.get_bug(db_session, bug_id)
    assert bug is not None
    with pytest.raises(IllegalBugStateTransition):
        repo.update_bug_state(db_session, bug, BugState.MERGING)


def test_update_bug_state_stamps_terminated_at_on_terminal(db_session: Session) -> None:
    bug_id = _make_bug(db_session, notion_page_id="term")
    bug = repo.get_bug(db_session, bug_id)
    assert bug is not None
    repo.update_bug_state(db_session, bug, BugState.CANCELLED)
    db_session.commit()
    assert bug.terminated_at is not None


def test_record_rejection_increments_count(db_session: Session) -> None:
    bug_id = _make_bug(db_session, notion_page_id="rej")
    assert repo.get_bug(db_session, bug_id) is not None
    repo.record_rejection(db_session, bug_id=bug_id, reason="no good")
    repo.record_rejection(db_session, bug_id=bug_id, reason="still no")
    db_session.commit()
    bug = repo.get_bug(db_session, bug_id)
    assert bug is not None
    assert bug.rejection_count == 2
    assert len(bug.rejections) == 2


def test_record_agent_run_and_list(db_session: Session) -> None:
    bug_id = _make_bug(db_session, notion_page_id="ar")
    now = datetime.now(UTC).replace(tzinfo=None)
    repo.record_agent_run(
        db_session,
        bug_id=bug_id,
        agent_role="investigator",
        attempt=1,
        started_at=now,
        log_path="/tmp/log.txt",
        prompt_path="/tmp/prompt.md",
    )
    db_session.commit()
    runs = repo.list_agent_runs(db_session, bug_id)
    assert len(runs) == 1
    assert runs[0].agent_role == "investigator"
    assert runs[0].attempt == 1


def test_record_pr_defaults_status_open(db_session: Session) -> None:
    bug_id = _make_bug(db_session, notion_page_id="pr")
    pr = repo.record_pr(
        db_session,
        bug_id=bug_id,
        repo_name="avm-backend",
        branch="triage/1",
    )
    db_session.commit()
    assert pr.status == "open"
    assert pr.pr_url is None


def test_get_current_test_instance_returns_running(db_session: Session) -> None:
    bug_id = _make_bug(db_session, notion_page_id="ti")
    # Simulate a running test instance.
    ti = TestInstance(
        bug_id=bug_id,
        pids="{}",
        ports="{}",
        status="running",
    )
    db_session.add(ti)
    db_session.commit()
    current = repo.get_current_test_instance(db_session)
    assert current is not None
    assert current.bug_id == bug_id


def test_get_current_test_instance_ignores_stopped(db_session: Session) -> None:
    bug_id = _make_bug(db_session, notion_page_id="ti2")
    ti = TestInstance(
        bug_id=bug_id,
        pids="{}",
        ports="{}",
        status="stopped",
    )
    db_session.add(ti)
    db_session.commit()
    assert repo.get_current_test_instance(db_session) is None


def test_parse_affected_repos_handles_null_and_json() -> None:
    assert repo.parse_affected_repos(None) == []
    assert repo.parse_affected_repos("") == []
    assert repo.parse_affected_repos('["a", "b"]') == ["a", "b"]
    assert repo.parse_affected_repos("not-json") == []


def test_agent_runs_ordered_by_start_time(db_session: Session) -> None:
    bug_id = _make_bug(db_session, notion_page_id="ar2")
    # Insert two runs with different start times.
    first = repo.record_agent_run(
        db_session,
        bug_id=bug_id,
        agent_role="investigator",
        attempt=1,
        started_at=datetime(2026, 1, 1, 0, 0, 0),
        log_path="/tmp/a.log",
        prompt_path="/tmp/a.md",
    )
    second = repo.record_agent_run(
        db_session,
        bug_id=bug_id,
        agent_role="fixer",
        attempt=1,
        started_at=datetime(2026, 1, 1, 0, 1, 0),
        log_path="/tmp/b.log",
        prompt_path="/tmp/b.md",
    )
    db_session.commit()
    runs = repo.list_agent_runs(db_session, bug_id)
    assert [r.id for r in runs] == [first.id, second.id]
    assert isinstance(runs[0], AgentRun)
