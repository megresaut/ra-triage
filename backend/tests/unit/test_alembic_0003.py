"""Smoke test for the 0003_bug_needs_manual_review Alembic migration.

The shared ``alembic_db`` fixture in ``tests/conftest.py`` runs
``alembic upgrade head`` against a fresh tmp DB. After that completes, the
``bugs`` table must include the ``needs_manual_review`` column (Agent 12's
schema addition) with a ``False`` (0) default.

We also exercise the column round-trip and confirm an INSERT that omits
the column gets the default ``0``.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text

from ra_triage.db import engine as engine_module


def test_migration_0003_adds_needs_manual_review_column(
    alembic_db: Path,
) -> None:
    """``bugs.needs_manual_review`` exists after ``alembic upgrade head``."""
    _ = alembic_db
    inspector = inspect(engine_module.engine)
    cols = {col["name"] for col in inspector.get_columns("bugs")}
    assert "needs_manual_review" in cols, (
        "Migration 0003 should have added bugs.needs_manual_review"
    )


def test_migration_0003_default_is_zero(alembic_db: Path) -> None:
    """An insert without ``needs_manual_review`` defaults to 0 (False)."""
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
                "npid": "page-mig-0003-default",
                "pid": "ra_avm",
                "title": "Default flag",
                "desc": "—",
                "state": "OPEN",
            },
        )
        row = conn.execute(
            text(
                "SELECT needs_manual_review FROM bugs WHERE notion_page_id = :npid"
            ),
            {"npid": "page-mig-0003-default"},
        ).fetchone()

    assert row is not None
    # SQLite stores BOOLEAN as 0/1 INTEGER under the hood.
    assert row[0] == 0


def test_migration_0003_round_trip_true(alembic_db: Path) -> None:
    """Round-trip: stash needs_manual_review=1, read back as truthy."""
    _ = alembic_db

    with engine_module.engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO bugs (
                    notion_page_id, project_id, title, description,
                    state, needs_manual_review
                ) VALUES (
                    :npid, :pid, :title, :desc, :state, :nmr
                )
                """
            ),
            {
                "npid": "page-mig-0003-true",
                "pid": "ra_avm",
                "title": "Needs review",
                "desc": "—",
                "state": "READY_FOR_REVIEW",
                "nmr": 1,
            },
        )
        row = conn.execute(
            text(
                "SELECT needs_manual_review FROM bugs WHERE notion_page_id = :npid"
            ),
            {"npid": "page-mig-0003-true"},
        ).fetchone()

    assert row is not None
    assert row[0] == 1


def test_migration_0003_orm_round_trip(alembic_db: Path) -> None:
    """The ORM-level model exposes ``needs_manual_review`` as a Python bool."""
    _ = alembic_db
    from ra_triage.db import repo as repo_module

    session = engine_module.SessionLocal()
    try:
        bug = repo_module.create_bug(
            session,
            notion_page_id="page-mig-0003-orm",
            project_id="ra_avm",
            title="ORM round-trip",
            description="—",
        )
        session.commit()
        # Default is False on a fresh insert.
        assert bug.needs_manual_review is False or bug.needs_manual_review == 0
        bug.needs_manual_review = True
        session.commit()
        reloaded = repo_module.get_bug(session, bug.id)
        assert reloaded is not None
        assert bool(reloaded.needs_manual_review) is True
    finally:
        session.close()
