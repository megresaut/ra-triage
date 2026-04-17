"""Tests for ``orchestrator.pipeline.reinvestigate_with_feedback``.

Verifies that:
  1. The Investigator is spawned with the rejection feedback in its
     rendered prompt (grep ``investigator.2.md`` for the reason text).
  2. The resulting TRIAGE.md drives the normal routing (single-repo →
     FIXING → TESTING → READY_FOR_REVIEW) just like the forward path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from ra_triage.agents.base import AgentResult
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.orchestrator import pipeline as pipeline_module
from ra_triage.orchestrator.states import BugState


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _walk_to_rfr(session: Session, bug_id: int) -> None:
    bug = repo_module.get_bug(session, bug_id)
    assert bug is not None
    for nxt in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.FIXING,
        BugState.TESTING,
        BugState.READY_FOR_REVIEW,
    ):
        repo_module.update_bug_state(session, bug, nxt)
    session.commit()


@pytest.fixture()
def seed_worktrees(
    triage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[int, dict[str, Path]]:
    """Stub worktree creation/listing so the pipeline doesn't touch git.

    Returns the per-bug {repo_name: path} map so tests can inspect.
    """
    from ra_triage.workspaces import worktree as wt_module

    per_bug: dict[int, dict[str, Path]] = {}

    async def _fake_create(bug_id: int, repos: list[Any]) -> dict[str, Path]:
        mapping: dict[str, Path] = {}
        for r in repos:
            dest = triage_home / "worktrees" / str(bug_id) / r.name
            dest.mkdir(parents=True, exist_ok=True)
            mapping[r.name] = dest
        per_bug[bug_id] = mapping
        return mapping

    def _fake_list(bug_id: int) -> dict[str, Path]:
        """List worktrees for ``bug_id`` — used by reinvestigate path."""
        parent = triage_home / "worktrees" / str(bug_id)
        if not parent.is_dir():
            return {}
        return {
            child.name: child
            for child in sorted(parent.iterdir())
            if child.is_dir()
        }

    monkeypatch.setattr(wt_module, "create_worktrees_for_bug", _fake_create)
    monkeypatch.setattr(wt_module, "list_worktrees", _fake_list)
    return per_bug


def _make_fake_investigator_capturing(
    *,
    captured: dict[str, Any],
    completion_token: str = "INVESTIGATOR_COMPLETE",
    triage_md_body: str = "",
) -> Any:
    """Capture the context dict + render a TRIAGE.md + persist the prompt."""

    class _FakeInvestigator:
        role = "investigator"

        async def run(
            self, bug: Any, worktree: Path, context: dict[str, Any]
        ) -> AgentResult:
            captured["context"] = context
            captured["attempt"] = context["attempt"]
            # Write TRIAGE.md as the Investigator would.
            triage_path = worktree / "TRIAGE.md"
            triage_path.write_text(triage_md_body, encoding="utf-8")
            # The base class normally renders the prompt template to disk.
            # We render it ourselves here so the test can grep it for the
            # rejection_feedback string — that's the actual acceptance
            # criterion in the Agent 8 spec.
            from ra_triage.agents.base import _jinja_env
            from ra_triage.settings import get_settings

            template = _jinja_env.get_template("investigator.md.j2")
            rendered = template.render(**context)
            prompt_path = (
                get_settings().prompts_dir
                / str(bug.id)
                / f"investigator.{context['attempt']}.md"
            )
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(rendered, encoding="utf-8")
            captured["prompt_path"] = prompt_path
            captured["rendered_prompt"] = rendered

            return AgentResult(
                exit_code=0 if completion_token == "INVESTIGATOR_COMPLETE" else 1,
                completion_token=completion_token,
                log_path=prompt_path.with_suffix(".log"),
                prompt_path=prompt_path,
                triage_md=triage_md_body,
                started_at=_now(),
                ended_at=_now(),
            )

    return _FakeInvestigator


async def test_reinvestigate_renders_rejection_feedback_into_prompt(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_worktrees: dict[int, dict[str, Path]],
) -> None:
    """The Investigator's rendered prompt for attempt 2 contains the feedback."""
    _ = alembic_db

    # Create a bug and walk it to INVESTIGATING (the /reject handler
    # would have transitioned it there before calling
    # reinvestigate_with_feedback).
    session = engine_module.SessionLocal()
    bug = repo_module.create_bug(
        session,
        notion_page_id="reinv-1",
        project_id="ra_avm",
        title="t",
        description="d",
    )
    import json

    bug.affected_repos = json.dumps(["avm-backend"])
    bug.scope = "single-repo"
    session.flush()
    for nxt in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
    ):
        repo_module.update_bug_state(session, bug, nxt)
    session.commit()
    bug_id = bug.id
    session.close()

    # Pre-seed worktree + a prior investigator.1.md so attempt starts at 2.
    # (The pipeline computes next attempt via list_agent_runs.)
    session = engine_module.SessionLocal()
    repo_module.record_agent_run(
        session,
        bug_id=bug_id,
        agent_role="investigator",
        attempt=1,
        started_at=_now(),
        ended_at=_now(),
        exit_code=0,
        completion_token="INVESTIGATOR_COMPLETE",
        log_path="/tmp/investigator.1.log",
        prompt_path="/tmp/investigator.1.md",
    )
    session.commit()
    session.close()

    captured: dict[str, Any] = {}
    triage_md = (
        "## Root Cause\n\nX\n\n"
        "## Scope\n\n"
        "scope: single-repo\n"
        'affected_repos: ["avm-backend"]\n'
        "deny_list_hit: false\n\n"
        "## Analysis\n\nok.\n\n"
        "## Recommended Fix Approach\n\nguard.\n"
    )
    monkeypatch.setattr(
        pipeline_module,
        "Investigator",
        _make_fake_investigator_capturing(
            captured=captured, triage_md_body=triage_md
        ),
    )

    # Stub Fixer+Tester so the single-repo happy path completes.
    from tests.unit.test_pipeline import (
        _make_fake_agent,
        _patch_tester_safety_clean,
    )

    monkeypatch.setattr(
        pipeline_module,
        "Fixer",
        _make_fake_agent(
            role="fixer",
            completion_token="FIXER_COMPLETE",
            append_section="Fixer Notes",
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "Tester",
        _make_fake_agent(
            role="tester",
            completion_token="TESTER_COMPLETE",
            append_section="Tester Notes",
        ),
    )
    _patch_tester_safety_clean(monkeypatch)

    feedback = (
        "The previous fix regressed autosave. Don't touch that code path; "
        "check the validator instead."
    )
    await pipeline_module.reinvestigate_with_feedback(bug_id, feedback)

    # Attempt 2 (next after the prior attempt 1 we seeded).
    assert captured["attempt"] == 2

    # The rendered prompt file on disk contains the feedback.
    prompt_path = triage_home / "prompts" / str(bug_id) / "investigator.2.md"
    assert prompt_path.exists(), f"Expected prompt at {prompt_path}"
    prompt_contents = prompt_path.read_text(encoding="utf-8")
    assert "PRIOR ATTEMPT REJECTED" in prompt_contents
    assert feedback in prompt_contents

    # The bug progressed to READY_FOR_REVIEW via the single-repo happy path.
    session = engine_module.SessionLocal()
    try:
        final = repo_module.get_bug(session, bug_id)
        assert final is not None
        assert final.state == BugState.READY_FOR_REVIEW.value
    finally:
        session.close()


async def test_reinvestigate_aborts_if_bug_not_in_investigating(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_worktrees: dict[int, dict[str, Path]],
) -> None:
    """Reinvestigate is a no-op if the bug isn't in INVESTIGATING."""
    _ = alembic_db

    session = engine_module.SessionLocal()
    bug = repo_module.create_bug(
        session,
        notion_page_id="reinv-abort",
        project_id="ra_avm",
        title="t",
        description="d",
    )
    session.commit()
    bug_id = bug.id
    session.close()

    # Bug is in OPEN state; reinvestigate should refuse.
    ran: list[Any] = []

    class _Boom:
        role = "investigator"

        async def run(self, *a: Any, **k: Any) -> AgentResult:
            ran.append(1)
            raise AssertionError("Investigator should not run from OPEN state")

    monkeypatch.setattr(pipeline_module, "Investigator", _Boom)
    await pipeline_module.reinvestigate_with_feedback(bug_id, "hello")
    assert ran == []
