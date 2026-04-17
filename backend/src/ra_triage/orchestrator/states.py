"""Bug lifecycle state machine.

This module is the canonical source of the `BugState` enum and the legal
transitions between them. Tech plan §4 is the visual reference; the
`TRANSITIONS` dict below is authoritative.

The repository layer (`db.repo.update_bug_state`) calls `transition()` before
writing, so every illegal transition surfaces the same exception regardless of
the caller. The pipeline (`orchestrator.pipeline`, Agent 5+) uses the same
helpers when advancing a bug between steps.

The design is deliberately "pure": no Bug object, no DB access. This keeps the
rules reviewable at a glance and keeps the state machine independently testable.
"""

from __future__ import annotations

import enum


class BugState(enum.StrEnum):
    """Every state a bug can be in during its lifecycle.

    Terminal states (FIXED, WONT_FIX, CANCELLED) have no outgoing transitions.
    """

    OPEN = "OPEN"
    TRIAGED = "TRIAGED"
    INVESTIGATING = "INVESTIGATING"
    DESIGNING = "DESIGNING"
    FIXING = "FIXING"
    FIXING_PARALLEL = "FIXING_PARALLEL"
    TESTING = "TESTING"
    INTEGRATION_TESTING = "INTEGRATION_TESTING"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    BLOCKED = "BLOCKED"
    MERGING = "MERGING"
    FIXED = "FIXED"
    WONT_FIX = "WONT_FIX"
    CANCELLED = "CANCELLED"


TERMINAL_STATES: frozenset[BugState] = frozenset(
    {BugState.FIXED, BugState.WONT_FIX, BugState.CANCELLED}
)


# Legal transitions as declared in the orchestrator instructions for Agent 2.
# Every state maps to the set of states it may transition into.
TRANSITIONS: dict[BugState, set[BugState]] = {
    BugState.OPEN: {BugState.TRIAGED, BugState.CANCELLED},
    BugState.TRIAGED: {BugState.INVESTIGATING, BugState.CANCELLED},
    BugState.INVESTIGATING: {
        BugState.FIXING,
        BugState.DESIGNING,
        BugState.BLOCKED,
        BugState.CANCELLED,
    },
    BugState.DESIGNING: {
        BugState.FIXING_PARALLEL,
        BugState.BLOCKED,
        BugState.CANCELLED,
    },
    BugState.FIXING: {BugState.TESTING, BugState.BLOCKED, BugState.CANCELLED},
    BugState.FIXING_PARALLEL: {
        BugState.INTEGRATION_TESTING,
        BugState.DESIGNING,
        BugState.BLOCKED,
        BugState.CANCELLED,
    },
    BugState.TESTING: {BugState.READY_FOR_REVIEW, BugState.BLOCKED, BugState.CANCELLED},
    BugState.INTEGRATION_TESTING: {
        BugState.READY_FOR_REVIEW,
        BugState.DESIGNING,
        BugState.BLOCKED,
        BugState.CANCELLED,
    },
    BugState.READY_FOR_REVIEW: {
        BugState.MERGING,
        BugState.INVESTIGATING,
        BugState.BLOCKED,
        BugState.CANCELLED,
    },
    BugState.BLOCKED: {
        BugState.FIXING,
        BugState.FIXING_PARALLEL,
        BugState.CANCELLED,
        BugState.WONT_FIX,
    },
    BugState.MERGING: {BugState.FIXED, BugState.BLOCKED},
    BugState.FIXED: set(),
    BugState.WONT_FIX: set(),
    BugState.CANCELLED: set(),
}


class IllegalBugStateTransition(Exception):
    """Raised when a transition is attempted that is not in TRANSITIONS[current]."""


def is_terminal(state: BugState) -> bool:
    """Return True if `state` is a terminal state."""
    return state in TERMINAL_STATES


def transition(current: BugState, new: BugState) -> None:
    """Validate a state transition.

    Raises `IllegalBugStateTransition` if `new` is not in the allowed set for
    `current`. Returns None on success — the caller performs the write.
    """
    allowed = TRANSITIONS.get(current, set())
    if new not in allowed:
        raise IllegalBugStateTransition(
            f"Cannot transition from {current.value} to {new.value}; "
            f"allowed next states are {sorted(s.value for s in allowed) or '(none — terminal)'}."
        )
