"""Shared pytest fixtures for the RA Triage backend tests.

Strategy: every test that touches the DB gets a fresh SQLite file in a
tmp_path, and we apply the *real* Alembic migration against it so the
migration itself is exercised (not just `models.Base.metadata.create_all`).
This catches drift between the migration and the ORM.

We monkeypatch `TRIAGE_HOME` to the tmp dir so `settings.triage_db_path`
points inside the test's tmp tree, then call `alembic upgrade head`
programmatically. We also override `db.engine.SessionLocal` / `engine` so
in-process code picks up the test DB.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from ra_triage import settings as settings_module
from ra_triage.db import engine as engine_module

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"


def _build_test_engine(db_path: Path) -> Any:
    """Create a SQLAlchemy engine pointed at `db_path` with FK enforcement on."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


@pytest.fixture()
def triage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A fresh TRIAGE_HOME tree for each test. Clears settings cache so
    `get_settings()` re-reads env vars after the monkeypatch."""
    home = tmp_path / "ra-triage"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TRIAGE_HOME", str(home))
    settings_module.get_settings.cache_clear()
    yield home
    settings_module.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_shutdown_flag() -> Iterator[None]:
    """Reset Agent 12's process-wide shutdown flag between tests.

    FastAPI ``TestClient`` enters + exits the lifespan context per test,
    and the lifespan-exit hook calls ``graceful_shutdown`` which trips
    the flag. Without this reset, the next test's pipeline coroutines
    observe the flag and bail at their first checkpoint.

    We reset BEFORE each test (not after) so failures that raise mid-test
    still leave a clean slate for the next one.
    """
    from ra_triage.orchestrator import shutdown as shutdown_module

    shutdown_module.reset_for_tests()
    # Re-import-style refresh: other modules import ``shutdown`` as a
    # name at module import time, so we also swap the attribute on
    # every live import.
    import ra_triage.orchestrator.shutdown as _s  # noqa: F401

    yield


@pytest.fixture()
def alembic_db(triage_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Apply the Alembic migration to a fresh DB inside `triage_home`.

    Also rebinds `db.engine`'s module-level `engine` and `SessionLocal` so that
    `api.bugs.get_session` and `repo` helpers use this test DB.
    """
    db_path = triage_home / "triage.db"
    cfg = AlembicConfig(str(ALEMBIC_INI))
    # env.py reads from get_settings(), which will now resolve TRIAGE_HOME to
    # our tmp dir (via the `triage_home` fixture). The monkeypatch on
    # TRIAGE_HOME plus the settings cache clear handles that.
    command.upgrade(cfg, "head")

    # Rebind the engine/sessionmaker the app uses.
    new_engine = _build_test_engine(db_path)
    new_sessionmaker = sessionmaker(
        bind=new_engine, autoflush=False, autocommit=False, future=True
    )
    monkeypatch.setattr(engine_module, "engine", new_engine)
    monkeypatch.setattr(engine_module, "SessionLocal", new_sessionmaker)
    return db_path


@pytest.fixture()
def db_session(alembic_db: Path) -> Iterator[Session]:
    """Session bound to the migrated test DB."""
    session: Session = engine_module.SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
