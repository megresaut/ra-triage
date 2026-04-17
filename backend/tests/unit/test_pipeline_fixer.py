"""End-to-end pipeline tests for the Agent 6 Fixer + Tester slice.

Strategy: use a *real* git-backed worktree fixture rather than the
synthetic ``tmp_path / worktrees / <id> / <name>`` that
``test_pipeline.py``'s ``stub_worktrees`` fixture produces. That way the
orchestrator's post-Tester safety check, which runs ``git diff
--name-only HEAD`` in the worktree, sees accurate data.

We don't touch ``config.registry`` — we instead rebind
``ra_triage.orchestrator.pipeline.Fixer`` / ``Tester`` / ``Investigator``
and ``workspaces.worktree.create_worktrees_for_bug`` / ``path_for`` with
fake implementations that point at the git-backed temp path.

The Investigator stub appears in every scenario; Fixer and Tester stubs
encode the specific outcomes we're checking.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ra_triage.agents.base import AgentResult
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.orchestrator import pipeline as pipeline_module
from ra_triage.orchestrator.states import BugState

# --- helpers ------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True
    )


def _make_real_worktree(tmp: Path, bug_id: int, repo_name: str) -> Path:
    """Create a tmp git repo and return a worktree path for (bug, repo).

    Structure:
      tmp/canonical/               — bare-ish origin with a ``main`` branch.
      tmp/worktrees/<bug>/<repo>/  — real git worktree on branch triage/<bug>.

    The worktree is created via ``git worktree add``, rooted off the
    canonical clone's ``main``. A baseline commit exists on ``main``
    with a couple of files; the caller usually mutates the worktree
    after this returns.
    """
    canonical = tmp / "canonical"
    canonical.mkdir(parents=True, exist_ok=True)

    _run_git(["init", "-q", "-b", "main"], cwd=canonical)
    _run_git(
        ["config", "user.email", "triage-test@example.com"], cwd=canonical
    )
    _run_git(["config", "user.name", "Triage Test"], cwd=canonical)

    (canonical / "src").mkdir(exist_ok=True)
    (canonical / "src" / "handler.go").write_text(
        "package main\nfunc Handler() {}\n", encoding="utf-8"
    )
    (canonical / "src" / "handler_test.go").write_text(
        "package main\nimport \"testing\"\nfunc TestHandler(t *testing.T) {}\n",
        encoding="utf-8",
    )
    (canonical / "TRIAGE.md").write_text("", encoding="utf-8")
    _run_git(["add", "."], cwd=canonical)
    _run_git(["commit", "-q", "-m", "baseline"], cwd=canonical)

    dest = tmp / "worktrees" / str(bug_id) / repo_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run_git(
        ["worktree", "add", "-q", str(dest), "-b", f"triage/{bug_id}"],
        cwd=canonical,
    )
    return dest


def _make_bug(session: Any, *, notion_page_id: str = "n-fixer-1") -> Any:
    bug = repo_module.create_bug(
        session,
        notion_page_id=notion_page_id,
        project_id="ra_avm",
        title="Handler returns nil",
        description="500 on missing user",
        repro_steps="POST /users/?id=nope",
        severity="med",
        reporter="qa",
    )
    session.commit()
    return bug


def _investigator_stub(triage_md_body: str) -> Any:
    class _StubInv:
        role = "investigator"

        async def run(
            self, bug: Any, worktree: Path, context: dict[str, Any]
        ) -> AgentResult:
            (worktree / "TRIAGE.md").write_text(
                triage_md_body, encoding="utf-8"
            )
            now = _now()
            attempt = context["attempt"]
            from ra_triage.settings import get_settings

            settings = get_settings()
            log_path = (
                settings.logs_dir
                / str(bug.id)
                / f"investigator.{attempt}.log"
            )
            prompt_path = (
                settings.prompts_dir
                / str(bug.id)
                / f"investigator.{attempt}.md"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("stub\n", encoding="utf-8")
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text("stub\n", encoding="utf-8")
            return AgentResult(
                exit_code=0,
                completion_token="INVESTIGATOR_COMPLETE",
                log_path=log_path,
                prompt_path=prompt_path,
                triage_md=triage_md_body,
                started_at=now,
                ended_at=now,
            )

    return _StubInv


def _agent_stub(
    *,
    role: str,
    completion_token: str | None,
    edit_worktree: Any | None = None,
) -> Any:
    """Generic Fixer/Tester stub.

    ``edit_worktree`` (optional) is a callable ``(worktree: Path) -> None``
    that mutates the worktree before the stub returns — used to simulate
    the real Fixer writing source edits or the Tester creating a test
    (and optionally a bad prod-file edit).
    """
    from ra_triage.triage_md import parser as _parser

    class _StubAgent:
        role = ""

        async def run(
            self, bug: Any, worktree: Path, context: dict[str, Any]
        ) -> AgentResult:
            if edit_worktree is not None:
                edit_worktree(worktree)
            now = _now()
            attempt = context["attempt"]
            from ra_triage.settings import get_settings

            settings = get_settings()
            log_path = (
                settings.logs_dir / str(bug.id) / f"{role}.{attempt}.log"
            )
            prompt_path = (
                settings.prompts_dir / str(bug.id) / f"{role}.{attempt}.md"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("stub\n", encoding="utf-8")
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text("stub\n", encoding="utf-8")
            # TRIAGE.md snapshot after optional edits.
            triage_path = worktree / "TRIAGE.md"
            triage_md = (
                triage_path.read_text(encoding="utf-8")
                if triage_path.exists()
                else ""
            )
            return AgentResult(
                exit_code=0
                if completion_token and completion_token.endswith("_COMPLETE")
                else 1,
                completion_token=completion_token,
                log_path=log_path,
                prompt_path=prompt_path,
                triage_md=triage_md,
                started_at=now,
                ended_at=now,
            )

    _StubAgent.role = role

    # Expose the parser so callers can compose edit_worktree that uses it.
    _StubAgent._parser = _parser  # type: ignore[attr-defined]
    return _StubAgent


def _fix_edit(body_extra: str = "- Files changed: src/handler.go\n- Decisions: guarded nil\n"):
    def _edit(worktree: Path) -> None:
        from ra_triage.triage_md import parser as _parser

        # Mutate a prod file (the real Fixer WOULD do this).
        f = worktree / "src" / "handler.go"
        f.write_text(
            "package main\nfunc Handler() { /* fixed */ }\n", encoding="utf-8"
        )
        _parser.append(worktree / "TRIAGE.md", "Fixer Notes", body_extra)

    return _edit


def _tester_test_only_edit():
    def _edit(worktree: Path) -> None:
        from ra_triage.triage_md import parser as _parser

        # Add a regression test (test-file only).
        (worktree / "src" / "handler_test.go").write_text(
            "package main\nimport \"testing\"\nfunc TestHandler(t *testing.T) {}\n"
            "func TestRegression(t *testing.T) {}\n",
            encoding="utf-8",
        )
        _parser.append(
            worktree / "TRIAGE.md",
            "Tester Notes",
            "- Tests added (file:path): src/handler_test.go\n- Suite: 2 passed\n",
        )

    return _edit


def _tester_writes_prod_edit():
    def _edit(worktree: Path) -> None:
        from ra_triage.triage_md import parser as _parser

        # Add a test file (fine).
        (worktree / "src" / "handler_test.go").write_text(
            "package main\nimport \"testing\"\nfunc TestHandler(t *testing.T) {}\n"
            "func TestRegression(t *testing.T) {}\n",
            encoding="utf-8",
        )
        # And ALSO mutate a prod file (not fine — orchestrator reverts).
        (worktree / "src" / "handler.go").write_text(
            "package main\nfunc Handler() { /* tester snuck this in */ }\n",
            encoding="utf-8",
        )
        _parser.append(
            worktree / "TRIAGE.md",
            "Tester Notes",
            "- Tests added + prod mutation (oops)\n",
        )

    return _edit


@pytest.fixture()
def real_worktree_monkeypatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Stub ``workspaces.worktree`` to return a real git-backed path.

    Returns the worktree path for bug_id=42 repo=avm-backend. The
    stubs cover the subset the Agent 6 tests touch:
      - ``create_worktrees_for_bug`` — one-entry mapping.
      - ``path_for`` — resolves (bug_id, repo_name) to the same path.
      - ``cleanup`` — no-op.
    """
    from ra_triage.workspaces import worktree as wt_module

    created: dict[int, dict[str, Path]] = {}

    def _path_for(bug_id: int, repo_name: str) -> Path:
        # Always the same path for the test setup; if a test wants
        # distinct repos it can create them explicitly.
        return tmp_path / "worktrees" / str(bug_id) / repo_name

    async def _create(bug_id: int, repos: list[Any]) -> dict[str, Path]:
        mapping: dict[str, Path] = {}
        for r in repos:
            # Only build a real worktree for the repo that matches the
            # bug's affected_repos (saves time + avoids pg_restore calls).
            if r.name == "avm-backend":
                dest = _make_real_worktree(tmp_path, bug_id, r.name)
            else:
                # Synthetic placeholder for the frontend repo — not
                # exercised by Agent 6 tests.
                dest = tmp_path / "worktrees" / str(bug_id) / r.name
                dest.mkdir(parents=True, exist_ok=True)
            mapping[r.name] = dest
        created[bug_id] = mapping
        return mapping

    async def _cleanup(bug_id: int, repos: list[Any]) -> None:
        created.pop(bug_id, None)

    monkeypatch.setattr(wt_module, "create_worktrees_for_bug", _create)
    monkeypatch.setattr(wt_module, "cleanup", _cleanup)
    monkeypatch.setattr(wt_module, "path_for", _path_for)
    # Also stub the orchestrator-local alias.
    monkeypatch.setattr(pipeline_module.wt_module, "path_for", _path_for)

    return tmp_path


