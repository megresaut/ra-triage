"""Unit tests for ``db.repo.list_inflight_for_repo`` + Agent 11 helpers.

The rebase sweeper queries ``list_inflight_for_repo(repo_name, exclude_bug_id)``
to find every other in-flight bug holding a worktree for the just-merged
repo. The function MUST exclude:

  - terminal bugs (FIXED, WONT_FIX, CANCELLED) — no worktree to rebase.
  - bugs in MERGING — racy to rebase mid-merge.
  - the trigger bug itself (``exclude_bug_id``).
  - bugs whose ``affected_repos`` does NOT contain ``repo_name``.

We also exercise ``record_rebase_attempt`` + ``mark_blocked_with_conflict``
+ ``parse_conflict_files`` + ``clear_conflict_files`` here since they're
directly chained from the sweeper's hot path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ra_triage.db import repo as repo_module
from ra_triage.orchestrator.states import BugState

_PATH_TO_STATE: dict[BugState, list[BugState]] = {
    BugState.OPEN: [],
    BugState.TRIAGED: [BugState.TRIAGED],
    BugState.INVESTIGATING: [BugState.TRIAGED, BugState.INVESTIGATING],
    BugState.DESIGNING: [
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.DESIGNING,
    ],
    BugState.FIXING: [
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.FIXING,
    ],
    BugState.FIXING_PARALLEL: [
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.DESIGNING,
        BugState.FIXING_PARALLEL,
    ],
    BugState.TESTING: [
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.FIXING,
        BugState.TESTING,
    ],
    BugState.INTEGRATION_TESTING: [
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.DESIGNING,
        BugState.FIXING_PARALLEL,
        BugState.INTEGRATION_TESTING,
    ],
    BugState.READY_FOR_REVIEW: [
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.FIXING,
        BugState.TESTING,
        BugState.READY_FOR_REVIEW,
    ],
    BugState.BLOCKED: [
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.BLOCKED,
    ],
    BugState.MERGING: [
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.FIXING,
        BugState.TESTING,
        BugState.READY_FOR_REVIEW,
        BugState.MERGING,
    ],
    BugState.FIXED: [
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.FIXING,
        BugState.TESTING,
        BugState.READY_FOR_REVIEW,
        BugState.MERGING,
        BugState.FIXED,
    ],
    BugState.CANCELLED: [BugState.CANCELLED],
    BugState.WONT_FIX: [
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.BLOCKED,
        BugState.WONT_FIX,
    ],
}


def _make_bug(
    session,
    *,
    notion_page_id: str,
    state: BugState,
    affected_repos: list[str] | None = None,
):
    """Create a bug, walk to ``state`` via legal transitions, set affected_repos."""
    bug = repo_module.create_bug(
        session,
        notion_page_id=notion_page_id,
        project_id="ra_avm",
        title="t",
        description="d",
    )
    if affected_repos is not None:
        bug.affected_repos = json.dumps(affected_repos)
        session.flush()

    for step in _PATH_TO_STATE[state]:
        repo_module.update_bug_state(session, bug, step)
    session.commit()
    return bug


def test_list_inflight_excludes_terminal_states(db_session) -> None:
    """FIXED, WONT_FIX, CANCELLED bugs never appear."""
    inflight = _make_bug(
        db_session,
        notion_page_id="p-inflight",
        state=BugState.FIXING,
        affected_repos=["avm-backend"],
    )
    _ = _make_bug(
        db_session,
        notion_page_id="p-fixed",
        state=BugState.FIXED,
        affected_repos=["avm-backend"],
    )
    _ = _make_bug(
        db_session,
        notion_page_id="p-cancelled",
        state=BugState.CANCELLED,
        affected_repos=["avm-backend"],
    )
    _ = _make_bug(
        db_session,
        notion_page_id="p-wont-fix",
        state=BugState.WONT_FIX,
        affected_repos=["avm-backend"],
    )

    rows = repo_module.list_inflight_for_repo(db_session, "avm-backend")
    ids = [b.id for b in rows]
    assert ids == [inflight.id]


def test_list_inflight_excludes_merging_state(db_session) -> None:
    """Bugs currently being merged are NOT swept (racy)."""
    inflight = _make_bug(
        db_session,
        notion_page_id="p-fixing",
        state=BugState.FIXING,
        affected_repos=["avm-backend"],
    )
    _ = _make_bug(
        db_session,
        notion_page_id="p-merging",
        state=BugState.MERGING,
        affected_repos=["avm-backend"],
    )
    rows = repo_module.list_inflight_for_repo(db_session, "avm-backend")
    assert [b.id for b in rows] == [inflight.id]


def test_list_inflight_excludes_trigger_bug(db_session) -> None:
    """``exclude_bug_id`` (the trigger) is filtered out."""
    bug_a = _make_bug(
        db_session,
        notion_page_id="p-a",
        state=BugState.FIXING,
        affected_repos=["avm-backend"],
    )
    bug_b = _make_bug(
        db_session,
        notion_page_id="p-b",
        state=BugState.FIXING,
        affected_repos=["avm-backend"],
    )

    rows = repo_module.list_inflight_for_repo(
        db_session, "avm-backend", exclude_bug_id=bug_a.id
    )
    assert [b.id for b in rows] == [bug_b.id]


def test_list_inflight_filters_by_affected_repos(db_session) -> None:
    """Bugs whose ``affected_repos`` does NOT include the repo are skipped."""
    fe_only = _make_bug(
        db_session,
        notion_page_id="p-fe",
        state=BugState.FIXING,
        affected_repos=["builder-frontend"],
    )
    be_one = _make_bug(
        db_session,
        notion_page_id="p-be1",
        state=BugState.FIXING,
        affected_repos=["avm-backend"],
    )
    cross = _make_bug(
        db_session,
        notion_page_id="p-cross",
        state=BugState.FIXING_PARALLEL,
        affected_repos=["avm-backend", "builder-frontend"],
    )

    be_results = repo_module.list_inflight_for_repo(
        db_session, "avm-backend"
    )
    assert sorted(b.id for b in be_results) == sorted([be_one.id, cross.id])
    assert fe_only.id not in [b.id for b in be_results]

    fe_results = repo_module.list_inflight_for_repo(
        db_session, "builder-frontend"
    )
    assert sorted(b.id for b in fe_results) == sorted([fe_only.id, cross.id])


def test_list_inflight_includes_blocked_and_ready_for_review(
    db_session,
) -> None:
    """BLOCKED and READY_FOR_REVIEW bugs still hold worktrees and are swept."""
    blocked = _make_bug(
        db_session,
        notion_page_id="p-blocked",
        state=BugState.BLOCKED,
        affected_repos=["avm-backend"],
    )
    rfr = _make_bug(
        db_session,
        notion_page_id="p-rfr",
        state=BugState.READY_FOR_REVIEW,
        affected_repos=["avm-backend"],
    )

    rows = repo_module.list_inflight_for_repo(db_session, "avm-backend")
    ids = sorted(b.id for b in rows)
    assert ids == sorted([blocked.id, rfr.id])


# --- record_rebase_attempt + list_rebase_attempts -------------------------


def test_record_rebase_attempt_clean(db_session) -> None:
    """A clean attempt records the row with no conflict_files."""
    trigger = _make_bug(
        db_session, notion_page_id="p-t", state=BugState.READY_FOR_REVIEW
    )
    target = _make_bug(
        db_session,
        notion_page_id="p-target",
        state=BugState.FIXING,
        affected_repos=["avm-backend"],
    )

    row = repo_module.record_rebase_attempt(
        db_session,
        bug_id=target.id,
        repo_name="avm-backend",
        trigger_bug_id=trigger.id,
        result="clean",
    )
    db_session.commit()
    assert row.result == "clean"
    assert row.conflict_files is None

    rows = repo_module.list_rebase_attempts(db_session, target.id)
    assert len(rows) == 1
    assert rows[0].id == row.id


def test_record_rebase_attempt_conflict_stores_files(db_session) -> None:
    """A conflict attempt persists the file list as JSON."""
    trigger = _make_bug(
        db_session, notion_page_id="p-t2", state=BugState.READY_FOR_REVIEW
    )
    target = _make_bug(
        db_session,
        notion_page_id="p-target2",
        state=BugState.FIXING,
        affected_repos=["avm-backend"],
    )

    row = repo_module.record_rebase_attempt(
        db_session,
        bug_id=target.id,
        repo_name="avm-backend",
        trigger_bug_id=trigger.id,
        result="conflict",
        conflict_files=["api/handlers.go", "api/db.go"],
    )
    db_session.commit()
    assert row.conflict_files is not None
    assert json.loads(row.conflict_files) == ["api/handlers.go", "api/db.go"]


def test_record_rebase_attempt_invalid_result_raises(db_session) -> None:
    """Unknown ``result`` strings raise ValueError."""
    bug = _make_bug(
        db_session,
        notion_page_id="p-bad",
        state=BugState.FIXING,
        affected_repos=["avm-backend"],
    )
    with pytest.raises(ValueError):
        repo_module.record_rebase_attempt(
            db_session,
            bug_id=bug.id,
            repo_name="avm-backend",
            trigger_bug_id=bug.id,
            result="purple-monkey",
        )


# --- mark_blocked_with_conflict --------------------------------------------


def test_mark_blocked_with_conflict_transitions_and_stamps(db_session) -> None:
    """From an in-flight state we transition → BLOCKED + stamp conflict_files."""
    trigger = _make_bug(
        db_session, notion_page_id="p-mb-t", state=BugState.READY_FOR_REVIEW
    )
    target = _make_bug(
        db_session,
        notion_page_id="p-mb-target",
        state=BugState.FIXING,
        affected_repos=["avm-backend"],
    )

    # First record a conflict row so mark_blocked... can backfill it.
    repo_module.record_rebase_attempt(
        db_session,
        bug_id=target.id,
        repo_name="avm-backend",
        trigger_bug_id=trigger.id,
        result="conflict",
    )
    db_session.commit()

    files = ["api/handlers.go"]
    repo_module.mark_blocked_with_conflict(
        db_session, target.id, "avm-backend", files
    )
    db_session.commit()

    refreshed = repo_module.get_bug(db_session, target.id)
    assert refreshed is not None
    assert refreshed.state == BugState.BLOCKED.value
    assert refreshed.conflict_files is not None
    assert json.loads(refreshed.conflict_files) == files

    # The latest rebase_attempts row got conflict_files backfilled.
    attempts = repo_module.list_rebase_attempts(db_session, target.id)
    assert attempts[-1].conflict_files is not None
    assert json.loads(attempts[-1].conflict_files) == files


def test_mark_blocked_with_conflict_idempotent_when_already_blocked(
    db_session,
) -> None:
    """If the bug is already BLOCKED we just stamp the column (no transition)."""
    target = _make_bug(
        db_session,
        notion_page_id="p-mb-already",
        state=BugState.BLOCKED,
        affected_repos=["avm-backend"],
    )
    repo_module.mark_blocked_with_conflict(
        db_session, target.id, "avm-backend", ["x.go"]
    )
    db_session.commit()
    refreshed = repo_module.get_bug(db_session, target.id)
    assert refreshed is not None
    assert refreshed.state == BugState.BLOCKED.value
    assert refreshed.conflict_files is not None
    assert json.loads(refreshed.conflict_files) == ["x.go"]


# --- clear_conflict_files / parse_conflict_files ---------------------------


def test_clear_conflict_files_nulls_the_column(db_session) -> None:
    target = _make_bug(
        db_session,
        notion_page_id="p-clear",
        state=BugState.BLOCKED,
        affected_repos=["avm-backend"],
    )
    target.conflict_files = json.dumps(["a.go"])
    db_session.flush()
    repo_module.clear_conflict_files(db_session, target.id)
    db_session.commit()
    refreshed = repo_module.get_bug(db_session, target.id)
    assert refreshed is not None
    assert refreshed.conflict_files is None


def test_parse_conflict_files_round_trips_or_returns_none(db_session) -> None:
    """``parse_conflict_files`` is symmetric with the JSON encoding."""
    assert repo_module.parse_conflict_files(None) is None
    assert repo_module.parse_conflict_files(json.dumps(["a", "b"])) == [
        "a",
        "b",
    ]
    # Garbled JSON returns None (defensive).
    assert repo_module.parse_conflict_files("not-json{") is None
    # Wrong shape (object instead of list) returns None.
    assert repo_module.parse_conflict_files('{"k": "v"}') is None


# Pytest signature: _ keeps the unused fixture happy.
_ = Path
