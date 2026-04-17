"""Tests for /projects endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from ra_triage.main import create_app


def test_list_projects_returns_ra_avm() -> None:
    client = TestClient(create_app())
    response = client.get("/projects")
    assert response.status_code == 200
    projects = response.json()
    assert any(p["id"] == "ra_avm" for p in projects)


def test_get_ra_avm_contains_repos_with_ports() -> None:
    client = TestClient(create_app())
    response = client.get("/projects/ra_avm")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "ra_avm"
    assert body["auto_merge"] is False
    names = {r["name"] for r in body["repos"]}
    assert names == {"avm-backend", "builder-frontend"}
    # Ports and github_origin should be present on repos.
    for r in body["repos"]:
        assert r["dev_port"] > 0
        assert r["test_launch_port"] > 0
        assert r["integration_port"] > 0
        assert r["github_origin"].startswith("github.com/")


def test_get_project_404_on_unknown() -> None:
    client = TestClient(create_app())
    response = client.get("/projects/nope")
    assert response.status_code == 404


def test_project_response_does_not_leak_absolute_paths() -> None:
    """Public view must not include filesystem paths or DB URLs."""
    client = TestClient(create_app())
    body = client.get("/projects/ra_avm").json()
    serialized = str(body)
    assert "/Users/" not in serialized
    assert "postgres://" not in serialized
    assert "RA_AVM_SLACK_WEBHOOK" not in serialized
