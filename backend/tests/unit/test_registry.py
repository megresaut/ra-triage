"""Smoke test: the ra_avm registry entry matches Agent-1 approved deviation."""

from __future__ import annotations

from ra_triage.config.registry import PROJECTS, get_project


def test_ra_avm_project_exists() -> None:
    assert "ra_avm" in PROJECTS
    project = get_project("ra_avm")
    assert project.display_name == "RA AVM"
    assert project.slack_webhook_env == "RA_AVM_SLACK_WEBHOOK"
    assert project.auto_merge is False


def test_ra_avm_has_backend_and_frontend_repos() -> None:
    project = get_project("ra_avm")
    repo_names = {r.name for r in project.repos}
    assert repo_names == {"avm-backend", "builder-frontend"}


def test_ra_avm_backend_repo_config() -> None:
    project = get_project("ra_avm")
    backend = next(r for r in project.repos if r.role == "backend")
    assert backend.name == "avm-backend"
    assert str(backend.path) == "/Users/megkrish/Desktop/ra-avm/dev/avm-backend"
    assert backend.main_branch == "main"
    assert backend.branch_prefix == "triage/"
    assert backend.dev_port == 8080
    assert backend.test_launch_port == 18080
    assert backend.integration_port == 28080
    assert backend.env_file == ".env"
    # AVM has no health endpoint — Agent 5 will leave this None.
    assert backend.health_url is None
    assert backend.github_origin == "github.com/megresaut/avm-backend"


def test_ra_avm_frontend_repo_config() -> None:
    project = get_project("ra_avm")
    frontend = next(r for r in project.repos if r.role == "frontend")
    assert frontend.name == "builder-frontend"
    assert str(frontend.path) == "/Users/megkrish/Desktop/ra-avm/dev/builder_frontend"
    assert frontend.dev_port == 5173
    assert frontend.test_launch_port == 15173
    assert frontend.integration_port == 25173
    assert frontend.env_file == ".env.local"
    assert frontend.health_url is None
    assert frontend.github_origin == "github.com/megresaut/builder_frontend"


def test_ra_avm_deny_list_has_expected_entries() -> None:
    project = get_project("ra_avm")
    # Paths from tech plan §8:
    expected_paths = {
        "**/auth/**",
        "**/security/**",
        "**/payments/**",
        "**/migrations/**",
        "**/.env*",
        "**/schema.sql",
    }
    assert expected_paths.issubset(set(project.deny_list.paths))
    expected_categories = {"schema_change", "migration", "auth", "payments"}
    assert expected_categories.issubset(set(project.deny_list.categories))
