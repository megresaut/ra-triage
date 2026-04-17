"""Tests for Agent 5's new bug endpoints (triage + prompt + ws logs).

Uses FastAPI's ``TestClient`` with the migrated DB fixture. The tests
stub the Investigator and worktree subsystem with tiny fakes so we can
drive state transitions and TRIAGE.md artifacts without real agents.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ra_triage.main import create_app
from ra_triage.triage_md import parser as triage_parser


def _make_worktrees(tmp_path: Path, bug_id: int, names: list[str]) -> dict[str, Path]:
    """Create synthetic worktree directories under ``tmp_path``."""
    result: dict[str, Path] = {}
    for name in names:
        p = tmp_path / "worktrees" / str(bug_id) / name
        p.mkdir(parents=True, exist_ok=True)
        result[name] = p
    return result


@pytest.fixture()
def stub_worktree_module(
    triage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect the worktree module's filesystem operations to ``triage_home``.

    We override ``list_worktrees`` to scan ``$TRIAGE_HOME/worktrees/<id>/``
    just like the real one does, but since the test sets TRIAGE_HOME to a
    tmp dir via the ``triage_home`` fixture, the default implementation
    already Does The Right Thing. We return ``triage_home`` for callers
    that need to write test fixtures (TRIAGE.md content) into the tree.
    """
    return triage_home


def test_get_triage_endpoint_404_when_bug_missing(
    alembic_db: object,
) -> None:
    _ = alembic_db
    client = TestClient(create_app())
    resp = client.get("/bugs/9999/triage")
    assert resp.status_code == 404


def test_get_triage_endpoint_404_when_worktree_missing(
    alembic_db: object, stub_worktree_module: Path
) -> None:
    """Bug exists but no worktree on disk → 404 with guidance."""
    _ = alembic_db
    client = TestClient(create_app())
    created = client.post(
        "/poller/enqueue", json={"notion_page_id": "no-worktree"}
    ).json()
    resp = client.get(f"/bugs/{created['id']}/triage")
    assert resp.status_code == 404


def test_get_triage_endpoint_returns_empty_sections_when_file_missing(
    alembic_db: object, stub_worktree_module: Path
) -> None:
    """Worktree exists but TRIAGE.md absent → empty sections + empty raw."""
    _ = alembic_db
    client = TestClient(create_app())
    created = client.post(
        "/poller/enqueue", json={"notion_page_id": "empty-triage"}
    ).json()
    bug_id = created["id"]

    # Create the backend worktree dir to satisfy the list_worktrees check.
    backend_wt = (
        stub_worktree_module / "worktrees" / str(bug_id) / "avm-backend"
    )
    backend_wt.mkdir(parents=True, exist_ok=True)

    resp = client.get(f"/bugs/{bug_id}/triage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sections"] == {}
    assert body["raw"] == ""
    assert body["path"].endswith("TRIAGE.md")


def test_get_triage_endpoint_parses_sections(
    alembic_db: object, stub_worktree_module: Path
) -> None:
    """Existing TRIAGE.md is parsed into sections."""
    _ = alembic_db
    client = TestClient(create_app())
    created = client.post(
        "/poller/enqueue", json={"notion_page_id": "parsed-triage"}
    ).json()
    bug_id = created["id"]
    backend_wt = (
        stub_worktree_module / "worktrees" / str(bug_id) / "avm-backend"
    )
    backend_wt.mkdir(parents=True, exist_ok=True)

    triage_path = backend_wt / "TRIAGE.md"
    triage_parser.append(triage_path, "Root Cause", "src/x.py:1")
    triage_parser.append(triage_path, "Analysis", "it broke")

    resp = client.get(f"/bugs/{bug_id}/triage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sections"] == {
        "Root Cause": "src/x.py:1",
        "Analysis": "it broke",
    }
    assert "## Root Cause" in body["raw"]


def test_get_prompt_endpoint_returns_404_when_missing(
    alembic_db: object,
) -> None:
    _ = alembic_db
    client = TestClient(create_app())
    created = client.post(
        "/poller/enqueue", json={"notion_page_id": "p-1"}
    ).json()
    resp = client.get(
        f"/bugs/{created['id']}/prompt",
        params={"role": "investigator", "attempt": 1},
    )
    assert resp.status_code == 404


def test_get_prompt_endpoint_returns_file_when_exists(
    alembic_db: object, triage_home: Path
) -> None:
    """Prompt persisted by the base class → readable via /bugs/:id/prompt."""
    _ = alembic_db
    client = TestClient(create_app())
    created = client.post(
        "/poller/enqueue", json={"notion_page_id": "p-2"}
    ).json()
    bug_id = created["id"]

    prompt_dir = triage_home / "prompts" / str(bug_id)
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / "investigator.1.md"
    prompt_path.write_text("HELLO PROMPT", encoding="utf-8")

    resp = client.get(
        f"/bugs/{bug_id}/prompt",
        params={"role": "investigator", "attempt": 1},
    )
    assert resp.status_code == 200
    assert resp.text == "HELLO PROMPT"
    assert "markdown" in resp.headers["content-type"]
