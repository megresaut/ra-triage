"""Tests for :func:`ra_triage.db.repo._post_transition_hook`.

Post Agent 9 this hook dispatches to three best-effort side effects:

  1. Notion status write-back (Agent 3).
  2. Slack escalation notification — only on entering BLOCKED when an
     unresolved ``escalations`` row exists.
  3. Slack ready-for-review notification — only on entering
     READY_FOR_REVIEW.

Contract: failures in ANY of these must NOT roll back the DB state
transition. These tests verify each hook fires under the right
preconditions AND that a failure in one doesn't prevent the transition
from committing.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from ra_triage.db import repo
from ra_triage.integrations import notion as notion_mod
from ra_triage.integrations import slack as slack_mod
from ra_triage.orchestrator.states import BugState


@pytest.fixture(autouse=True)
def _reset_hooks() -> Any:
    """Isolate Slack + Notion test seams across tests."""
    slack_mod.set_client_builder(None)
    notion_mod.set_client_builder(None)
    yield
    slack_mod.set_client_builder(None)
    notion_mod.set_client_builder(None)


def _make_bug(session: Session, *, page_id: str) -> Any:
    bug = repo.create_bug(
        session,
        notion_page_id=page_id,
        project_id="ra_avm",
        title="Hook test",
        description="x",
    )
    session.commit()
    return bug


# --- Slack escalation hook -------------------------------------------------


def test_slack_escalation_fires_on_blocked_with_unresolved_row(
    alembic_db: object, db_session: Session
) -> None:
    _ = alembic_db
    bug = _make_bug(db_session, page_id="hook-esc-1")
    bug_id = bug.id
    # Seed an escalation row; this is what the hook checks.
    repo.record_escalation(
        db_session,
        bug_id=bug_id,
        triggered_rule="deny_list",
        brief_markdown="- **TL;DR:** hook test",
        options=json.dumps(
            [
                {"id": "approve_recommended", "label": "Approve Recommended", "kind": "approve"},
                {"id": "take_over", "label": "Take Over Manually", "kind": "take_over"},
            ]
        ),
    )
    db_session.commit()

    # Silence Notion so we can focus on the Slack hook.
    notion_mod.set_client_builder(lambda: None)

    fake_slack = MagicMock()
    slack_mod.set_client_builder(lambda: fake_slack)

    # Walk to BLOCKED.
    for next_state in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.BLOCKED,
    ):
        repo.update_bug_state(db_session, bug, next_state)
    db_session.commit()

    # Slack was called exactly once — on entering BLOCKED.
    assert fake_slack.post.call_count == 1


def test_slack_escalation_noop_when_blocked_without_row(
    alembic_db: object, db_session: Session
) -> None:
    """BLOCKED transitions without an escalation row skip Slack (e.g. take-over)."""
    _ = alembic_db
    bug = _make_bug(db_session, page_id="hook-esc-2")

    notion_mod.set_client_builder(lambda: None)
    fake_slack = MagicMock()
    slack_mod.set_client_builder(lambda: fake_slack)

    for next_state in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.BLOCKED,
    ):
        repo.update_bug_state(db_session, bug, next_state)
    db_session.commit()

    assert fake_slack.post.call_count == 0


def test_slack_escalation_noop_when_row_already_resolved(
    alembic_db: object, db_session: Session
) -> None:
    """A resolved escalation shouldn't re-fire Slack on a later BLOCKED transition."""
    _ = alembic_db
    bug = _make_bug(db_session, page_id="hook-esc-3")
    row = repo.record_escalation(
        db_session,
        bug_id=bug.id,
        triggered_rule="judgment",
        brief_markdown="- **TL;DR:** prior",
        options="[]",
    )
    repo.update_escalation_resolution(
        db_session,
        escalation_id=row.id,
        resolution="taken_over",
        resolved_option="take_over",
    )
    db_session.commit()

    notion_mod.set_client_builder(lambda: None)
    fake_slack = MagicMock()
    slack_mod.set_client_builder(lambda: fake_slack)

    for next_state in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.BLOCKED,
    ):
        repo.update_bug_state(db_session, bug, next_state)
    db_session.commit()

    assert fake_slack.post.call_count == 0


