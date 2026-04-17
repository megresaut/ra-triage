"""Unit tests for the Notion status write-back hook.

The hook fires from `db.repo.update_bug_state` after a successful state
transition. We inject a fake NotionClient via
`integrations.notion.set_client_builder` so we can observe (or break)
the write-back without real network traffic.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from ra_triage.db import repo
from ra_triage.integrations import notion as notion_mod
from ra_triage.integrations.notion import STATE_TO_NOTION_STATUS
from ra_triage.orchestrator.states import BugState


def _make_bug(session: Session, *, page_id: str = "nbk-1") -> int:
    bug = repo.create_bug(
        session,
        notion_page_id=page_id,
        project_id="ra_avm",
        title="t",
        description="d",
    )
    session.commit()
    return bug.id


@pytest.fixture(autouse=True)
def reset_client_builder() -> Any:
    """Ensure the writeback hook has no custom builder installed across tests."""
    notion_mod.set_client_builder(None)
    yield
    notion_mod.set_client_builder(None)


# --- mapping coverage -------------------------------------------------------


def test_state_to_notion_status_covers_required_transitions() -> None:
    """Every transition named in the orchestrator spec maps to the right status."""
    assert STATE_TO_NOTION_STATUS[BugState.TRIAGED] == "Triaged"
    for in_progress in (
        BugState.INVESTIGATING,
        BugState.DESIGNING,
        BugState.FIXING,
        BugState.FIXING_PARALLEL,
        BugState.TESTING,
        BugState.INTEGRATION_TESTING,
    ):
        assert STATE_TO_NOTION_STATUS[in_progress] == "In progress"
    assert STATE_TO_NOTION_STATUS[BugState.BLOCKED] == "Blocked"
    assert STATE_TO_NOTION_STATUS[BugState.FIXED] == "Fixed"
    assert STATE_TO_NOTION_STATUS[BugState.WONT_FIX] == "Won't Fix"
    assert STATE_TO_NOTION_STATUS[BugState.CANCELLED] == "Won't Fix"


def test_no_writeback_for_unmapped_states() -> None:
    """OPEN, MERGING, READY_FOR_REVIEW don't write to Notion."""
    for unmapped in (BugState.OPEN, BugState.MERGING, BugState.READY_FOR_REVIEW):
        assert unmapped not in STATE_TO_NOTION_STATUS


# --- hook happy paths --------------------------------------------------------


def test_transition_triaged_calls_set_status_triaged(
    alembic_db: object, db_session: Session
) -> None:
    _ = alembic_db
    bug_id = _make_bug(db_session, page_id="page-triaged")
    fake_client = MagicMock()
    notion_mod.set_client_builder(lambda: fake_client)

    bug = repo.get_bug(db_session, bug_id)
    assert bug is not None
    repo.update_bug_state(db_session, bug, BugState.TRIAGED)
    db_session.commit()

    fake_client.set_status.assert_called_once_with(
        page_id="page-triaged", notion_status="Triaged"
    )
    # Transition committed despite the side effect.
    db_session.refresh(bug)
    assert bug.state == BugState.TRIAGED.value


def test_transition_to_investigating_maps_to_in_progress(
    alembic_db: object, db_session: Session
) -> None:
    _ = alembic_db
    bug_id = _make_bug(db_session, page_id="page-inv")
    fake_client = MagicMock()
    notion_mod.set_client_builder(lambda: fake_client)

    bug = repo.get_bug(db_session, bug_id)
    assert bug is not None
    repo.update_bug_state(db_session, bug, BugState.TRIAGED)
    repo.update_bug_state(db_session, bug, BugState.INVESTIGATING)
    db_session.commit()

    assert fake_client.set_status.call_count == 2
    # Second call mapped INVESTIGATING → "In progress".
    _, kwargs = fake_client.set_status.call_args_list[1]
    assert kwargs == {"page_id": "page-inv", "notion_status": "In progress"}


def test_transition_to_fixed_maps_to_fixed(
    alembic_db: object, db_session: Session
) -> None:
    _ = alembic_db
    bug_id = _make_bug(db_session, page_id="page-fixed")
    fake_client = MagicMock()
    notion_mod.set_client_builder(lambda: fake_client)

    bug = repo.get_bug(db_session, bug_id)
    assert bug is not None
    # Walk: OPEN → TRIAGED → INVESTIGATING → FIXING → TESTING → RFR → MERGING → FIXED
    for next_state in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.FIXING,
        BugState.TESTING,
        BugState.READY_FOR_REVIEW,
        BugState.MERGING,
        BugState.FIXED,
    ):
        repo.update_bug_state(db_session, bug, next_state)
    db_session.commit()

    # READY_FOR_REVIEW + MERGING have no writeback; the rest do. So 5 calls.
    statuses_written = [
        call.kwargs["notion_status"] for call in fake_client.set_status.call_args_list
    ]
    assert statuses_written == [
        "Triaged",
        "In progress",
        "In progress",  # FIXING
        "In progress",  # TESTING
        "Fixed",
    ]


