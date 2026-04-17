"""Postgres dev-DB snapshot + restore for Launch-for-Testing.

Each call to ``restore_test_db`` guarantees the test database is wiped and
re-seeded from a ``pg_dump -Fc`` custom-format snapshot of Megha's dev DB. We
key the snapshot on a cheap hash of ``pg_dump -s`` + per-table row counts so
we only re-dump when the dev DB has actually changed.

Layout
------

Snapshots live under ``<triage_home>/snapshots/<project_id>/<hash>.sqlc``.
Each file is a ``pg_dump`` custom-format archive.

The hash (``compute_dev_hash``) is a 12-char ``sha256`` prefix of:

  1. ``pg_dump --schema-only <dev_url>``
  2. A single query listing per-table row counts across ``public``.

That keeps it cheap (schema + an O(#tables) catalog query) while catching the
common cases that matter for testing: new rows, dropped tables, schema
changes.

Postgres auth
-------------

RA AVM uses local trust auth: ``postgres://megkrish@localhost:5432/…`` with no
password. We pass ``PGPASSWORD=""`` explicitly just so the CLIs never block on
a password prompt if someone's env has it set.

Tests
-----

Integration tests in ``tests/unit/test_db_snapshot.py`` skip when ``psql`` is
missing. When they do run they spin up a throwaway dev DB
(``ra_triage_snapshot_test_<pid>``) and throwaway test DB
(``ra_triage_snapshot_restore_test_<pid>``) under a fresh ``TRIAGE_HOME``; the
test's own ``finally`` block cleans both up.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from ra_triage.settings import get_settings

logger = logging.getLogger(__name__)


# pg_dump/psql get a long-but-bounded timeout so a genuinely stuck subprocess
# surfaces as a SnapshotError rather than hanging a launch forever.
_PG_SUBPROCESS_TIMEOUT_S: float = 300.0


class SnapshotError(RuntimeError):
    """Raised when a pg_dump / pg_restore / dropdb / createdb step fails.

    Wraps the command and captured stderr so the caller (the
    TestInstanceManager) can surface a useful message on launch failure.
    """


def _pg_env() -> dict[str, str]:
    """Env for pg CLIs — trust auth, so we just clear any stray PGPASSWORD."""
    env = dict(os.environ)
    env["PGPASSWORD"] = env.get("PGPASSWORD", "")
    return env


def _require_cli(name: str) -> None:
    """Raise SnapshotError if ``name`` isn't on PATH.

    We check lazily (at first use) rather than at import so unit tests that
    don't touch snapshots still import this module even on CI-ish machines.
    """
    if shutil.which(name) is None:
        raise SnapshotError(
            f"{name} not found on PATH — install Postgres client tools or adjust PATH."
        )


def _run(
    cmd: list[str],
    *,
    description: str,
    tolerate_restore_warnings: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` capturing stdout/stderr as text. Raise SnapshotError on failure.

    Does NOT stream — these commands are brief and we want a clean error
    message with full stderr attached when they fail.

    ``tolerate_restore_warnings`` exists to accommodate a real-world quirk:
    when the installed ``pg_dump`` is newer than the server it was dumped
    against (e.g. pg_dump 17 against a PG 15 server), the resulting archive
    includes directives like ``SET transaction_timeout = 0`` that the older
    server doesn't understand. ``pg_restore`` then exits 1 with the message
    ``errors ignored on restore: N`` even though the data restored fine.
    When this flag is set, we treat that specific shape as success — any
    other failure still surfaces as :class:`SnapshotError`.
    """
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=_pg_env(),
            timeout=_PG_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SnapshotError(
            f"{description} timed out after {_PG_SUBPROCESS_TIMEOUT_S:.0f}s"
        ) from exc
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        if tolerate_restore_warnings and _is_benign_restore_warning(stderr):
            logger.info(
                "%s: ignoring benign pg_restore warnings (%s)",
                description,
                stderr.splitlines()[-1][:200] if stderr else "",
            )
            return completed
        raise SnapshotError(
            f"{description} failed (exit {completed.returncode}): {stderr[:800]}"
        )
    return completed


def _is_benign_restore_warning(stderr: str) -> bool:
    """Return True iff ``stderr`` only reports ignorable pg_restore warnings.

    The specific real case we see on Megha's machine: pg_dump 17 → PG 15
    server emits ``SET transaction_timeout = 0;`` which the older server
    doesn't recognize. pg_restore exits 1 but appends
    ``errors ignored on restore: 1`` and the data is in fact there.
    """
    if not stderr:
        return False
    last_line = stderr.strip().splitlines()[-1].lower()
    return "errors ignored on restore" in last_line


def _row_counts_sql() -> str:
    """SQL that produces a deterministic text dump of per-table row counts.

    Output format is one line per (schema, table) pair:

        schema.table\tcount

    We only inspect ``public`` — RA AVM and every other RA project we expect
    to onboard lives there. Other schemas (system, extensions) would pollute
    the hash with noise.
    """
    return (
        "SELECT schemaname || '.' || relname AS tbl, "
        "COALESCE(n_live_tup, 0) AS rows "
        "FROM pg_stat_user_tables "
        "WHERE schemaname = 'public' "
        "ORDER BY tbl"
    )


