"""Tests for /ws/bugs/{bug_id}/logs.

Exercises the WS endpoint with a TestClient: subscribe, then publish an
``agent_stdout`` payload on the bus and assert the client receives it.
Also verifies the backfill-from-log-file path.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ra_triage.main import create_app
from ra_triage.utils.events import publish_agent_stdout


def test_ws_bug_logs_forwards_agent_stdout(alembic_db: object) -> None:
    """Connect → publish → client receives the event frame."""
    _ = alembic_db
    app = create_app()
    client = TestClient(app)

    with client.websocket_connect("/ws/bugs/1/logs") as ws:
        hello = ws.receive_json()
        assert hello == {"type": "hello", "bug_id": 1}

        # Publish onto the per-bug logs topic. Because TestClient wraps the
        # ASGI app in its own loop, publish_agent_stdout (which uses the
        # bus's attached loop) will land on the same loop.
        publish_agent_stdout(bug_id=1, role="investigator", attempt=1, text="hi")

        frame = ws.receive_json()
        assert frame["type"] == "agent_stdout"
        assert frame["bug_id"] == 1
        assert frame["text"] == "hi"
        assert frame["role"] == "investigator"
        assert frame["attempt"] == 1


def test_ws_bug_logs_backfill_from_recent_run(
    alembic_db: object, triage_home: Path
) -> None:
    """A pre-existing agent_runs row with a log file replays its tail on connect."""
    from ra_triage.db import engine as engine_module
    from ra_triage.db import repo as repo_module

    _ = alembic_db

    # Seed: create a bug + agent_run with a log file on disk.
    session = engine_module.SessionLocal()
    try:
        bug = repo_module.create_bug(
            session,
            notion_page_id="bk-1",
            project_id="ra_avm",
            title="x",
            description="y",
        )
        bug_id = bug.id
        log_dir = triage_home / "logs" / str(bug_id)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "investigator.1.log"
        log_path.write_text(
            "line one\nline two\nline three\n", encoding="utf-8"
        )
        from datetime import UTC, datetime

        repo_module.record_agent_run(
            session,
            bug_id=bug_id,
            agent_role="investigator",
            attempt=1,
            started_at=datetime.now(UTC).replace(tzinfo=None),
            log_path=str(log_path),
            prompt_path=str(log_dir / "investigator.1.md"),
        )
        session.commit()
    finally:
        session.close()

    app = create_app()
    client = TestClient(app)

    received: list[dict] = []
    with client.websocket_connect(f"/ws/bugs/{bug_id}/logs") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"

        # Backfill lines arrive back-to-back. Read with a short timeout
        # budget; stop when no more arrive.
        for _ in range(10):
            try:
                frame = ws.receive_json()
            except Exception:
                break
            received.append(frame)
            if frame.get("backfill") and frame.get("text") == "line three":
                break

    texts = [r["text"] for r in received if r.get("backfill")]
    assert "line one" in texts
    assert "line two" in texts
    assert "line three" in texts
