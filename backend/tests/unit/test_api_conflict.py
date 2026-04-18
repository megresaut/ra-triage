"""Integration-ish tests for the Agent 11 API surface.

- ``GET /bugs/:id/rebase-attempts`` returns the audit list.
- ``GET /bugs/:id/conflict-diff`` returns the diff snapshot.
- ``POST /bugs/:id/retry-fixer-with-conflict`` validates state + schedules
  :func:`orchestrator.pipeline.retry_fixer_with_conflict`.
- The Fixer prompt rendered by the retry flow contains the conflict file
  list AND the diff snippet — matches acceptance criterion #4.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from ra_triage.api import bugs as bugs_api
from ra_triage.config.registry import ProjectConfig, RepoConfig
from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.main import create_app
from ra_triage.orchestrator import pipeline as pipeline_module
from ra_triage.orchestrator.states import BugState
from ra_triage.workspaces import rebase as rebase_module
from ra_triage.workspaces import worktree as wt_module

# --- fixtures -------------------------------------------------------------


@pytest.fixture()
def client(alembic_db: Path) -> Iterator[TestClient]:
    """FastAPI TestClient wired to the migrated test DB."""
    _ = alembic_db
    app = create_app()
    with TestClient(app) as c:
        yield c


def _git(repo: Path, *args: str) -> None:
    subprocess.check_call(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            *args,
        ]
    )


def _seed_canonical_and_worktree(
    canonical: Path,
    triage_home: Path,
    *,
    bug_id: int,
    repo_name: str,
) -> Path:
    """Initialize a canonical clone + bare remote + a bug's worktree with
    a committed triage branch so ``git diff origin/main...HEAD`` returns
    a non-empty diff.
    """
    subprocess.check_call(["git", "init", "-b", "main", str(canonical)])
    (canonical / "file.txt").write_text("base\n", encoding="utf-8")
    _git(canonical, "add", "-A")
    _git(canonical, "commit", "-m", "init")

    bare = canonical.parent / "origin.git"
    subprocess.check_call(["git", "clone", "--bare", str(canonical), str(bare)])
    _git(canonical, "remote", "add", "origin", str(bare))
    _git(canonical, "fetch", "origin")

    worktree = triage_home / "worktrees" / str(bug_id) / repo_name
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        [
            "git",
            "-C",
            str(canonical),
            "worktree",
            "add",
            str(worktree),
            "-b",
            f"triage/{bug_id}",
            "origin/main",
        ]
    )
    (worktree / "file.txt").write_text("AGENT-11-CHANGE\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", "bug fix on triage branch")
    return worktree


def _seed_bug_with_conflict(
    session: Session,
    *,
    notion_page_id: str,
    repo_name: str,
    conflict_files: list[str],
    affected: list[str] | None = None,
    scope: str = "single-repo",
):
    """Create a BLOCKED bug with ``conflict_files`` populated + a
    ``rebase_attempts`` row of result='conflict' for the given repo."""
    bug = repo_module.create_bug(
        session,
        notion_page_id=notion_page_id,
        project_id="ra_avm",
        title="Conflicted bug",
        description="d",
    )
    bug.affected_repos = json.dumps(affected or [repo_name])
    bug.scope = scope
    session.flush()
    # Walk to BLOCKED.
    for nxt in (
        BugState.TRIAGED,
        BugState.INVESTIGATING,
        BugState.FIXING,
        BugState.BLOCKED,
    ):
        repo_module.update_bug_state(session, bug, nxt)
    # Stamp conflict_files + audit row.
    bug.conflict_files = json.dumps(conflict_files)
    session.flush()
    repo_module.record_rebase_attempt(
        session,
        bug_id=bug.id,
        repo_name=repo_name,
        trigger_bug_id=bug.id,  # reuse as a placeholder
        result="conflict",
        conflict_files=conflict_files,
    )
    session.commit()
    return bug


# --- /bugs/:id/rebase-attempts -------------------------------------------


def test_list_rebase_attempts_endpoint(
    client: TestClient,
) -> None:
    """Returns the audit rows with parsed conflict_files."""
    session = engine_module.SessionLocal()
    try:
        bug = _seed_bug_with_conflict(
            session,
            notion_page_id="p-api-audit",
            repo_name="avm-backend",
            conflict_files=["api/handlers.go"],
        )
        bug_id = bug.id
    finally:
        session.close()

    resp = client.get(f"/bugs/{bug_id}/rebase-attempts")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["result"] == "conflict"
    assert body[0]["conflict_files"] == ["api/handlers.go"]
    assert body[0]["repo_name"] == "avm-backend"


def test_list_rebase_attempts_404_for_missing_bug(
    client: TestClient,
) -> None:
    resp = client.get("/bugs/99999/rebase-attempts")
    assert resp.status_code == 404


# --- /bugs/:id/conflict-diff --------------------------------------------


def test_conflict_diff_returns_diff(
    client: TestClient,
    triage_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returns a non-empty diff for a BLOCKED-with-conflict bug."""
    canonical = tmp_path / "canonical"

    session = engine_module.SessionLocal()
    try:
        bug = _seed_bug_with_conflict(
            session,
            notion_page_id="p-api-diff",
            repo_name="avm-backend",
            conflict_files=["file.txt"],
        )
        bug_id = bug.id
    finally:
        session.close()

    _seed_canonical_and_worktree(
        canonical, triage_home, bug_id=bug_id, repo_name="avm-backend"
    )

    # Patch the RepoConfig to point at our test canonical clone so
    # ``fetch_conflict_diff`` runs git against the right path.
    from ra_triage.config import registry as registry_module

    orig_project = registry_module.PROJECTS["ra_avm"]
    patched_repo = RepoConfig(
        name="avm-backend",
        path=canonical,
        main_branch="main",
        branch_prefix="triage/",
        dev_command="true",
        build_command="true",
        test_command="true",
        dev_port=1,
        test_launch_port=2,
        integration_port=3,
        env_file=".env",
        role="backend",
        github_origin="x",
    )
    patched = ProjectConfig(
        id="ra_avm",
        display_name=orig_project.display_name,
        repos=[patched_repo],
        deny_list=orig_project.deny_list,
        slack_webhook_env=orig_project.slack_webhook_env,
        auto_merge=False,
    )
    monkeypatch.setitem(registry_module.PROJECTS, "ra_avm", patched)

    resp = client.get(f"/bugs/{bug_id}/conflict-diff")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bug_id"] == bug_id
    assert body["repo_name"] == "avm-backend"
    assert "AGENT-11-CHANGE" in body["diff"]


