"""End-to-end cross-repo pipeline tests (Agent 10).

Mocks all four cross-repo agents (FixDesigner, FixerFrontend,
FixerBackend, IntegrationTester) plus the Investigator at the
``pipeline`` module's class-attribute level (the same monkeypatch
strategy the single-repo Agent 6 tests already use). Exercises:

  - Happy path → READY_FOR_REVIEW with overlapping fixer start times.
  - Contract concern from one Fixer (first cycle) → DESIGNING again →
    success → RFR.
  - Contract concern on second cycle → BLOCKED.
  - Integration failure (first cycle) → DESIGNING again → success.
  - Fixer-Frontend FAILED → BLOCKED.
  - Post-INTEGRATION pkill is invoked for both integration ports
    regardless of outcome.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ra_triage.agents.base import AgentResult
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.orchestrator import pipeline as pipeline_module
from ra_triage.orchestrator.states import BugState
from ra_triage.triage_md import parser as triage_parser

# --- helpers --------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _make_cross_repo_bug(session: Any, *, notion_page_id: str) -> Any:
    bug = repo_module.create_bug(
        session,
        notion_page_id=notion_page_id,
        project_id="ra_avm",
        title="Cross-repo bug",
        description="d",
        repro_steps="r",
        severity="med",
        reporter="qa",
    )
    session.commit()
    return bug


_TRIAGE_MD_CROSS_REPO = (
    "## Root Cause\n\napi/page.go:10 + ui/page.tsx:20\n\n"
    "## Scope\n\n"
    "scope: cross-repo\n"
    'affected_repos: ["avm-backend", "builder-frontend"]\n'
    "deny_list_hit: false\n\n"
    "## Analysis\n\ncontract drift.\n\n"
    "## Recommended Fix Approach\n\nalign both sides.\n"
)


@pytest.fixture()
def stub_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """Stub workspaces.worktree to return synthetic dirs under tmp_path."""
    from ra_triage.workspaces import worktree as wt_module

    per_bug: dict[int, dict[str, Path]] = {}

    async def _fake_create(bug_id: int, repos: list[Any]) -> dict[str, Path]:
        mapping: dict[str, Path] = {}
        for r in repos:
            dest = tmp_path / "worktrees" / str(bug_id) / r.name
            dest.mkdir(parents=True, exist_ok=True)
            mapping[r.name] = dest
        per_bug[bug_id] = mapping
        return mapping

    async def _fake_cleanup(bug_id: int, repos: list[Any]) -> None:
        per_bug.pop(bug_id, None)

    monkeypatch.setattr(wt_module, "create_worktrees_for_bug", _fake_create)
    monkeypatch.setattr(wt_module, "cleanup", _fake_cleanup)
    return {"tmp": tmp_path}


def _stub_investigator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the Investigator so it routes the bug to cross-repo scope."""

    class _StubInvestigator:
        role = "investigator"

        async def run(
            self, bug: Any, worktree: Path, context: dict[str, Any]
        ) -> AgentResult:
            (worktree / "TRIAGE.md").write_text(
                _TRIAGE_MD_CROSS_REPO, encoding="utf-8"
            )
            now = _now()
            attempt = context["attempt"]
            from ra_triage.settings import get_settings

            s = get_settings()
            log_path = (
                s.logs_dir / str(bug.id) / f"investigator.{attempt}.log"
            )
            prompt_path = (
                s.prompts_dir / str(bug.id) / f"investigator.{attempt}.md"
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
                triage_md=_TRIAGE_MD_CROSS_REPO,
                started_at=now,
                ended_at=now,
            )

    monkeypatch.setattr(pipeline_module, "Investigator", _StubInvestigator)


