"""Unit tests for `path_hits_deny_list` against the RA AVM deny-list."""

from __future__ import annotations

import pytest

from ra_triage.config.deny_list import category_hits_deny_list, path_hits_deny_list
from ra_triage.config.registry import get_project


@pytest.fixture()
def deny_list() -> object:
    return get_project("ra_avm").deny_list


@pytest.mark.parametrize(
    "path",
    [
        "api/auth/login.go",
        "src/security/middleware.ts",
        "server/payments/stripe.go",
        "backend/migrations/001_init.sql",
        ".env",
        "api/.env.production",
        "schema.sql",
        "api/db/schema.sql",
    ],
)
def test_denied_paths_are_rejected(path: str, deny_list: object) -> None:
    assert path_hits_deny_list(path, deny_list) is True  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "path",
    [
        "api/handlers/bugs.go",
        "src/components/BugRow.tsx",
        "frontend/lib/api.ts",
        "cmd/server/main.go",
        "README.md",
    ],
)
def test_allowed_paths_pass(path: str, deny_list: object) -> None:
    assert path_hits_deny_list(path, deny_list) is False  # type: ignore[arg-type]


def test_categories_enforced(deny_list: object) -> None:
    assert category_hits_deny_list("migration", deny_list) is True  # type: ignore[arg-type]
    assert category_hits_deny_list("schema_change", deny_list) is True  # type: ignore[arg-type]
    assert category_hits_deny_list("refactor", deny_list) is False  # type: ignore[arg-type]
