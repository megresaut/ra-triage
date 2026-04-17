"""Integration-ish tests for ``test_runner.db_snapshot``.

These tests exercise real Postgres commands. They're skipped automatically
on machines without ``psql`` / ``pg_dump`` etc. — on Megha's machine we
expect all of them to run. Each test creates a throwaway pair of DBs under
a unique-per-pid name so concurrent test runs can't step on each other, and
cleans up in a ``finally``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import pytest

from ra_triage.test_runner import db_snapshot

_CLIS = ("psql", "pg_dump", "pg_restore", "createdb", "dropdb")

pytestmark = pytest.mark.skipif(
    any(shutil.which(c) is None for c in _CLIS),
    reason="Postgres client tools not on PATH; skipping db_snapshot tests",
)


def _pg_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PGPASSWORD", "")
    return env


def _db_url(db: str, user: str = "megkrish") -> str:
    """Local trust-auth URL for ``db`` on 127.0.0.1:5432.

    Uses ``postgresql://`` rather than ``postgres://`` because pg_dump
    rejects the short form as an "invalid connection option" (libpq-specific
    quirk — both schemes are accepted by psql).
    """
    return f"postgresql://{quote(user)}@localhost:5432/{db}?sslmode=disable"


def _createdb(db: str) -> None:
    subprocess.run(
        ["createdb", db, "-h", "localhost", "-U", "megkrish"],
        check=True,
        env=_pg_env(),
        capture_output=True,
        text=True,
    )


def _dropdb(db: str) -> None:
    subprocess.run(
        ["dropdb", "--if-exists", db, "-h", "localhost", "-U", "megkrish"],
        check=False,
        env=_pg_env(),
        capture_output=True,
        text=True,
    )


def _exec_sql(url: str, sql: str) -> str:
    completed = subprocess.run(
        ["psql", "-tAc", sql, url],
        check=True,
        env=_pg_env(),
        capture_output=True,
        text=True,
    )
    return completed.stdout


@pytest.fixture()
def dev_db(triage_home: Path) -> Iterator[str]:
    """Create a tiny throwaway 'dev' DB, yield its URL, drop on teardown.

    Returns a plain string — do not wrap in Path() because Path normalizes
    the ``//`` in ``postgresql://`` to a single slash, which pg_dump rejects.
    """
    _ = triage_home  # forces the fixture to monkeypatch TRIAGE_HOME
    pid = os.getpid()
    db = f"ra_triage_snapshot_test_{pid}"
    _dropdb(db)
    _createdb(db)
    url = _db_url(db)
    try:
        _exec_sql(
            url,
            "CREATE TABLE widgets (id serial primary key, name text not null);",
        )
        _exec_sql(url, "INSERT INTO widgets (name) VALUES ('alpha'), ('beta');")
        yield url
    finally:
        _dropdb(db)


def test_compute_dev_hash_is_deterministic(dev_db: str) -> None:
    """Hashing the same DB twice yields the same short digest."""
    url = dev_db
    h1 = db_snapshot.compute_dev_hash(url)
    h2 = db_snapshot.compute_dev_hash(url)
    assert h1 == h2
    assert len(h1) == 12


def test_snapshot_dev_db_is_cache_hit_on_repeat(dev_db: str) -> None:
    """Second call without data changes returns the same path without re-dumping."""
    url = dev_db
    first = db_snapshot.snapshot_dev_db("ra_avm", url)
    assert first.exists()
    first_mtime = first.stat().st_mtime

    second = db_snapshot.snapshot_dev_db("ra_avm", url)
    assert second == first
    # Repeat calls MUST NOT re-dump — we check the mtime to prove it.
    assert second.stat().st_mtime == first_mtime


def test_restore_test_db_reseeds_from_snapshot(dev_db: str) -> None:
    """Full round-trip: dump dev, drop/create test, restore. Row comes back."""
    dev_url = dev_db
    pid = os.getpid()
    test_db = f"ra_triage_snapshot_restore_test_{pid}"
    test_url = _db_url(test_db)

    try:
        digest = db_snapshot.restore_test_db(
            "ra_avm",
            dev_url,
            test_url,
            test_db_name=test_db,
        )
        assert digest
        # Data should mirror dev's seed rows.
        out = _exec_sql(test_url, "SELECT count(*) FROM widgets;")
        assert out.strip() == "2"
    finally:
        _dropdb(test_db)


def test_restore_is_idempotent_across_calls(dev_db: str) -> None:
    """Two consecutive restores both succeed (dropdb --if-exists handles recreate)."""
    dev_url = dev_db
    pid = os.getpid()
    test_db = f"ra_triage_snapshot_restore_test_idem_{pid}"
    test_url = _db_url(test_db)

    try:
        db_snapshot.restore_test_db("ra_avm", dev_url, test_url, test_db_name=test_db)
        db_snapshot.restore_test_db("ra_avm", dev_url, test_url, test_db_name=test_db)
        out = _exec_sql(test_url, "SELECT count(*) FROM widgets;")
        assert out.strip() == "2"
    finally:
        _dropdb(test_db)


def test_snapshot_error_surfaces_bad_url() -> None:
    """A clearly-unreachable DB URL raises SnapshotError, not a raw subprocess error."""
    with pytest.raises(db_snapshot.SnapshotError):
        db_snapshot.compute_dev_hash(
            "postgresql://nouser@localhost:5432/this_db_does_not_exist?sslmode=disable"
        )