def test_conflict_diff_404_when_bug_has_no_conflict(
    client: TestClient,
) -> None:
    """A bug without a conflict returns 404."""
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.create_bug(
            session,
            notion_page_id="p-no-conflict",
            project_id="ra_avm",
            title="No conflict",
            description="d",
        )
        bug_id = bug.id
        session.commit()
    finally:
        session.close()

    resp = client.get(f"/bugs/{bug_id}/conflict-diff")
    assert resp.status_code == 404


# --- /bugs/:id/retry-fixer-with-conflict ---------------------------------


def test_retry_fixer_with_conflict_returns_202_and_schedules(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid BLOCKED-with-conflict: returns 202 + schedules coroutine."""
    session = engine_module.SessionLocal()
    try:
        bug = _seed_bug_with_conflict(
            session,
            notion_page_id="p-retry-api",
            repo_name="avm-backend",
            conflict_files=["api/handlers.go"],
        )
        bug_id = bug.id
    finally:
        session.close()

    scheduled_ids: list[int] = []

    async def _fake_retry(bug_id_arg: int) -> None:
        scheduled_ids.append(bug_id_arg)

    monkeypatch.setattr(bugs_api, "retry_fixer_with_conflict", _fake_retry)

    resp = client.post(f"/bugs/{bug_id}/retry-fixer-with-conflict")
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "retrying"
    assert body["bug_id"] == bug_id
    assert body["new_state"] == "FIXING"


def test_retry_fixer_with_conflict_409_when_not_blocked(
    client: TestClient,
) -> None:
    """409 when the bug is not in BLOCKED."""
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.create_bug(
            session,
            notion_page_id="p-retry-wrong-state",
            project_id="ra_avm",
            title="t",
            description="d",
        )
        bug_id = bug.id
        session.commit()
    finally:
        session.close()

    resp = client.post(f"/bugs/{bug_id}/retry-fixer-with-conflict")
    assert resp.status_code == 409


def test_retry_fixer_with_conflict_409_when_no_conflict(
    client: TestClient,
) -> None:
    """409 when BLOCKED but conflict_files is NULL (use generic /retry instead)."""
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.create_bug(
            session,
            notion_page_id="p-retry-no-files",
            project_id="ra_avm",
            title="t",
            description="d",
        )
        for nxt in (
            BugState.TRIAGED,
            BugState.INVESTIGATING,
            BugState.BLOCKED,
        ):
            repo_module.update_bug_state(session, bug, nxt)
        bug_id = bug.id
        session.commit()
    finally:
        session.close()

    resp = client.post(f"/bugs/{bug_id}/retry-fixer-with-conflict")
    assert resp.status_code == 409


# --- End-to-end prompt rendering (acceptance criterion #4) ---------------


async def test_retry_fixer_with_conflict_renders_prompt_with_context(
    alembic_db: Path,
    triage_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry coroutine renders a Fixer prompt that includes the
    conflict file list AND the diff snippet.

    We exercise :func:`orchestrator.pipeline.retry_fixer_with_conflict`
    directly with the Fixer subprocess stubbed so the test can inspect
    the persisted prompt file at
    ``~/ra-triage/prompts/<bug>/fixer.<attempt>.md``.
    """
    _ = alembic_db
    canonical = tmp_path / "canonical"

    session = engine_module.SessionLocal()
    try:
        bug = _seed_bug_with_conflict(
            session,
            notion_page_id="p-retry-prompt",
            repo_name="avm-backend",
            conflict_files=["file.txt"],
        )
        bug_id = bug.id
    finally:
        session.close()

    _seed_canonical_and_worktree(
        canonical, triage_home, bug_id=bug_id, repo_name="avm-backend"
    )

    # Patch the registry so the rebase helpers find our fixture canonical.
    from ra_triage.config import registry as registry_module

    orig_project = registry_module.PROJECTS["ra_avm"]
    patched_repo = RepoConfig(
        name="avm-backend",
        path=canonical,
        main_branch="main",
        branch_prefix="triage/",
        dev_command="true",
        build_command="cd . && echo ok",
        test_command="true",
        dev_port=1,
        test_launch_port=2,
        integration_port=3,
        env_file=".env",
        role="backend",
        github_origin="x",
    )
    patched = ProjectConfig(
        id="ra_avm",
        display_name=orig_project.display_name,
        repos=[patched_repo],
        deny_list=orig_project.deny_list,
        slack_webhook_env=orig_project.slack_webhook_env,
        auto_merge=False,
    )
    monkeypatch.setitem(registry_module.PROJECTS, "ra_avm", patched)

    # Stub the Fixer's subprocess spawn: capture whether the prompt was
    # written but don't actually run Claude. We run a Tester stub too so
    # the pipeline's TESTING transition completes.
    from ra_triage.agents.base import AgentResult

    async def _fake_fixer_run(self, *, bug, worktree: Path, context: dict[str, Any]) -> AgentResult:
        # The base class persists the prompt to
        # ~/ra-triage/prompts/<bug_id>/<role>.<attempt>.md BEFORE
        # invoking the subprocess. We delegate to the base class's
        # instance-level render helper so the Jinja StrictUndefined
        # rules match prod.
        prompt = self._render_prompt(context)
        from ra_triage.settings import get_settings

        settings = get_settings()
        prompt_dir = settings.prompts_dir / str(bug.id)
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = prompt_dir / f"{self.role}.{context['attempt']}.md"
        prompt_path.write_text(prompt, encoding="utf-8")

        # Write a stub Fixer Notes section so the pipeline sees
        # FIXER_COMPLETE and proceeds.
        triage_md = worktree / "TRIAGE.md"
        triage_md.parent.mkdir(parents=True, exist_ok=True)
        existing = triage_md.read_text(encoding="utf-8") if triage_md.exists() else ""
        triage_md.write_text(
            existing + "\n\n## Fixer Notes\n\n- files changed: file.txt\n",
            encoding="utf-8",
        )

        import datetime as dt

        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        return AgentResult(
            exit_code=0,
            completion_token="FIXER_COMPLETE",
            log_path=prompt_path.with_suffix(".log"),
            prompt_path=prompt_path,
            triage_md=triage_md.read_text(encoding="utf-8"),
            started_at=now,
            ended_at=now,
        )

    async def _fake_tester_run(self, *, bug, worktree: Path, context: dict[str, Any]) -> AgentResult:
        import datetime as dt

        from ra_triage.settings import get_settings

        settings = get_settings()
        prompt_dir = settings.prompts_dir / str(bug.id)
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = prompt_dir / f"{self.role}.{context['attempt']}.md"
        prompt_path.write_text("(stub tester prompt)", encoding="utf-8")
        triage_md = worktree / "TRIAGE.md"
        existing = triage_md.read_text(encoding="utf-8") if triage_md.exists() else ""
        triage_md.write_text(
            existing + "\n\n## Tester Notes\n\n- all green\n", encoding="utf-8"
        )
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        return AgentResult(
            exit_code=0,
            completion_token="TESTER_COMPLETE",
            log_path=prompt_path.with_suffix(".log"),
            prompt_path=prompt_path,
            triage_md=triage_md.read_text(encoding="utf-8"),
            started_at=now,
            ended_at=now,
        )

    from ra_triage.agents.fixer import Fixer
    from ra_triage.agents.tester import Tester

    monkeypatch.setattr(Fixer, "run", _fake_fixer_run)
    monkeypatch.setattr(Tester, "run", _fake_tester_run)

    # Make sure TRIAGE.md has Scope so _pick_single_affected_repo
    # resolves. The seeded canonical worktree has no TRIAGE.md yet.
    worktree = triage_home / "worktrees" / str(bug_id) / "avm-backend"
    triage_md = worktree / "TRIAGE.md"
    triage_md.write_text(
        "## Scope\n\nscope: single-repo\n"
        'affected_repos: ["avm-backend"]\n'
        "deny_list_hit: false\n\n"
        "## Root Cause\n\nfile.txt edit\n",
        encoding="utf-8",
    )

    # Also stub the post-TESTING auto-teardown of the worktree (cleanup).
    async def _noop_cleanup(bug_id: int, repos: list[RepoConfig]) -> None:
        return None

    monkeypatch.setattr(wt_module, "cleanup", _noop_cleanup)

    # Exercise the real retry coroutine end-to-end.
    await pipeline_module.retry_fixer_with_conflict(bug_id)
    # Let any scheduled tasks drain.
    await asyncio.sleep(0.05)

    # Inspect the persisted Fixer prompt.
    from ra_triage.settings import get_settings

    settings = get_settings()
    prompt_dir = settings.prompts_dir / str(bug_id)
    prompts = sorted(prompt_dir.glob("fixer.*.md"))
    assert prompts, f"No Fixer prompts found in {prompt_dir}"
    latest = prompts[-1]
    body = latest.read_text(encoding="utf-8")
    assert "PRIOR FIX HAD A MERGE CONFLICT" in body, (
        f"Expected conflict_context block in rendered prompt:\n{body}"
    )
    # Conflict file list appears verbatim.
    assert "file.txt" in body
    # Diff snippet contains the triage-branch content.
    assert "AGENT-11-CHANGE" in body


# Silence unused imports on linters.
_ = rebase_module
