"""Bug state machine unit tests.

Covers:
- Every legal transition in TRANSITIONS is accepted by `transition()`.
- A representative set of illegal transitions raises
  `IllegalBugStateTransition`.
- `is_terminal()` is true exactly for FIXED, WONT_FIX, CANCELLED.
"""

from __future__ import annotations

import pytest

from ra_triage.orchestrator.states import (
    TERMINAL_STATES,
    TRANSITIONS,
    BugState,
    IllegalBugStateTransition,
    is_terminal,
    transition,
)


def test_every_legal_transition_is_accepted() -> None:
    for current, allowed in TRANSITIONS.items():
        for new in allowed:
            transition(current, new)  # should not raise


@pytest.mark.parametrize(
    ("current", "new"),
    [
        # OPEN cannot skip straight into INVESTIGATING.
        (BugState.OPEN, BugState.INVESTIGATING),
        # TRIAGED cannot go back to OPEN.
        (BugState.TRIAGED, BugState.OPEN),
        # INVESTIGATING cannot merge without going through TESTING/READY_FOR_REVIEW.
        (BugState.INVESTIGATING, BugState.MERGING),
        # DESIGNING cannot jump to READY_FOR_REVIEW.
        (BugState.DESIGNING, BugState.READY_FOR_REVIEW),
        # FIXING cannot skip TESTING.
        (BugState.FIXING, BugState.READY_FOR_REVIEW),
        # MERGING cannot re-enter INVESTIGATING.
        (BugState.MERGING, BugState.INVESTIGATING),
        # FIXED is terminal — no outgoing transitions at all.
        (BugState.FIXED, BugState.OPEN),
        (BugState.WONT_FIX, BugState.FIXING),
        (BugState.CANCELLED, BugState.MERGING),
        # BLOCKED cannot go straight to TESTING.
        (BugState.BLOCKED, BugState.TESTING),
    ],
)
def test_illegal_transitions_raise(current: BugState, new: BugState) -> None:
    with pytest.raises(IllegalBugStateTransition):
        transition(current, new)


def test_terminal_states_are_exactly_as_specified() -> None:
    assert TERMINAL_STATES == frozenset(
        {BugState.FIXED, BugState.WONT_FIX, BugState.CANCELLED}
    )
    for state in BugState:
        assert is_terminal(state) is (state in TERMINAL_STATES)


def test_terminal_states_have_no_outgoing_transitions() -> None:
    for terminal in TERMINAL_STATES:
        assert TRANSITIONS[terminal] == set()


def test_illegal_transition_message_names_both_states() -> None:
    with pytest.raises(IllegalBugStateTransition) as excinfo:
        transition(BugState.OPEN, BugState.MERGING)
    msg = str(excinfo.value)
    assert "OPEN" in msg and "MERGING" in msg
