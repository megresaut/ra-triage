"""Smoke test: /health returns 200 with {"status": "ok"}."""

from __future__ import annotations

from fastapi.testclient import TestClient

from ra_triage.main import app


def test_health_endpoint_returns_ok() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
