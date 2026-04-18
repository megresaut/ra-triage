"""Per-project settings overrides persisted to a JSON file.

Agent 13 design choice: rather than introducing a new Alembic table for
the v1 settings surface (currently a single ``auto_merge`` toggle per
project), we persist overrides as a small JSON file at
``<TRIAGE_HOME>/settings.json``. Reasoning:

  - **Simpler than a migration.** Adding the file is a zero-DDL change
    that can't break ``alembic upgrade head`` for existing installs.
  - **Inspectable / restoreable.** Megha can ``cat`` it, edit it, or
    ``rm`` it to clear all overrides — no SQL required.
  - **Matches the access pattern.** Reads happen on every settings GET
    and on the (no-op for v1) ``effective_auto_merge`` lookup; writes
    happen only when Megha clicks a toggle. Single-writer, infrequent,
    small payload — a JSON file is plenty.

The schema is intentionally narrow:

.. code-block:: json

    {
      "projects": {
        "ra_avm": {"auto_merge": true}
      }
    }

Unknown keys at the project level are tolerated (we log a warning) so a
future v2 toggle that an older backend doesn't recognize doesn't trip a
load-time crash. Pydantic enforces the known shape.

**Important non-behavior contract:** :func:`effective_auto_merge` exists
so the API can report a per-project override, but ``orchestrator/merge.py``
does NOT consult it in v1 — the product plan's "No auto-merge without
human confirmation" non-goal explicitly forbids changing merge behavior
based on the toggle. The toggle is persisted for v2.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ra_triage.config.registry import PROJECTS, get_project
from ra_triage.settings import get_settings

logger = logging.getLogger(__name__)

# Filename inside <TRIAGE_HOME>/. Resolved lazily via ``settings_path()`` so
# tests can monkeypatch ``TRIAGE_HOME`` and have writes land in tmp_path.
SETTINGS_FILENAME = "settings.json"


# --- Public dataclasses --------------------------------------------------


@dataclass
class ProjectSettings:
    """Persisted overrides for a single project.

    Currently only ``auto_merge`` is meaningful in v1. Add fields here as
    new toggles ship.
    """

    project_id: str
    auto_merge: bool


@dataclass
class StoredSettings:
    """Top-level shape of ``settings.json``.

    ``projects`` is keyed by project_id (matches the registry key) so we
    can do O(1) lookups without scanning a list.
    """

    projects: dict[str, ProjectSettings] = field(default_factory=dict)


# --- Pydantic validation models -------------------------------------------
#
# We separate these from the public dataclasses so the file schema can
# evolve (e.g. add new optional fields) without forcing every caller to
# adopt Pydantic semantics.


class _ProjectSettingsModel(BaseModel):
    """On-disk shape of a single project's settings entry."""

    # Tolerate unknown keys for forward-compat (we log a warning at load).
    model_config = ConfigDict(extra="allow")

    auto_merge: bool = Field(default=False)


class _StoredSettingsModel(BaseModel):
    """On-disk shape of the whole ``settings.json`` document."""

    model_config = ConfigDict(extra="allow")

    projects: dict[str, _ProjectSettingsModel] = Field(default_factory=dict)


# --- Path helpers --------------------------------------------------------


def settings_path() -> Path:
    """Return the absolute path to ``<TRIAGE_HOME>/settings.json``.

    Resolved lazily (per-call) because tests monkeypatch ``TRIAGE_HOME``
    and the cached settings singleton mid-run.
    """
    return Path(get_settings().triage_home).expanduser() / SETTINGS_FILENAME


# --- Load / save ---------------------------------------------------------


