"""TestClient exercises for /test-instances/*.

Mocks the manager's effects (spawn / db restore / npm / webbrowser) so the
endpoints can be exercised without real Go/Vite/Postgres.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.main import create_app
from ra_triage.orchestrator.states import BugState
from ra_triage.test_runner import db_snapshot, instance, processes
from ra_triage.test_runner.instance import reset_manager_for_tests
from ra_triage.test_runner.processes import SpawnedProcess


class _FakeProc:
    def __init__(self) -> None:
        self.returncode: int | None = None

    async def wait(self) -> int:
        return 0


def _fake_spawned(repo_name: str, port: int, pid: int) -> SpawnedProcess:
    return SpawnedProcess(
        repo_name=repo_name,
        pid=pid,
        port=port,
        proc=_FakeProc(),  # type: ignore[arg-type]
        log_path=Path("/dev/null"),
        log_task=None,
    )


def _seed_bug(notion_id: str) -> int:
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.create_bug(
            session,
            notion_page_id=notion_id,
            project_id="ra_avm",
            title="t",
            description="d",
        )
        for s in [
            BugState.TRIAGED,
            BugState.INVESTIGATING,
            BugState.FIXING,
            BugState.TESTING,
            BugState.READY_FOR_REVIEW,
        ]:
            repo_module.update_bug_state(session, bug, s)
        session.commit()
        return int(bug.id)
    finally:
        session.close()


def _create_worktrees_on_disk(triage_home: Path, bug_id: int) -> None:
    for name in ("avm-backend", "builder-frontend"):
        (triage_home / "worktrees" / str(bug_id) / name).mkdir(
            parents=True, exist_ok=True
        )


@pytest.fixture()
def patch_launch(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch spawn / readiness / stop / db / npm / browser; return a state dict."""
    reset_manager_for_tests()
    state: dict[str, Any] = {"spawned": [], "opened_urls": []}
    pid_counter = [2000]

    async def _fake_spawn_dev(
        repo: Any, worktree: Any, port: int, extra_env: Any, bug_id: int
    ) -> SpawnedProcess:
        pid_counter[0] += 1
        state["spawned"].append((repo.name, port))
        return _fake_spawned(repo.name, port, pid_counter[0])

    async def _fake_wait(port: int, timeout_s: float = 30.0) -> None:
        return None

    async def _fake_stop(sp: SpawnedProcess, term_wait: float = 5.0) -> None:
        object.__setattr__(sp.proc, "returncode", 0)

    def _fake_restore(pid: str, du: str, tu: str, test_db_name: str = "ra_triage_test") -> str:
        return "cafef00d1234"

    async def _fake_npm(worktree: Path) -> None:
        return None

    def _fake_open(url: str) -> bool:
        state["opened_urls"].append(url)
        return True

    monkeypatch.setattr(processes, "spawn_dev", _fake_spawn_dev)
    monkeypatch.setattr(processes, "wait_for_readiness", _fake_wait)
    monkeypatch.setattr(processes, "stop_process", _fake_stop)
    monkeypatch.setattr(instance.processes, "spawn_dev", _fake_spawn_dev)
    monkeypatch.setattr(instance.processes, "wait_for_readiness", _fake_wait)
    monkeypatch.setattr(instance.processes, "stop_process", _fake_stop)
    monkeypatch.setattr(db_snapshot, "restore_test_db", _fake_restore)
    monkeypatch.setattr(instance.db_snapshot, "restore_test_db", _fake_restore)
    monkeypatch.setattr(instance, "_run_npm_ci", _fake_npm)
    monkeypatch.setattr(instance.webbrowser, "open", _fake_open)
    return state


def test_launch_returns_running_snapshot(
    alembic_db: Path, triage_home: Path, patch_launch: dict[str, Any]
) -> None:
    _ = alembic_db, patch_launch
    bug_id = _seed_bug("api-launch-1")
    _create_worktrees_on_disk(triage_home, bug_id)

    app = create_app()
    client = TestClient(app)
    resp = client.post("/test-instances/launch", json={"bug_id": bug_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bug_id"] == bug_id
    assert body["status"] == "running"
    assert body["ports"]["avm-backend"] == 18080
    assert body["ports"]["builder-frontend"] == 15173
    assert body["db_snapshot_hash"] == "cafef00d1234"

    # The frontend port should be the browser-open target.
    assert body["browser_port"] == 15173
    assert patch_launch["opened_urls"] == ["http://127.0.0.1:15173"]

    # Follow-up: GET /current returns the same snapshot.
    current_resp = client.get("/test-instances/current")
    assert current_resp.status_code == 200
    current = current_resp.json()
    assert current["bug_id"] == bug_id
    assert current["status"] == "running"


def test_current_returns_204_when_nothing_running(
    alembic_db: Path, triage_home: Path, patch_launch: dict[str, Any]
) -> None:
    _ = alembic_db, triage_home, patch_launch
    app = create_app()
    client = TestClient(app)
    resp = client.get("/test-instances/current")
    assert resp.status_code == 204
    assert resp.content == b""


def test_stop_returns_stopped_when_running_else_none(
    alembic_db: Path, triage_home: Path, patch_launch: dict[str, Any]
) -> None:
    _ = alembic_db, patch_launch
    bug_id = _seed_bug("api-stop-1")
    _create_worktrees_on_disk(triage_home, bug_id)

    app = create_app()
    client = TestClient(app)

    resp = client.post("/test-instances/stop")
    assert resp.status_code == 200
    assert resp.json() == {"status": "none"}

    client.post("/test-instances/launch", json={"bug_id": bug_id})
    resp = client.post("/test-instances/stop")
    assert resp.status_code == 200
    assert resp.json() == {"status": "stopped"}

    # /current is 204 again.
    assert client.get("/test-instances/current").status_code == 204


def test_launch_while_another_running_switches(
    alembic_db: Path, triage_home: Path, patch_launch: dict[str, Any]
) -> None:
    _ = alembic_db, patch_launch
    bug_a = _seed_bug("api-switch-a")
    bug_b = _seed_bug("api-switch-b")
    _create_worktrees_on_disk(triage_home, bug_a)
    _create_worktrees_on_disk(triage_home, bug_b)

    app = create_app()
    client = TestClient(app)

    r1 = client.post("/test-instances/launch", json={"bug_id": bug_a})
    assert r1.status_code == 200
    r2 = client.post("/test-instances/launch", json={"bug_id": bug_b})
    assert r2.status_code == 200

    current = client.get("/test-instances/current").json()
    assert current["bug_id"] == bug_b


def test_bug_response_launched_for_testing_flag(
    alembic_db: Path, triage_home: Path, patch_launch: dict[str, Any]
) -> None:
    """/bugs/{id} reports launched_for_testing true/false correctly."""
    _ = alembic_db, patch_launch
    bug_id = _seed_bug("api-flag-1")
    _create_worktrees_on_disk(triage_home, bug_id)

    app = create_app()
    client = TestClient(app)

    before = client.get(f"/bugs/{bug_id}").json()
    assert before["launched_for_testing"] is False

    client.post("/test-instances/launch", json={"bug_id": bug_id})
    during = client.get(f"/bugs/{bug_id}").json()
    assert during["launched_for_testing"] is True

    client.post("/test-instances/stop")
    after = client.get(f"/bugs/{bug_id}").json()
    assert after["launched_for_testing"] is False