def _make_cross_repo_agent_stub(
    *,
    role: str,
    completion_tokens: list[str | None],
    edit_worktree: Callable[[Path], None] | None = None,
) -> Any:
    """Generic cross-repo agent stub that returns a different completion
    token on each call (poll() consumes the list).

    ``edit_worktree`` is invoked once per call. The callable receives
    the path of the canonical TRIAGE.md DIRECTORY (i.e. the backend
    worktree where the shared TRIAGE.md lives) — NOT the agent's own
    cwd. We resolve this from ``context["triage_md_path"]`` if present
    (FE/BE fixers + integration tester include it), otherwise fall back
    to the agent's ``worktree`` arg (Fix Designer runs from the backend
    worktree directly so worktree IS the canonical dir).

    This mirrors how the real agents behave per the prompt templates:
    every cross-repo agent appends to the SAME shared TRIAGE.md that
    lives in the backend worktree, even when the agent's own cwd is
    elsewhere (e.g. Fixer-Frontend's cwd is the FE worktree but it
    appends to {{ triage_md_path }} which points at the BE worktree).
    """
    call_count = {"n": 0}

    class _StubAgent:
        role = ""

        async def run(
            self, bug: Any, worktree: Path, context: dict[str, Any]
        ) -> AgentResult:
            n = call_count["n"]
            call_count["n"] += 1
            triage_md_str = context.get("triage_md_path")
            if triage_md_str:
                # Append to the shared TRIAGE.md's parent dir (so
                # _appender's "worktree / 'TRIAGE.md'" still resolves).
                edit_target = Path(triage_md_str).parent
            else:
                edit_target = worktree
            if edit_worktree is not None:
                edit_worktree(edit_target)
            tok = (
                completion_tokens[n]
                if n < len(completion_tokens)
                else completion_tokens[-1]
            )
            now = _now()
            attempt = context["attempt"]
            from ra_triage.settings import get_settings

            s = get_settings()
            log_path = s.logs_dir / str(bug.id) / f"{role}.{attempt}.log"
            prompt_path = s.prompts_dir / str(bug.id) / f"{role}.{attempt}.md"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("stub\n", encoding="utf-8")
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text("stub\n", encoding="utf-8")
            triage_path = worktree / "TRIAGE.md"
            triage_md = (
                triage_path.read_text(encoding="utf-8")
                if triage_path.exists()
                else ""
            )
            return AgentResult(
                exit_code=0 if tok and tok.endswith("_COMPLETE") else 1,
                completion_token=tok,
                log_path=log_path,
                prompt_path=prompt_path,
                triage_md=triage_md,
                started_at=now,
                ended_at=now,
            )

    _StubAgent.role = role
    return _StubAgent


def _designer_appends_contract(append_body: str | None = None) -> Callable[[Path], None]:
    """Edit closure that appends a Cross-Repo Contract section."""
    body = append_body or (
        "### Shared Data Shape\nuser: { id, name }\n\n"
        "### Backend Responsibilities\nGET /users/:id\n\n"
        "### Frontend Responsibilities\nrender card on 200\n\n"
        "### Shared Assumptions\nbackward-compat\n\n"
        "### Integration Test Scenario\n1. POST 2. GET 3. assert\n"
    )

    def _edit(worktree: Path) -> None:
        triage_parser.append(worktree / "TRIAGE.md", "Cross-Repo Contract", body)

    return _edit


def _appender(section: str, body: str) -> Callable[[Path], None]:
    """Generic 'append this section' closure."""

    def _edit(worktree: Path) -> None:
        triage_parser.append(worktree / "TRIAGE.md", section, body)

    return _edit


def _multi_appender(*pairs: tuple[str, str]) -> Callable[[Path], None]:
    """Append multiple sections in one stub call (e.g. Notes + Concern)."""

    def _edit(worktree: Path) -> None:
        for section, body in pairs:
            triage_parser.append(worktree / "TRIAGE.md", section, body)

    return _edit


