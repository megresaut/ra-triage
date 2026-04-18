"""Tests for ``api.settings`` (Agent 13).

Coverage:
  - GET /settings returns the registry projects with effective + default
    ``auto_merge`` values, plus repos and deny-list views.
  - GET response is public-safe (no filesystem paths, no DB URLs).
  - PUT /settings/projects/{id} writes the override and a subsequent GET
    reflects it.
  - PUT survives a backend restart (re-instantiating the app re-reads
    ``settings.json``).
  - PUT for unknown project returns 404.
  - PUT body validation: missing body / wrong type → 422.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from ra_triage.main import create_app

# --- GET /settings -------------------------------------------------------


def test_get_settings_returns_ra_avm_with_auto_merge_false(
    triage_home: Path,
) -> None:
    """Default state: AVM appears with effective and default both False."""
    client = TestClient(create_app())
    response = client.get("/settings")
    assert response.status_code == 200
    body = response.json()

    assert "projects" in body
    avm = next(p for p in body["projects"] if p["id"] == "ra_avm")
    assert avm["display_name"] == "RA AVM"
    assert avm["auto_merge"] is False
    assert avm["auto_merge_default"] is False


def test_get_settings_includes_repo_views_and_deny_list(
    triage_home: Path,
) -> None:
    """Repos and deny-list are surfaced for the read-only registry display."""
    client = TestClient(create_app())
    body = client.get("/settings").json()
    avm = next(p for p in body["projects"] if p["id"] == "ra_avm")

    repo_names = {r["name"] for r in avm["repos"]}
    assert repo_names == {"avm-backend", "builder-frontend"}
    for r in avm["repos"]:
        assert r["dev_port"] > 0
        assert r["test_launch_port"] > 0
        assert r["integration_port"] > 0
        assert r["github_origin"].startswith("github.com/")
        assert r["main_branch"] == "main"
        assert r["branch_prefix"] == "triage/"

    assert "paths" in avm["deny_list"]
    assert "categories" in avm["deny_list"]
    assert any("auth" in p for p in avm["deny_list"]["paths"])


def test_get_settings_response_does_not_leak_paths_or_secrets(
    triage_home: Path,
) -> None:
    """Public surface must not include filesystem paths or DB URLs."""
    client = TestClient(create_app())
    body = client.get("/settings").json()
    serialized = json.dumps(body)
    assert "/Users/" not in serialized
    assert "postgres://" not in serialized
    assert "RA_AVM_SLACK_WEBHOOK" not in serialized


# --- PUT /settings/projects/{id} -----------------------------------------


def test_put_settings_writes_override_and_get_reflects_it(
    triage_home: Path,
) -> None:
    """PUT toggles the override; subsequent GET shows the new effective value."""
    client = TestClient(create_app())

    response = client.put(
        "/settings/projects/ra_avm", json={"auto_merge": True}
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "project_id": "ra_avm",
        "auto_merge": True,
        "auto_merge_default": False,
    }

    follow_up = client.get("/settings").json()
    avm = next(p for p in follow_up["projects"] if p["id"] == "ra_avm")
    assert avm["auto_merge"] is True
    assert avm["auto_merge_default"] is False


def test_put_settings_creates_settings_json_on_disk(triage_home: Path) -> None:
    """The toggle is durable — file appears under TRIAGE_HOME."""
    client = TestClient(create_app())
    assert not (triage_home / "settings.json").exists()
    client.put("/settings/projects/ra_avm", json={"auto_merge": True})
    assert (triage_home / "settings.json").exists()
    parsed = json.loads((triage_home / "settings.json").read_text(encoding="utf-8"))
    assert parsed["projects"]["ra_avm"]["auto_merge"] is True


def test_put_settings_survives_backend_restart(triage_home: Path) -> None:
    """Re-instantiating the FastAPI app re-reads settings.json from disk."""
    client = TestClient(create_app())
    client.put("/settings/projects/ra_avm", json={"auto_merge": True})

    # Simulate a backend restart by building a brand-new app instance.
    fresh_client = TestClient(create_app())
    body = fresh_client.get("/settings").json()
    avm = next(p for p in body["projects"] if p["id"] == "ra_avm")
    assert avm["auto_merge"] is True


def test_put_settings_404_on_unknown_project(triage_home: Path) -> None:
    """Unknown project id → 404, no file mutation."""
    client = TestClient(create_app())
    response = client.put(
        "/settings/projects/no_such_project", json={"auto_merge": True}
    )
    assert response.status_code == 404
    assert "no_such_project" in response.json()["detail"]
    # File never created for the bad project.
    assert not (triage_home / "settings.json").exists()


def test_put_settings_with_omitted_body_keeps_existing(triage_home: Path) -> None:
    """Empty body (auto_merge=None) is a no-op + returns current state."""
    client = TestClient(create_app())
    client.put("/settings/projects/ra_avm", json={"auto_merge": True})
    response = client.put("/settings/projects/ra_avm", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["auto_merge"] is True


def test_put_settings_rejects_wrong_type(triage_home: Path) -> None:
    """``auto_merge`` must be a boolean — strings are rejected."""
    client = TestClient(create_app())
    response = client.put(
        "/settings/projects/ra_avm", json={"auto_merge": "yes"}
    )
    assert response.status_code == 422


# --- Behavior contract: toggle is a no-op for merging --------------------


def test_merge_module_does_not_consult_effective_auto_merge() -> None:
    """The auto-merge toggle is persisted but does NOT change merge behavior.

    Per the product plan's explicit non-goal "No auto-merge without
    human confirmation" (and the orchestrator instructions for Agent 13),
    ``orchestrator.merge`` MUST NOT consult :func:`effective_auto_merge`
    or import the settings store. This test guards against accidental
    re-introduction of that coupling.

    We check by importing the module's source text and grep'ing for the
    relevant symbols. If a future agent legitimately wires this up, the
    test will fail and that agent's review can decide to update or remove
    the assertion.
    """
    from pathlib import Path as _Path

    import ra_triage.orchestrator.merge as merge_module

    src_path = _Path(merge_module.__file__)
    src = src_path.read_text(encoding="utf-8")
    assert "effective_auto_merge" not in src, (
        f"{src_path} references effective_auto_merge — v1 forbids the "
        "auto-merge toggle from changing merge behavior. See Agent 13 "
        "instructions and product plan non-goals."
    )
    assert "settings_store" not in src, (
        f"{src_path} imports settings_store — v1 forbids merge behavior "
        "from depending on the settings file."
    )