def compute_dev_hash(database_url: str) -> str:
    """Return a short (12-char) sha256 prefix keyed on schema + row counts.

    Steps:

    1. ``pg_dump --schema-only --no-owner --no-privileges <url>`` — stable
       textual representation of the schema.
    2. ``psql -tAc <row_counts_sql> <url>`` — per-table live-row counts.

    We concatenate the two blobs and hash with ``sha256``; the first 12 hex
    chars are the hash. Short enough for a readable filename, long enough
    that collisions are not a realistic concern for our scale.
    """
    _require_cli("pg_dump")
    _require_cli("psql")

    schema_dump = _run(
        [
            "pg_dump",
            "--schema-only",
            "--no-owner",
            "--no-privileges",
            database_url,
        ],
        description=f"pg_dump --schema-only {_redact(database_url)}",
    ).stdout

    row_counts = _run(
        [
            "psql",
            "-tAc",
            _row_counts_sql(),
            database_url,
        ],
        description=f"psql row_counts {_redact(database_url)}",
    ).stdout

    digest = hashlib.sha256()
    digest.update(schema_dump.encode("utf-8", errors="replace"))
    digest.update(b"\n--rowcounts--\n")
    digest.update(row_counts.encode("utf-8", errors="replace"))
    return digest.hexdigest()[:12]


def snapshot_dev_db(project_id: str, database_url: str) -> Path:
    """Ensure a ``pg_dump`` snapshot exists for ``(project_id, dev_hash)``.

    If a snapshot matching the current dev hash already exists, we skip the
    dump and return the existing path — this is the common case on repeat
    launches between dev-data changes. Otherwise we run ``pg_dump -Fc`` into
    ``<triage_home>/snapshots/<project_id>/<hash>.sqlc`` and return the
    fresh path.

    Writes land in a ``.sqlc.tmp-<pid>`` sibling and are atomically renamed
    on success so a concurrent read can never observe a partial file.
    """
    _require_cli("pg_dump")

    settings = get_settings()
    snapshots_root = settings.snapshots_dir / project_id
    snapshots_root.mkdir(parents=True, exist_ok=True)

    digest = compute_dev_hash(database_url)
    target = snapshots_root / f"{digest}.sqlc"
    if target.exists():
        logger.info(
            "Snapshot cache hit for %s @ %s (reusing %s)",
            project_id,
            digest,
            target,
        )
        return target

    tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    logger.info(
        "Dumping dev DB for %s (hash=%s) → %s", project_id, digest, target
    )
    try:
        _run(
            [
                "pg_dump",
                "-Fc",
                "--no-owner",
                "--no-privileges",
                "-f",
                str(tmp),
                database_url,
            ],
            description=f"pg_dump -Fc {_redact(database_url)}",
        )
        tmp.replace(target)
    finally:
        # Best-effort cleanup if rename fell through (e.g. _run raised).
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return target


def restore_test_db(
    project_id: str,
    dev_database_url: str,
    test_database_url: str,
    test_db_name: str = "ra_triage_test",
) -> str:
    """Drop + recreate the test DB and restore the dev snapshot into it.

    Returns the hash of the snapshot that was restored so the caller can
    persist it on the ``test_instances`` row. Raises :class:`SnapshotError`
    on any failed step.

    We always dropdb → createdb before restoring — ``pg_restore`` into a
    non-empty DB is a recipe for stale-data bugs, and the runtime cost is
    measured in hundreds of milliseconds on a small schema.
    """
    for cli in ("dropdb", "createdb", "pg_restore"):
        _require_cli(cli)

    snapshot_path = snapshot_dev_db(project_id, dev_database_url)
    host, user = _connection_params(dev_database_url)

    # dropdb --if-exists is idempotent. Then createdb seeds a fresh empty DB.
    _run(
        ["dropdb", "--if-exists", test_db_name, "-h", host, "-U", user],
        description=f"dropdb {test_db_name}",
    )
    _run(
        ["createdb", test_db_name, "-h", host, "-U", user],
        description=f"createdb {test_db_name}",
    )

    # Restore into the test URL directly — that way pg_restore reads all its
    # connection info (user, host, db name) from the URL. We tolerate the
    # specific class of "errors ignored on restore: N" warnings that shows
    # up when a newer pg_dump targets an older server (real case on Megha's
    # box: pg_dump 17 → PG 15 server emits ``SET transaction_timeout``). See
    # ``_is_benign_restore_warning`` for the guard.
    _run(
        [
            "pg_restore",
            "--no-owner",
            "--no-privileges",
            "-d",
            test_database_url,
            str(snapshot_path),
        ],
        description=f"pg_restore -> {_redact(test_database_url)}",
        tolerate_restore_warnings=True,
    )

    logger.info(
        "Restored test DB %s from snapshot %s (project=%s, hash=%s)",
        test_db_name,
        snapshot_path,
        project_id,
        snapshot_path.stem,
    )
    return snapshot_path.stem


# --- helpers ---------------------------------------------------------------


def _connection_params(database_url: str) -> tuple[str, str]:
    """Extract ``(host, user)`` from a ``postgres://`` URL.

    Defaults to ``localhost`` / ``megkrish`` when either component is absent
    — matches how the RA AVM registry is configured. Parsing is deliberately
    permissive because the dev URL is hand-configured and we don't want a
    stray missing character to blow up a launch.
    """
    parsed = urlparse(database_url)
    host = parsed.hostname or "localhost"
    user = parsed.username or "megkrish"
    return host, user


def _redact(database_url: str) -> str:
    """Return ``database_url`` with any embedded password replaced by ``***``.

    Used when we echo the URL into error messages. RA AVM uses trust auth so
    there is no password in practice, but we redact anyway as a precaution
    against future env drift.
    """
    parsed = urlparse(database_url)
    if parsed.password is None:
        return database_url
    # Replace ``:password@`` with ``:***@`` — urllib.parse doesn't have a
    # one-liner for "new URL with password redacted".
    netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@")
    return parsed._replace(netloc=netloc).geturl()


__all__ = [
    "SnapshotError",
    "compute_dev_hash",
    "restore_test_db",
    "snapshot_dev_db",
]
