"""Smoke test for the 0002_bug_conflict_files Alembic migration.

The shared ``alembic_db`` fixture in ``tests/conftest.py`` runs
``alembic upgrade head`` against a fresh tmp DB. After that completes, the
``bugs`` table must include the ``conflict_files`` column (Agent 11's
schema addition).

We also exercise the column round-trip: insert a bug with a JSON-encoded
file list in ``conflict_files``, read it back, assert the JSON parses to
the original list. This guards against future migrations that drop or
rename the column.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import inspect, text

from ra_triage.db import engine as engine_module


def test_migration_0002_adds_bugs_conflict_files_column(
    alembic_db: Path,
) -> None:
    """``bugs.conflict_files`` exists after ``alembic upgrade head``."""
    _ = alembic_db
    inspector = inspect(engine_module.engine)
    cols = {col["name"] for col in inspector.get_columns("bugs")}
    assert "conflict_files" in cols, (
        "Migration 0002 should have added bugs.conflict_files"
    )


def test_migration_0002_column_is_nullable_text_round_trip(
    alembic_db: Path,
) -> None:
    """Round-trip: stash JSON in conflict_files, read back, parse."""
    _ = alembic_db

    expected_files = ["src/InvoiceForm.tsx", "src/invoiceApi.ts"]

    with engine_module.engine.begin() as conn:
        # Use a raw insert so we don't depend on the ORM model's defaults.
        conn.execute(
            text(
                """
                INSERT INTO bugs (
                    notion_page_id, project_id, title, description,
                    state, conflict_files
                ) VALUES (
                    :npid, :pid, :title, :desc, :state, :cf
                )
                """
            ),
            {
                "npid": "page-mig-0002",
                "pid": "ra_avm",
                "title": "Round-trip conflict_files",
                "desc": "—",
                "state": "BLOCKED",
                "cf": json.dumps(expected_files),
            },
        )
        row = conn.execute(
            text(
                "SELECT conflict_files FROM bugs WHERE notion_page_id = :npid"
            ),
            {"npid": "page-mig-0002"},
        ).fetchone()

    assert row is not None
    assert json.loads(row[0]) == expected_files


def test_migration_0002_column_defaults_to_null(alembic_db: Path) -> None:
    """An insert without ``conflict_files`` leaves it NULL, not empty string."""
    _ = alembic_db
    with engine_module.engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO bugs (
                    notion_page_id, project_id, title, description, state
                ) VALUES (:npid, :pid, :title, :desc, :state)
                """
            ),
            {
                "npid": "page-mig-0002-null",
                "pid": "ra_avm",
                "title": "No conflict",
                "desc": "—",
                "state": "OPEN",
            },
        )
        row = conn.execute(
            text(
                "SELECT conflict_files FROM bugs WHERE notion_page_id = :npid"
            ),
            {"npid": "page-mig-0002-null"},
        ).fetchone()

    assert row is not None
    assert row[0] is None