def test_slack_escalation_failure_does_not_roll_back_transition(
    alembic_db: object, db_session: Session
) -> None:
    _ = alembic_db
    bug = _make_bug(db_session, page_id="hook-esc-fail")
    repo.record_escalation(
        db_session,
        bug_id=bug.id,
        triggered_rule="deny_list",
        brief_markdown="- **TL;DR:** fail path",
        options="[]",
    )
    db_session.commit()

    notion_mod.set_client_builder(lambda: None)

    class _BadClient:
        def post(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("slack down")

        def close(self) -> None:
            return None

    slack_mod.set_client_builder(lambda: _BadClient())

    # Walk to BLOCKED. Must NOT raise.
    for next_state in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.BLOCKED,
    ):
        repo.update_bug_state(db_session, bug, next_state)
    db_session.commit()

    db_session.refresh(bug)
    assert bug.state == BugState.BLOCKED.value


# --- Slack RFR hook --------------------------------------------------------


def test_slack_rfr_fires_on_ready_for_review(
    alembic_db: object, db_session: Session
) -> None:
    _ = alembic_db
    bug = _make_bug(db_session, page_id="hook-rfr-1")

    notion_mod.set_client_builder(lambda: None)
    fake_slack = MagicMock()
    slack_mod.set_client_builder(lambda: fake_slack)

    # Walk to READY_FOR_REVIEW via a legal sequence.
    for next_state in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.FIXING,
        BugState.TESTING,
        BugState.READY_FOR_REVIEW,
    ):
        repo.update_bug_state(db_session, bug, next_state)
    db_session.commit()

    # Exactly one Slack post — triggered by the RFR transition. The
    # intermediate states (TRIAGED, INVESTIGATING, FIXING, TESTING) do
    # not ping Slack.
    assert fake_slack.post.call_count == 1
    text = fake_slack.post.call_args.kwargs.get("text", "")
    assert "ready for review" in text


def test_slack_rfr_failure_does_not_roll_back_transition(
    alembic_db: object, db_session: Session
) -> None:
    _ = alembic_db
    bug = _make_bug(db_session, page_id="hook-rfr-fail")

    notion_mod.set_client_builder(lambda: None)

    class _BadClient:
        def post(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("slack down")

        def close(self) -> None:
            return None

    slack_mod.set_client_builder(lambda: _BadClient())

    for next_state in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.FIXING,
        BugState.TESTING,
        BugState.READY_FOR_REVIEW,
    ):
        repo.update_bug_state(db_session, bug, next_state)
    db_session.commit()

    db_session.refresh(bug)
    assert bug.state == BugState.READY_FOR_REVIEW.value


# --- Cross-hook isolation --------------------------------------------------


def test_notion_failure_does_not_prevent_slack_ping(
    alembic_db: object, db_session: Session
) -> None:
    """If Notion write-back raises, the Slack escalation ping still fires."""
    _ = alembic_db
    bug = _make_bug(db_session, page_id="hook-cross-1")
    repo.record_escalation(
        db_session,
        bug_id=bug.id,
        triggered_rule="deny_list",
        brief_markdown="- **TL;DR:** cross",
        options="[]",
    )
    db_session.commit()

    notion_fail = MagicMock()
    notion_fail.set_status.side_effect = RuntimeError("notion down")
    notion_mod.set_client_builder(lambda: notion_fail)

    fake_slack = MagicMock()
    slack_mod.set_client_builder(lambda: fake_slack)

    for next_state in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.BLOCKED,
    ):
        repo.update_bug_state(db_session, bug, next_state)
    db_session.commit()

    # Slack still fired on BLOCKED despite Notion raising.
    assert fake_slack.post.call_count == 1


def test_cancel_does_not_ping_slack(
    alembic_db: object, db_session: Session
) -> None:
    """Cancelling a bug does NOT trigger a Slack ping (terminal state, not RFR/BLOCKED)."""
    _ = alembic_db
    bug = _make_bug(db_session, page_id="hook-cancel-1")

    notion_mod.set_client_builder(lambda: None)
    fake_slack = MagicMock()
    slack_mod.set_client_builder(lambda: fake_slack)

    repo.update_bug_state(db_session, bug, BugState.CANCELLED)
    db_session.commit()

    assert fake_slack.post.call_count == 0
