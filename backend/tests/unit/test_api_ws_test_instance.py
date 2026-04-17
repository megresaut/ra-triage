"""Tests for the /ws/test-instance WebSocket."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ra_triage.main import create_app
from ra_triage.utils.events import (
    publish_test_instance_log,
    publish_test_instance_status,
)


def test_ws_test_instance_hello_and_log_forward(
    alembic_db: Path, triage_home: Path
) -> None:
    _ = alembic_db, triage_home
    app = create_app()
    client = TestClient(app)

    with client.websocket_connect("/ws/test-instance") as ws:
        hello = ws.receive_json()
        assert hello == {"type": "hello"}

        # Initial status snapshot (no instance running → status="none").
        snapshot = ws.receive_json()
        assert snapshot["type"] == "test_instance_status"
        assert snapshot["status"] == "none"

        # Publish a fake log line — client should receive it.
        publish_test_instance_log(bug_id=7, repo="avm-backend", text="starting up")
        frame = ws.receive_json()
        assert frame["type"] == "test_log"
        assert frame["bug_id"] == 7
        assert frame["repo"] == "avm-backend"
        assert frame["line"] == "starting up"


def test_ws_test_instance_status_forward(
    alembic_db: Path, triage_home: Path
) -> None:
    _ = alembic_db, triage_home
    app = create_app()
    client = TestClient(app)

    with client.websocket_connect("/ws/test-instance") as ws:
        # hello + snapshot
        ws.receive_json()
        ws.receive_json()

        publish_test_instance_status(
            {
                "bug_id": 42,
                "status": "stopping",
                "row_id": 1,
                "ports": {"avm-backend": 18080},
                "pids": {"avm-backend": 1234},
            }
        )
        frame = ws.receive_json()
        assert frame["type"] == "test_instance_status"
        assert frame["status"] == "stopping"
        assert frame["bug_id"] == 42