# --- scenarios ----------------------------------------------------------


_TRIAGE_SINGLE_REPO = (
    "## Root Cause\n\nsrc/handler.go:1 — missing nil check\n\n"
    "## Scope\n\n"
    "scope: single-repo\n"
    'affected_repos: ["avm-backend"]\n'
    "deny_list_hit: false\n\n"
    "## Analysis\n\nHandler returns on nil user.\n\n"
    "## Recommended Fix Approach\n\nGuard the lookup; return 404.\n"
)


async def test_happy_path_lands_in_ready_for_review(
    alembic_db: Path,
    triage_home: Path,
    real_worktree_monkeypatch: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Investigator COMPLETE + Fixer COMPLETE + Tester COMPLETE → READY_FOR_REVIEW."""
    monkeypatch.setattr(
        pipeline_module,
        "Investigator",
        _investigator_stub(_TRIAGE_SINGLE_REPO),
    )
    monkeypatch.setattr(
        pipeline_module,
        "Fixer",
        _agent_stub(
            role="fixer",
            completion_token="FIXER_COMPLETE",
            edit_worktree=_fix_edit(),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "Tester",
        _agent_stub(
            role="tester",
            completion_token="TESTER_COMPLETE",
            edit_worktree=_tester_test_only_edit(),
        ),
    )

    session = engine_module.SessionLocal()
    bug = _make_bug(session, notion_page_id="happy-1")
    bug_id = bug.id
    session.close()

    await pipeline_module.run_pipeline(bug_id)

    session = engine_module.SessionLocal()
    try:
        got = repo_module.get_bug(session, bug_id)
        assert got is not None
        assert got.state == BugState.READY_FOR_REVIEW.value

        runs = repo_module.list_agent_runs(session, bug_id)
        roles = [r.agent_role for r in runs]
        assert roles == ["investigator", "fixer", "tester"]

        # TRIAGE.md ends up with both Fixer and Tester sections.
        worktree = real_worktree_monkeypatch / "worktrees" / str(bug_id) / "avm-backend"
        triage = (worktree / "TRIAGE.md").read_text(encoding="utf-8")
        assert "## Fixer Notes" in triage
        assert "## Tester Notes" in triage
    finally:
        session.close()


async def test_fixer_failed_lands_in_blocked(
    alembic_db: Path,
    triage_home: Path,
    real_worktree_monkeypatch: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FIXER_FAILED → BLOCKED with ## Fixer Failure visible."""
    from ra_triage.triage_md import parser as _parser

    def _fixer_failure_edit(worktree: Path) -> None:
        _parser.append(
            worktree / "TRIAGE.md",
            "Fixer Failure",
            "- Diagnosis: could not locate function\n",
        )

    monkeypatch.setattr(
        pipeline_module,
        "Investigator",
        _investigator_stub(_TRIAGE_SINGLE_REPO),
    )
    monkeypatch.setattr(
        pipeline_module,
        "Fixer",
        _agent_stub(
            role="fixer",
            completion_token="FIXER_FAILED",
            edit_worktree=_fixer_failure_edit,
        ),
    )
    # Tester should NOT be reached; if it is, the test will fail because
    # we don't mock a safety check and the worktree will have no diff.
    monkeypatch.setattr(
        pipeline_module,
        "Tester",
        _agent_stub(role="tester", completion_token="TESTER_COMPLETE"),
    )

    session = engine_module.SessionLocal()
    bug = _make_bug(session, notion_page_id="fail-fixer-1")
    bug_id = bug.id
    session.close()

    await pipeline_module.run_pipeline(bug_id)

    session = engine_module.SessionLocal()
    try:
        got = repo_module.get_bug(session, bug_id)
        assert got is not None
        assert got.state == BugState.BLOCKED.value

        runs = repo_module.list_agent_runs(session, bug_id)
        roles = [r.agent_role for r in runs]
        # Investigator + fixer only — Tester should NOT have been spawned.
        assert roles == ["investigator", "fixer"]

        worktree = (
            real_worktree_monkeypatch
            / "worktrees"
            / str(bug_id)
            / "avm-backend"
        )
        triage = (worktree / "TRIAGE.md").read_text(encoding="utf-8")
        assert "## Fixer Failure" in triage
    finally:
        session.close()


async def test_fixer_escalation_lands_in_blocked(
    alembic_db: Path,
    triage_home: Path,
    real_worktree_monkeypatch: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """## Fixer Escalation section present → BLOCKED even without FIXER_FAILED."""
    from ra_triage.triage_md import parser as _parser

    def _fixer_esc_edit(worktree: Path) -> None:
        _parser.append(
            worktree / "TRIAGE.md",
            "Fixer Escalation",
            "- Fix requires modifying a deny-list path.\n",
        )

    monkeypatch.setattr(
        pipeline_module,
        "Investigator",
        _investigator_stub(_TRIAGE_SINGLE_REPO),
    )
    # Fixer emits FIXER_COMPLETE but also writes the escalation — the
    # pipeline should still route to BLOCKED.
    monkeypatch.setattr(
        pipeline_module,
        "Fixer",
        _agent_stub(
            role="fixer",
            completion_token="FIXER_COMPLETE",
            edit_worktree=_fixer_esc_edit,
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "Tester",
        _agent_stub(role="tester", completion_token="TESTER_COMPLETE"),
    )

    session = engine_module.SessionLocal()
    bug = _make_bug(session, notion_page_id="esc-fixer-1")
    bug_id = bug.id
    session.close()

    await pipeline_module.run_pipeline(bug_id)

    session = engine_module.SessionLocal()
    try:
        got = repo_module.get_bug(session, bug_id)
        assert got is not None
        assert got.state == BugState.BLOCKED.value
        runs = repo_module.list_agent_runs(session, bug_id)
        roles = [r.agent_role for r in runs]
        assert roles == ["investigator", "fixer"]
    finally:
        session.close()


async def test_tester_failed_lands_in_blocked(
    alembic_db: Path,
    triage_home: Path,
    real_worktree_monkeypatch: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TESTER_FAILED → BLOCKED with ## Tester Failure visible."""
    from ra_triage.triage_md import parser as _parser

    def _tester_fail_edit(worktree: Path) -> None:
        _parser.append(
            worktree / "TRIAGE.md",
            "Tester Failure",
            "- Fix returns 500 instead of 404.\n",
        )

    monkeypatch.setattr(
        pipeline_module,
        "Investigator",
        _investigator_stub(_TRIAGE_SINGLE_REPO),
    )
    monkeypatch.setattr(
        pipeline_module,
        "Fixer",
        _agent_stub(
            role="fixer",
            completion_token="FIXER_COMPLETE",
            edit_worktree=_fix_edit(),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "Tester",
        _agent_stub(
            role="tester",
            completion_token="TESTER_FAILED",
            edit_worktree=_tester_fail_edit,
        ),
    )

    session = engine_module.SessionLocal()
    bug = _make_bug(session, notion_page_id="fail-tester-1")
    bug_id = bug.id
    session.close()

    await pipeline_module.run_pipeline(bug_id)

    session = engine_module.SessionLocal()
    try:
        got = repo_module.get_bug(session, bug_id)
        assert got is not None
        assert got.state == BugState.BLOCKED.value

        worktree = (
            real_worktree_monkeypatch
            / "worktrees"
            / str(bug_id)
            / "avm-backend"
        )
        triage = (worktree / "TRIAGE.md").read_text(encoding="utf-8")
        assert "## Tester Failure" in triage
    finally:
        session.close()


async def test_tester_writes_different_prod_file_preserves_fixer_work(
    alembic_db: Path,
    triage_home: Path,
    real_worktree_monkeypatch: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixer edits src/handler.go; Tester edits a DIFFERENT prod file.

    Expected: Tester's edit is reverted, Fixer's survives (baseline
    excludes handler.go from the safety check because its hash hasn't
    changed between Fixer-done and Tester-done).
    """
    from ra_triage.triage_md import parser as _parser

    def _tester_diff_prod(worktree: Path) -> None:
        # Test file (fine).
        (worktree / "src" / "handler_test.go").write_text(
            "package main\nimport \"testing\"\nfunc TestHandler(t *testing.T) {}\n"
            "func TestRegression(t *testing.T) {}\n",
            encoding="utf-8",
        )
        # A DIFFERENT prod file (not the one Fixer touched).
        (worktree / "src" / "config.go").write_text(
            "package main\n// Tester wrote this\n", encoding="utf-8"
        )
        _parser.append(
            worktree / "TRIAGE.md",
            "Tester Notes",
            "- Extra file created: src/config.go (should be reverted)\n",
        )

    monkeypatch.setattr(
        pipeline_module,
        "Investigator",
        _investigator_stub(_TRIAGE_SINGLE_REPO),
    )
    monkeypatch.setattr(
        pipeline_module,
        "Fixer",
        _agent_stub(
            role="fixer",
            completion_token="FIXER_COMPLETE",
            edit_worktree=_fix_edit(),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "Tester",
        _agent_stub(
            role="tester",
            completion_token="TESTER_COMPLETE",
            edit_worktree=_tester_diff_prod,
        ),
    )

    session = engine_module.SessionLocal()
    bug = _make_bug(session, notion_page_id="prod-disjoint-1")
    bug_id = bug.id
    session.close()

    await pipeline_module.run_pipeline(bug_id)

    session = engine_module.SessionLocal()
    try:
        got = repo_module.get_bug(session, bug_id)
        assert got is not None
        assert got.state == BugState.BLOCKED.value
    finally:
        session.close()

    worktree = (
        real_worktree_monkeypatch
        / "worktrees"
        / str(bug_id)
        / "avm-backend"
    )
    # Fixer's handler.go change SURVIVES — its hash is in the baseline
    # and the Tester didn't touch it.
    handler = (worktree / "src" / "handler.go").read_text(encoding="utf-8")
    assert "/* fixed */" in handler, (
        "Fixer's work on handler.go should not be reverted"
    )
    # Tester's config.go was reverted (the new file was deleted).
    assert not (worktree / "src" / "config.go").exists(), (
        "Tester-created prod file should be deleted"
    )
    # Test file survives.
    assert (worktree / "src" / "handler_test.go").exists()


async def test_tester_writes_prod_code_is_reverted_and_blocked(
    alembic_db: Path,
    triage_home: Path,
    real_worktree_monkeypatch: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tester mutates prod file → safety revert + BLOCKED + test additions kept."""
    monkeypatch.setattr(
        pipeline_module,
        "Investigator",
        _investigator_stub(_TRIAGE_SINGLE_REPO),
    )
    monkeypatch.setattr(
        pipeline_module,
        "Fixer",
        _agent_stub(
            role="fixer",
            completion_token="FIXER_COMPLETE",
            edit_worktree=_fix_edit(),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "Tester",
        _agent_stub(
            role="tester",
            completion_token="TESTER_COMPLETE",
            edit_worktree=_tester_writes_prod_edit(),
        ),
    )

    session = engine_module.SessionLocal()
    bug = _make_bug(session, notion_page_id="prod-tester-1")
    bug_id = bug.id
    session.close()

    await pipeline_module.run_pipeline(bug_id)

    session = engine_module.SessionLocal()
    try:
        got = repo_module.get_bug(session, bug_id)
        assert got is not None
        assert got.state == BugState.BLOCKED.value
    finally:
        session.close()

    worktree = (
        real_worktree_monkeypatch
        / "worktrees"
        / str(bug_id)
        / "avm-backend"
    )
    # Prod file reverted — the "tester snuck this in" string must be gone.
    # In this specific scenario the Tester and Fixer BOTH modified
    # handler.go, so the safety check can only revert to HEAD (Fixer's
    # pre-Tester content is lost too). This is the documented
    # conservative trade-off: better BLOCKED + lost Fixer work than a
    # merged PR containing Tester-written prod code.
    handler = (worktree / "src" / "handler.go").read_text(encoding="utf-8")
    assert "tester snuck this in" not in handler

    # Test file is preserved.
    test_file = worktree / "src" / "handler_test.go"
    assert test_file.exists()
    assert "TestRegression" in test_file.read_text(encoding="utf-8")

    # TRIAGE.md has a safety-violation section (kept — TRIAGE.md is
    # always allow-listed).
    triage = (worktree / "TRIAGE.md").read_text(encoding="utf-8")
    assert "## Tester Safety Violation" in triage
    assert "src/handler.go" in triage