def _stub_pkill(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace pipeline._pkill_integration_ports's subprocess call.

    Returns a list that the test reads: each tuple is the (executable,
    pattern) pair the orchestrator would have invoked. We replace the
    underlying function (NOT the subprocess module) so the test stays
    decoupled from asyncio's internals.
    """
    invocations: list[tuple[str, str]] = []

    async def _fake_pkill(*, be_port: int, fe_port: int) -> None:
        invocations.append(("pkill -f", f"port {be_port}"))
        invocations.append(("pkill -f", f"port {fe_port}"))

    monkeypatch.setattr(
        pipeline_module, "_pkill_integration_ports", _fake_pkill
    )
    return invocations


@pytest.fixture(autouse=True)
def _clear_designer_concerns_holder() -> Iterator[None]:
    """Per-test: ensure the module-level designer_concerns cache starts empty."""
    pipeline_module._designer_concerns_clear()
    yield
    pipeline_module._designer_concerns_clear()


# --- happy path -----------------------------------------------------------


async def test_cross_repo_happy_path_lands_in_ready_for_review(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_worktrees: dict[str, Path],
) -> None:
    """All four agents complete cleanly → READY_FOR_REVIEW."""
    _stub_investigator(monkeypatch)
    monkeypatch.setattr(
        pipeline_module,
        "FixDesigner",
        _make_cross_repo_agent_stub(
            role="fix_designer",
            completion_tokens=["FIX_DESIGNER_COMPLETE"],
            edit_worktree=_designer_appends_contract(),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "FixerFrontend",
        _make_cross_repo_agent_stub(
            role="fixer_frontend",
            completion_tokens=["FIXER_FRONTEND_COMPLETE"],
            edit_worktree=_appender(
                "Fixer-Frontend Notes",
                "- Files changed: src/UserCard.tsx\n",
            ),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "FixerBackend",
        _make_cross_repo_agent_stub(
            role="fixer_backend",
            completion_tokens=["FIXER_BACKEND_COMPLETE"],
            edit_worktree=_appender(
                "Fixer-Backend Notes",
                "- Files changed: api/handlers/user.go\n",
            ),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "IntegrationTester",
        _make_cross_repo_agent_stub(
            role="integration_tester",
            completion_tokens=["INTEGRATION_TESTER_COMPLETE"],
            edit_worktree=_appender(
                "Integration Tester Notes",
                "- Scenario result: passed\n",
            ),
        ),
    )
    pkill_invocations = _stub_pkill(monkeypatch)

    session = engine_module.SessionLocal()
    bug = _make_cross_repo_bug(session, notion_page_id="happy-cross-1")
    bug_id = bug.id
    session.close()

    await pipeline_module.run_pipeline(bug_id)

    session = engine_module.SessionLocal()
    try:
        got = repo_module.get_bug(session, bug_id)
        assert got is not None
        assert got.state == BugState.READY_FOR_REVIEW.value
        assert got.scope == "cross-repo"

        runs = repo_module.list_agent_runs(session, bug_id)
        roles = [r.agent_role for r in runs]
        assert roles == [
            "investigator",
            "fix_designer",
            "fixer_frontend",
            "fixer_backend",
            "integration_tester",
        ]

        # Both fixer start times must be within 1 second — they were
        # spawned via asyncio.gather so they overlap.
        fe_run = next(r for r in runs if r.agent_role == "fixer_frontend")
        be_run = next(r for r in runs if r.agent_role == "fixer_backend")
        delta = abs((fe_run.started_at - be_run.started_at).total_seconds())
        assert delta < 1.0, (
            f"fixer_frontend started_at and fixer_backend started_at "
            f"differ by {delta:.3f}s — expected concurrent spawn"
        )
    finally:
        session.close()

    # TRIAGE.md should have all the cross-repo sections.
    backend_worktree = stub_worktrees["tmp"] / "worktrees" / str(bug_id) / "avm-backend"
    triage_md = (backend_worktree / "TRIAGE.md").read_text(encoding="utf-8")
    assert "## Cross-Repo Contract" in triage_md
    assert "## Fixer-Frontend Notes" in triage_md
    assert "## Fixer-Backend Notes" in triage_md
    assert "## Integration Tester Notes" in triage_md

    # pkill was invoked for BOTH integration ports (28080 backend, 25173 fe).
    assert ("pkill -f", "port 28080") in pkill_invocations
    assert ("pkill -f", "port 25173") in pkill_invocations


# --- contract concern → re-design → success -------------------------------


async def test_contract_concern_first_cycle_loops_back_to_design(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_worktrees: dict[str, Path],
) -> None:
    """FE Fixer raises a Contract Concern on cycle 1 → DESIGNING re-runs → success → RFR."""
    _stub_investigator(monkeypatch)

    # Designer runs twice. Both pass.
    monkeypatch.setattr(
        pipeline_module,
        "FixDesigner",
        _make_cross_repo_agent_stub(
            role="fix_designer",
            completion_tokens=["FIX_DESIGNER_COMPLETE", "FIX_DESIGNER_COMPLETE"],
            edit_worktree=_designer_appends_contract(),
        ),
    )

    # FE Fixer raises a Contract Concern on first call, succeeds clean on
    # second. We use a stateful closure to differentiate.
    fe_calls = {"n": 0}

    def _fe_edit(worktree: Path) -> None:
        n = fe_calls["n"]
        fe_calls["n"] += 1
        if n == 0:
            triage_parser.append(
                worktree / "TRIAGE.md",
                "Fixer-Frontend Notes",
                "- Files changed: (none — concern raised)\n",
            )
            triage_parser.append(
                worktree / "TRIAGE.md",
                "Fixer-Frontend Contract Concern",
                "- email field nullability mismatch\n",
            )
        else:
            triage_parser.append(
                worktree / "TRIAGE.md",
                "Fixer-Frontend Notes",
                "- Files changed: src/UserCard.tsx (clean)\n",
            )

    monkeypatch.setattr(
        pipeline_module,
        "FixerFrontend",
        _make_cross_repo_agent_stub(
            role="fixer_frontend",
            completion_tokens=[
                "FIXER_FRONTEND_COMPLETE",
                "FIXER_FRONTEND_COMPLETE",
            ],
            edit_worktree=_fe_edit,
        ),
    )

    monkeypatch.setattr(
        pipeline_module,
        "FixerBackend",
        _make_cross_repo_agent_stub(
            role="fixer_backend",
            completion_tokens=["FIXER_BACKEND_COMPLETE", "FIXER_BACKEND_COMPLETE"],
            edit_worktree=_appender(
                "Fixer-Backend Notes",
                "- Files changed: api/handlers/user.go\n",
            ),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "IntegrationTester",
        _make_cross_repo_agent_stub(
            role="integration_tester",
            completion_tokens=["INTEGRATION_TESTER_COMPLETE"],
            edit_worktree=_appender(
                "Integration Tester Notes",
                "- Scenario result: passed\n",
            ),
        ),
    )
    _stub_pkill(monkeypatch)

    session = engine_module.SessionLocal()
    bug = _make_cross_repo_bug(session, notion_page_id="concern-cross-1")
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
        # 2 designers + 2 of each fixer + 1 integration tester
        assert roles.count("fix_designer") == 2
        assert roles.count("fixer_frontend") == 2
        assert roles.count("fixer_backend") == 2
        assert roles.count("integration_tester") == 1
    finally:
        session.close()


# --- contract concern on cycle 2 → BLOCKED --------------------------------


async def test_contract_concern_second_cycle_lands_in_blocked(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_worktrees: dict[str, Path],
) -> None:
    """FE Fixer raises Contract Concern on BOTH cycles → BLOCKED."""
    _stub_investigator(monkeypatch)
    monkeypatch.setattr(
        pipeline_module,
        "FixDesigner",
        _make_cross_repo_agent_stub(
            role="fix_designer",
            completion_tokens=["FIX_DESIGNER_COMPLETE", "FIX_DESIGNER_COMPLETE"],
            edit_worktree=_designer_appends_contract(),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "FixerFrontend",
        _make_cross_repo_agent_stub(
            role="fixer_frontend",
            completion_tokens=[
                "FIXER_FRONTEND_COMPLETE",
                "FIXER_FRONTEND_COMPLETE",
            ],
            edit_worktree=_multi_appender(
                ("Fixer-Frontend Notes", "- (concern)\n"),
                (
                    "Fixer-Frontend Contract Concern",
                    "- still mismatched\n",
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "FixerBackend",
        _make_cross_repo_agent_stub(
            role="fixer_backend",
            completion_tokens=["FIXER_BACKEND_COMPLETE", "FIXER_BACKEND_COMPLETE"],
            edit_worktree=_appender("Fixer-Backend Notes", "- ok\n"),
        ),
    )
    # Integration tester should NEVER be called on this path.
    monkeypatch.setattr(
        pipeline_module,
        "IntegrationTester",
        _make_cross_repo_agent_stub(
            role="integration_tester",
            completion_tokens=["INTEGRATION_TESTER_COMPLETE"],
        ),
    )
    _stub_pkill(monkeypatch)

    session = engine_module.SessionLocal()
    bug = _make_cross_repo_bug(session, notion_page_id="concern-cap-1")
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
        assert roles.count("fix_designer") == 2
        assert roles.count("integration_tester") == 0
    finally:
        session.close()


# --- integration failure first cycle → DESIGNING again --------------------


async def test_integration_failure_first_cycle_loops_back(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_worktrees: dict[str, Path],
) -> None:
    """Integration tester FAILED on cycle 1 → DESIGNING re-runs → success → RFR."""
    _stub_investigator(monkeypatch)
    monkeypatch.setattr(
        pipeline_module,
        "FixDesigner",
        _make_cross_repo_agent_stub(
            role="fix_designer",
            completion_tokens=["FIX_DESIGNER_COMPLETE", "FIX_DESIGNER_COMPLETE"],
            edit_worktree=_designer_appends_contract(),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "FixerFrontend",
        _make_cross_repo_agent_stub(
            role="fixer_frontend",
            completion_tokens=[
                "FIXER_FRONTEND_COMPLETE",
                "FIXER_FRONTEND_COMPLETE",
            ],
            edit_worktree=_appender("Fixer-Frontend Notes", "- ok\n"),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "FixerBackend",
        _make_cross_repo_agent_stub(
            role="fixer_backend",
            completion_tokens=["FIXER_BACKEND_COMPLETE", "FIXER_BACKEND_COMPLETE"],
            edit_worktree=_appender("Fixer-Backend Notes", "- ok\n"),
        ),
    )

    # Integration tester FAILED on first call, COMPLETE on second.
    int_calls = {"n": 0}

    def _int_edit(worktree: Path) -> None:
        n = int_calls["n"]
        int_calls["n"] += 1
        if n == 0:
            triage_parser.append(
                worktree / "TRIAGE.md",
                "Integration Tester Failure",
                "- 500 from BE\n",
            )
        else:
            triage_parser.append(
                worktree / "TRIAGE.md",
                "Integration Tester Notes",
                "- passed after re-design\n",
            )

    monkeypatch.setattr(
        pipeline_module,
        "IntegrationTester",
        _make_cross_repo_agent_stub(
            role="integration_tester",
            completion_tokens=[
                "INTEGRATION_TESTER_FAILED",
                "INTEGRATION_TESTER_COMPLETE",
            ],
            edit_worktree=_int_edit,
        ),
    )
    pkill_invocations = _stub_pkill(monkeypatch)

    session = engine_module.SessionLocal()
    bug = _make_cross_repo_bug(session, notion_page_id="int-loop-1")
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
        assert roles.count("integration_tester") == 2
        assert roles.count("fix_designer") == 2
    finally:
        session.close()

    # pkill should fire once per Integration Tester run × 2 ports = 4 entries.
    assert pkill_invocations.count(("pkill -f", "port 28080")) == 2
    assert pkill_invocations.count(("pkill -f", "port 25173")) == 2


# --- Fixer-FE FAILED → BLOCKED --------------------------------------------


async def test_fixer_frontend_failed_lands_in_blocked(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_worktrees: dict[str, Path],
) -> None:
    """Fixer-Frontend FAILED → BLOCKED with Fixer-Frontend Failure section."""
    _stub_investigator(monkeypatch)
    monkeypatch.setattr(
        pipeline_module,
        "FixDesigner",
        _make_cross_repo_agent_stub(
            role="fix_designer",
            completion_tokens=["FIX_DESIGNER_COMPLETE"],
            edit_worktree=_designer_appends_contract(),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "FixerFrontend",
        _make_cross_repo_agent_stub(
            role="fixer_frontend",
            completion_tokens=["FIXER_FRONTEND_FAILED"],
            edit_worktree=_appender(
                "Fixer-Frontend Failure",
                "- Diagnosis: tsc errors I can't resolve\n",
            ),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "FixerBackend",
        _make_cross_repo_agent_stub(
            role="fixer_backend",
            completion_tokens=["FIXER_BACKEND_COMPLETE"],
            edit_worktree=_appender("Fixer-Backend Notes", "- ok\n"),
        ),
    )
    # Integration tester should NEVER run.
    monkeypatch.setattr(
        pipeline_module,
        "IntegrationTester",
        _make_cross_repo_agent_stub(
            role="integration_tester",
            completion_tokens=["INTEGRATION_TESTER_COMPLETE"],
        ),
    )
    _stub_pkill(monkeypatch)

    session = engine_module.SessionLocal()
    bug = _make_cross_repo_bug(session, notion_page_id="fe-failed-1")
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
        assert "integration_tester" not in roles
    finally:
        session.close()

    backend_worktree = (
        stub_worktrees["tmp"] / "worktrees" / str(bug_id) / "avm-backend"
    )
    triage_md = (backend_worktree / "TRIAGE.md").read_text(encoding="utf-8")
    assert "## Fixer-Frontend Failure" in triage_md


# --- pkill is invoked even when integration FAILS terminally --------------


async def test_pkill_runs_even_on_terminal_integration_failure(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_worktrees: dict[str, Path],
) -> None:
    """Even when integration fails BOTH cycles → BLOCKED, pkill still runs."""
    _stub_investigator(monkeypatch)
    monkeypatch.setattr(
        pipeline_module,
        "FixDesigner",
        _make_cross_repo_agent_stub(
            role="fix_designer",
            completion_tokens=["FIX_DESIGNER_COMPLETE", "FIX_DESIGNER_COMPLETE"],
            edit_worktree=_designer_appends_contract(),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "FixerFrontend",
        _make_cross_repo_agent_stub(
            role="fixer_frontend",
            completion_tokens=[
                "FIXER_FRONTEND_COMPLETE",
                "FIXER_FRONTEND_COMPLETE",
            ],
            edit_worktree=_appender("Fixer-Frontend Notes", "- ok\n"),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "FixerBackend",
        _make_cross_repo_agent_stub(
            role="fixer_backend",
            completion_tokens=["FIXER_BACKEND_COMPLETE", "FIXER_BACKEND_COMPLETE"],
            edit_worktree=_appender("Fixer-Backend Notes", "- ok\n"),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "IntegrationTester",
        _make_cross_repo_agent_stub(
            role="integration_tester",
            completion_tokens=[
                "INTEGRATION_TESTER_FAILED",
                "INTEGRATION_TESTER_FAILED",
            ],
            edit_worktree=_appender(
                "Integration Tester Failure", "- 500\n"
            ),
        ),
    )
    pkill_invocations = _stub_pkill(monkeypatch)

    session = engine_module.SessionLocal()
    bug = _make_cross_repo_bug(session, notion_page_id="int-double-fail-1")
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

    # pkill should fire after EACH Integration Tester run = 2 calls.
    assert pkill_invocations.count(("pkill -f", "port 28080")) == 2
    assert pkill_invocations.count(("pkill -f", "port 25173")) == 2


# --- designer FAILED → BLOCKED --------------------------------------------


async def test_designer_failed_lands_in_blocked(
    alembic_db: Path,
    triage_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_worktrees: dict[str, Path],
) -> None:
    """FixDesigner FAILED → BLOCKED, no fixers ever run."""
    _stub_investigator(monkeypatch)
    monkeypatch.setattr(
        pipeline_module,
        "FixDesigner",
        _make_cross_repo_agent_stub(
            role="fix_designer",
            completion_tokens=["FIX_DESIGNER_FAILED"],
            edit_worktree=_appender(
                "Fix Designer Failure",
                "- Diagnosis: insufficient context\n",
            ),
        ),
    )
    # Fixers + integration tester must not be called.
    monkeypatch.setattr(
        pipeline_module,
        "FixerFrontend",
        _make_cross_repo_agent_stub(
            role="fixer_frontend",
            completion_tokens=["FIXER_FRONTEND_COMPLETE"],
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "FixerBackend",
        _make_cross_repo_agent_stub(
            role="fixer_backend",
            completion_tokens=["FIXER_BACKEND_COMPLETE"],
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "IntegrationTester",
        _make_cross_repo_agent_stub(
            role="integration_tester",
            completion_tokens=["INTEGRATION_TESTER_COMPLETE"],
        ),
    )
    _stub_pkill(monkeypatch)

    session = engine_module.SessionLocal()
    bug = _make_cross_repo_bug(session, notion_page_id="designer-fail-1")
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
        assert roles == ["investigator", "fix_designer"]
    finally:
        session.close()
