"""End-to-end escalation flow from the pipeline's perspective (Agent 9).

When the Investigator produces an Escalation Brief and the pipeline
routes the bug to BLOCKED, we expect:

  1. An ``escalations`` row is persisted with the raw brief + parsed
     options as JSON, and a ``triggered_rule`` that reflects whether a
     deny-list path was hit.
  2. :func:`parse_escalation_brief` runs against the live TRIAGE.md so
     the persisted options match what the API surface exposes.
  3. The full brief is mirrored to the bug's Notion page via
     ``append_block`` (mocked).
  4. The Slack escalation notification fires exactly once via the
     post-transition hook (mocked).

We reuse the ``stub_worktrees`` + ``_make_fake_investigator`` helpers
from ``test_pipeline.py`` to keep the setup compact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.db.models import Escalation
from ra_triage.integrations import notion as notion_mod
from ra_triage.integrations import slack as slack_mod
from ra_triage.orchestrator import pipeline as pipeline_module
from ra_triage.orchestrator.states import BugState

# Reuse the stubs defined in the broader pipeline tests.
from tests.unit.test_pipeline import (
    _make_bug,
    _make_fake_investigator,
    stub_worktrees,  # noqa: F401  (fixture re-export)
)

_ESCALATION_BRIEF = (
    "- **TL;DR:** Auth migration required; deny-list hit.\n"
    "- **Why blocked:** deny_list path auth/** matched.\n"
    "- **Proposed approach:** Additive migration adding a legacy_token column.\n"
    "- **Ranked alternatives:**\n"
    "  - **Alternative 1:** Session rehydrate via headers only.\n"
    "  - **Alternative 2:** Deprecate legacy tokens entirely.\n"
    "- **Files that would be touched:**\n"
    "  - backend/auth/token.py\n"
    "- **Test plan:** Integration test for session rehydrate.\n"
)


_TRIAGE_MD_WITH_BRIEF = (
    "## Root Cause\n\nsrc/auth/token.py:42\n\n"
    "## Scope\n\n"
    "scope: single-repo\n"
    'affected_repos: ["avm-backend"]\n'
    "deny_list_hit: true\n\n"
    "## Analysis\n\nauth touched.\n\n"
    "## Escalation Brief\n\n"
    f"{_ESCALATION_BRIEF}"
)


@pytest.fixture(autouse=True)
def _reset_slack_notion_hooks() -> Any:
    """Isolate Slack + Notion test seams across tests in this module."""
    slack_mod.set_client_builder(None)
    notion_mod.set_client_builder(None)
    yield
    slack_mod.set_client_builder(None)
    notion_mod.set_client_builder(None)


async def test_pipeline_persists_escalation_row_and_notifies_slack(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_worktrees: dict[str, Path],  # noqa: F811
) -> None:
    """BLOCKED + Escalation Brief → escalations row + Slack ping + Notion mirror."""
    monkeypatch.setattr(
        pipeline_module,
        "Investigator",
        _make_fake_investigator(
            completion_token="INVESTIGATOR_COMPLETE",
            triage_md_body=_TRIAGE_MD_WITH_BRIEF,
        ),
    )

    fake_slack = MagicMock()
    slack_mod.set_client_builder(lambda: fake_slack)

    fake_notion = MagicMock()
    notion_mod.set_client_builder(lambda: fake_notion)

    session = engine_module.SessionLocal()
    bug = _make_bug(session, notion_page_id="esc-flow-1")
    bug_id = bug.id
    session.close()

    await pipeline_module.run_pipeline(bug_id)

    # Assertions: bug ended in BLOCKED with the deny-list flag propagated.
    session = engine_module.SessionLocal()
    try:
        got = repo_module.get_bug(session, bug_id)
        assert got is not None
        assert got.state == BugState.BLOCKED.value
        assert got.deny_list_hit is True

        # One escalation row was persisted with the expected shape.
        rows = session.query(Escalation).filter(Escalation.bug_id == bug_id).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.triggered_rule == "deny_list"
        assert row.brief_markdown.strip().startswith("- **TL;DR:**")
        assert row.resolution is None

        options = json.loads(row.options)
        option_ids = [o["id"] for o in options]
        assert option_ids == [
            "approve_recommended",
            "alternative_1",
            "alternative_2",
            "take_over",
        ]
        # Kind classification mirrors ParsedBriefOption.
        kinds = {o["id"]: o["kind"] for o in options}
        assert kinds["approve_recommended"] == "approve"
        assert kinds["alternative_1"] == "alternative"
        assert kinds["take_over"] == "take_over"
    finally:
        session.close()

    # Slack escalation hook fired exactly once via the post-transition hook.
    assert fake_slack.post.call_count == 1
    post_args = fake_slack.post.call_args
    # Positional or keyword for text; we use keyword text=...
    text_fallback = post_args.kwargs.get("text") or (
        post_args.args[0] if post_args.args else ""
    )
    assert str(bug_id) in text_fallback
    # Slack close is called exactly once per ping (best-effort cleanup).
    assert fake_slack.close.call_count == 1

    # Notion mirror: append_block received the full brief.
    assert fake_notion.append_block.call_count == 1
    call = fake_notion.append_block.call_args
    assert call.kwargs["page_id"] == "esc-flow-1"
    assert "Escalation Brief" in call.kwargs["markdown_text"]
    assert "TL;DR:" in call.kwargs["markdown_text"]


async def test_pipeline_escalation_judgment_when_deny_list_false(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_worktrees: dict[str, Path],  # noqa: F811
) -> None:
    """Deny-list flag false → triggered_rule='judgment'."""
    judgment_brief = (
        "## Root Cause\n\nsrc/x.py\n\n"
        "## Scope\n\n"
        "scope: single-repo\n"
        'affected_repos: ["avm-backend"]\n'
        "deny_list_hit: false\n\n"
        "## Analysis\n\nambiguous.\n\n"
        "## Escalation Brief\n\n"
        "- **TL;DR:** Needs human judgment call.\n"
    )
    monkeypatch.setattr(
        pipeline_module,
        "Investigator",
        _make_fake_investigator(
            completion_token="INVESTIGATOR_COMPLETE",
            triage_md_body=judgment_brief,
        ),
    )

    session = engine_module.SessionLocal()
    bug = _make_bug(session, notion_page_id="judgment-1")
    bug_id = bug.id
    session.close()

    await pipeline_module.run_pipeline(bug_id)

    session = engine_module.SessionLocal()
    try:
        rows = session.query(Escalation).filter(Escalation.bug_id == bug_id).all()
        assert len(rows) == 1
        assert rows[0].triggered_rule == "judgment"
    finally:
        session.close()


async def test_notion_mirror_failure_does_not_break_transition(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_worktrees: dict[str, Path],  # noqa: F811
) -> None:
    """A Notion outage must not prevent BLOCKED transition or escalation row."""
    monkeypatch.setattr(
        pipeline_module,
        "Investigator",
        _make_fake_investigator(
            completion_token="INVESTIGATOR_COMPLETE",
            triage_md_body=_TRIAGE_MD_WITH_BRIEF,
        ),
    )

    class _AngryNotion:
        def append_block(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("notion down")

    notion_mod.set_client_builder(lambda: _AngryNotion())

    session = engine_module.SessionLocal()
    bug = _make_bug(session, notion_page_id="notion-fail-1")
    bug_id = bug.id
    session.close()

    await pipeline_module.run_pipeline(bug_id)

    session = engine_module.SessionLocal()
    try:
        got = repo_module.get_bug(session, bug_id)
        assert got is not None
        assert got.state == BugState.BLOCKED.value
        rows = session.query(Escalation).filter(Escalation.bug_id == bug_id).all()
        assert len(rows) == 1
    finally:
        session.close()
