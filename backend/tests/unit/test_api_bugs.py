"""End-to-end tests for /bugs and /poller/enqueue via FastAPI's TestClient.

We build a fresh FastAPI app inside each test so the imported `app` (which
bound routers at import time) picks up our alembic-migrated SessionLocal via
the monkeypatch on `engine_module`.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ra_triage.main import create_app


def test_poller_enqueue_creates_bug_and_list_returns_it(alembic_db: object) -> None:
    _ = alembic_db  # fixture side effects: DB migrated, SessionLocal rebound
    client = TestClient(create_app())

    response = client.post("/poller/enqueue", json={"notion_page_id": "test-1"})
    assert response.status_code == 200
    body = response.json()
    assert body["notion_page_id"] == "test-1"
    assert body["state"] == "OPEN"
    assert body["title"] == "Bug test-1"
    assert body["project_id"] == "ra_avm"
    assert body["launched_for_testing"] is False
    assert body["current_agent"] is None
    assert body["conflict_files"] is None

    listing = client.get("/bugs")
    assert listing.status_code == 200
    items = listing.json()
    assert len(items) == 1
    assert items[0]["notion_page_id"] == "test-1"


def test_poller_enqueue_is_idempotent(alembic_db: object) -> None:
    _ = alembic_db
    client = TestClient(create_app())
    body = {"notion_page_id": "dup-1", "title": "custom", "description": "custom-d"}
    first = client.post("/poller/enqueue", json=body)
    assert first.status_code == 200
    first_json = first.json()

    # Second post with SAME notion_page_id returns same bug, does not duplicate.
    second = client.post(
        "/poller/enqueue", json={"notion_page_id": "dup-1", "title": "different"}
    )
    assert second.status_code == 200
    second_json = second.json()
    assert second_json["id"] == first_json["id"]
    # Original title preserved (idempotent on the notion_page_id, doesn't overwrite).
    assert second_json["title"] == "custom"

    listing = client.get("/bugs").json()
    assert len(listing) == 1


def test_get_bug_by_id_and_404(alembic_db: object) -> None:
    _ = alembic_db
    client = TestClient(create_app())
    created = client.post("/poller/enqueue", json={"notion_page_id": "t-2"}).json()
    bug_id = created["id"]

    got = client.get(f"/bugs/{bug_id}")
    assert got.status_code == 200
    assert got.json()["id"] == bug_id

    missing = client.get("/bugs/99999")
    assert missing.status_code == 404


def test_list_bugs_filters_by_project(alembic_db: object) -> None:
    _ = alembic_db
    client = TestClient(create_app())
    client.post("/poller/enqueue", json={"notion_page_id": "a"})
    client.post(
        "/poller/enqueue", json={"notion_page_id": "b", "project_id": "other"}
    )

    avm = client.get("/bugs?project_id=ra_avm").json()
    assert len(avm) == 1
    assert avm[0]["notion_page_id"] == "a"

    other = client.get("/bugs?project_id=other").json()
    assert len(other) == 1
    assert other[0]["notion_page_id"] == "b"


def test_agent_runs_endpoint_empty_then_404(alembic_db: object) -> None:
    _ = alembic_db
    client = TestClient(create_app())
    created = client.post("/poller/enqueue", json={"notion_page_id": "ar"}).json()
    bug_id = created["id"]

    runs = client.get(f"/bugs/{bug_id}/agent-runs")
    assert runs.status_code == 200
    assert runs.json() == []

    missing = client.get("/bugs/42424242/agent-runs")
    assert missing.status_code == 404


def test_ws_bugs_receives_hello_and_state_change(alembic_db: object) -> None:
    _ = alembic_db
    # Connect WS, enqueue a bug, transition its state, then confirm we
    # received a state_change frame for the queue-wide topic.
    app = create_app()
    client = TestClient(app)

    # First, enqueue via HTTP to get a bug we can transition.
    enqueue = client.post("/poller/enqueue", json={"notion_page_id": "ws-1"})
    bug_id = enqueue.json()["id"]

    with client.websocket_connect("/ws/bugs") as ws:
        hello = ws.receive_json()
        assert hello == {"type": "hello"}

        # Perform a state transition via the repo to trigger publish.
        from ra_triage.db import engine as engine_module
        from ra_triage.db import repo
        from ra_triage.orchestrator.states import BugState

        session = engine_module.SessionLocal()
        try:
            bug = repo.get_bug(session, bug_id)
            assert bug is not None
            repo.update_bug_state(session, bug, BugState.TRIAGED)
            session.commit()
        finally:
            session.close()

        # Receive the state_change frame (TestClient runs WS on an event loop
        # under the covers, so `publish_sync` -> `call_soon_threadsafe` works).
        event = ws.receive_json()
        assert event["type"] == "state_change"
        assert event["bug_id"] == bug_id
        assert event["previous_state"] == "OPEN"
        assert event["new_state"] == "TRIAGED"