def test_transition_cancel_maps_to_wont_fix(
    alembic_db: object, db_session: Session
) -> None:
    _ = alembic_db
    bug_id = _make_bug(db_session, page_id="page-cancel")
    fake_client = MagicMock()
    notion_mod.set_client_builder(lambda: fake_client)

    bug = repo.get_bug(db_session, bug_id)
    assert bug is not None
    repo.update_bug_state(db_session, bug, BugState.CANCELLED)
    db_session.commit()

    fake_client.set_status.assert_called_once_with(
        page_id="page-cancel", notion_status="Won't Fix"
    )


# --- failure isolation -------------------------------------------------------


def test_writeback_failure_does_not_rollback_transition(
    alembic_db: object, db_session: Session
) -> None:
    _ = alembic_db
    bug_id = _make_bug(db_session, page_id="page-raise")
    fake_client = MagicMock()
    fake_client.set_status.side_effect = RuntimeError("notion exploded")
    notion_mod.set_client_builder(lambda: fake_client)

    bug = repo.get_bug(db_session, bug_id)
    assert bug is not None
    # Must not raise.
    repo.update_bug_state(db_session, bug, BugState.TRIAGED)
    db_session.commit()

    db_session.refresh(bug)
    assert bug.state == BugState.TRIAGED.value


def test_writeback_builder_raising_is_swallowed(
    alembic_db: object, db_session: Session
) -> None:
    _ = alembic_db
    bug_id = _make_bug(db_session, page_id="page-builder-raise")

    def _bad_builder() -> Any:
        raise RuntimeError("settings broken")

    notion_mod.set_client_builder(_bad_builder)

    bug = repo.get_bug(db_session, bug_id)
    assert bug is not None
    repo.update_bug_state(db_session, bug, BugState.TRIAGED)
    db_session.commit()

    db_session.refresh(bug)
    assert bug.state == BugState.TRIAGED.value


# --- unconfigured Notion: no-op ---------------------------------------------


def test_writeback_is_noop_when_notion_unconfigured(
    alembic_db: object, db_session: Session
) -> None:
    _ = alembic_db
    bug_id = _make_bug(db_session, page_id="page-noop")
    # Builder returns None → NotionClient unconfigured.
    notion_mod.set_client_builder(lambda: None)

    bug = repo.get_bug(db_session, bug_id)
    assert bug is not None
    repo.update_bug_state(db_session, bug, BugState.TRIAGED)
    db_session.commit()

    db_session.refresh(bug)
    assert bug.state == BugState.TRIAGED.value  # transition still succeeded


# --- bug without notion_page_id skips writeback ------------------------------


def test_writeback_skips_when_bug_has_empty_page_id(
    alembic_db: object, db_session: Session
) -> None:
    _ = alembic_db

    class _Fake:
        def __init__(self, page_id: str, state: str) -> None:
            self.notion_page_id = page_id
            self.id = 99
            self.state = state

    fake_client = MagicMock()
    notion_mod.set_client_builder(lambda: fake_client)

    from ra_triage.integrations.notion import status_writeback_for_state

    status_writeback_for_state(_Fake("", "TRIAGED"), BugState.TRIAGED)
    fake_client.set_status.assert_not_called()


# --- writeback state mapping covers every mapped state (parametrised) -------


@pytest.mark.parametrize(
    ("terminal_path_end", "expected_status"),
    [
        (BugState.TRIAGED, "Triaged"),
        (BugState.BLOCKED, "Blocked"),
    ],
)
def test_state_mappings_parametrized(
    alembic_db: object,
    db_session: Session,
    terminal_path_end: BugState,
    expected_status: str,
) -> None:
    _ = alembic_db
    bug_id = _make_bug(
        db_session, page_id=f"pg-{terminal_path_end.value.lower()}"
    )
    fake_client = MagicMock()
    notion_mod.set_client_builder(lambda: fake_client)

    bug = repo.get_bug(db_session, bug_id)
    assert bug is not None
    # Minimal path to reach the target state.
    if terminal_path_end == BugState.TRIAGED:
        repo.update_bug_state(db_session, bug, BugState.TRIAGED)
    elif terminal_path_end == BugState.BLOCKED:
        repo.update_bug_state(db_session, bug, BugState.TRIAGED)
        repo.update_bug_state(db_session, bug, BugState.INVESTIGATING)
        repo.update_bug_state(db_session, bug, BugState.BLOCKED)
    db_session.commit()

    # The LAST set_status call corresponds to the target state.
    assert (
        fake_client.set_status.call_args_list[-1].kwargs["notion_status"]
        == expected_status
    )