def load() -> StoredSettings:
    """Read ``settings.json`` and return the parsed :class:`StoredSettings`.

    Returns an empty ``StoredSettings`` if:
      - The file does not exist (first run).
      - The file exists but is empty.

    Logs a warning (and returns empty) if the file is present but
    unparseable / fails Pydantic validation — we never want a corrupt
    settings file to take down the whole backend.
    """
    path = settings_path()
    if not path.exists():
        return StoredSettings()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("settings_store: failed to read %s: %s", path, exc)
        return StoredSettings()
    if not raw.strip():
        return StoredSettings()
    try:
        parsed_json = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "settings_store: %s is not valid JSON (%s); ignoring", path, exc
        )
        return StoredSettings()
    try:
        model = _StoredSettingsModel.model_validate(parsed_json)
    except Exception as exc:  # pydantic ValidationError, etc.
        logger.warning(
            "settings_store: %s failed schema validation (%s); ignoring", path, exc
        )
        return StoredSettings()

    # Surface tolerated-but-unrecognized keys at WARN level so a v2 toggle
    # someone added by hand doesn't silently disappear on the next save.
    extras = getattr(model, "model_extra", None) or {}
    if extras:
        logger.warning(
            "settings_store: unknown top-level keys in %s ignored: %s",
            path,
            sorted(extras.keys()),
        )

    projects: dict[str, ProjectSettings] = {}
    for pid, pmodel in model.projects.items():
        pextras = getattr(pmodel, "model_extra", None) or {}
        if pextras:
            logger.warning(
                "settings_store: unknown keys for project %r in %s ignored: %s",
                pid,
                path,
                sorted(pextras.keys()),
            )
        projects[pid] = ProjectSettings(
            project_id=pid,
            auto_merge=bool(pmodel.auto_merge),
        )
    return StoredSettings(projects=projects)


def save(s: StoredSettings) -> None:
    """Atomically persist ``s`` to ``settings.json``.

    Uses the standard write-temp-then-rename pattern so a crash mid-write
    leaves the previous file untouched (POSIX rename is atomic on the
    same filesystem). The temp file is created in the same directory so
    the rename never crosses filesystems.
    """
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, object] = {
        "projects": {
            pid: {"auto_merge": ps.auto_merge}
            for pid, ps in sorted(s.projects.items())
        }
    }
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    # tempfile.NamedTemporaryFile leaves the file behind on close because
    # we set delete=False; we then atomically rename it into place.
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        tmp.write(serialized)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, path)
    except Exception:
        # Clean up the temp file on any failure path so we don't litter
        # ``<TRIAGE_HOME>/`` with stranded ``settings.json.*.tmp`` files.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


# --- Mutation ------------------------------------------------------------


def update_project(
    project_id: str, *, auto_merge: bool | None = None
) -> ProjectSettings:
    """Read-modify-write the settings for one project.

    Raises :class:`KeyError` if ``project_id`` is not in the registry —
    we refuse to persist overrides for projects we don't recognize, since
    those values would be unreachable via the registry-driven API.

    Currently only ``auto_merge`` is mutable; passing ``None`` leaves the
    existing override (or the registry default, when no override is set)
    in place. Returns the resulting :class:`ProjectSettings` reflecting
    what was just written.
    """
    if project_id not in PROJECTS:
        raise KeyError(project_id)

    stored = load()
    existing = stored.projects.get(project_id)
    base_auto_merge = (
        existing.auto_merge
        if existing is not None
        else _registry_auto_merge_default(project_id)
    )

    new_auto_merge = base_auto_merge if auto_merge is None else bool(auto_merge)
    updated = ProjectSettings(project_id=project_id, auto_merge=new_auto_merge)
    stored.projects[project_id] = updated
    save(stored)
    return updated


# --- Effective lookups ---------------------------------------------------


def effective_auto_merge(project_id: str) -> bool:
    """Return the per-project ``auto_merge`` override, or the registry default.

    The order of precedence is:
      1. ``settings.json`` override for the project, if any.
      2. ``ProjectConfig.auto_merge`` from the hardcoded registry.

    .. warning::

       This function is intentionally **not** consulted by
       ``orchestrator/merge.py`` in v1 — the product plan's explicit
       non-goal "No auto-merge without human confirmation" forbids
       changing merge behavior based on this toggle. The function exists
       so the settings API can report what the override would do once
       v2 wires it up, and so the toggle can persist across restarts.
    """
    stored = load()
    override = stored.projects.get(project_id)
    if override is not None:
        return override.auto_merge
    return _registry_auto_merge_default(project_id)


def registry_auto_merge_default(project_id: str) -> bool:
    """Public accessor for the registry's hardcoded ``auto_merge`` value.

    Raises :class:`KeyError` if the project is unknown. Used by the API
    so the response can show both the effective and the default value
    side-by-side.
    """
    return _registry_auto_merge_default(project_id)


def _registry_auto_merge_default(project_id: str) -> bool:
    return get_project(project_id).auto_merge


__all__ = [
    "SETTINGS_FILENAME",
    "ProjectSettings",
    "StoredSettings",
    "effective_auto_merge",
    "load",
    "registry_auto_merge_default",
    "save",
    "settings_path",
    "update_project",
]
