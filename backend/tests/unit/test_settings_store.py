"""Tests for ``config.settings_store`` (Agent 13).

Coverage:
  - load() returns empty when the file is missing or empty.
  - save() + load() round-trip preserves the stored projects.
  - update_project() reads existing values, mutates, and persists.
  - update_project() raises KeyError for unknown projects.
  - effective_auto_merge() falls back to the registry default when no
    override is present and prefers the override when one is.
  - Atomic write: a rename failure leaves the previous file untouched.
  - Tolerates unknown keys in the on-disk JSON (logs a warning).

All tests rely on the ``triage_home`` fixture from ``conftest.py`` so
``settings_path()`` resolves into a tmp directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ra_triage.config import settings_store

# pytest is used for the MonkeyPatch fixture and pytest.raises below.
_ = pytest


# --- load() basics --------------------------------------------------------


def test_load_returns_empty_when_file_missing(triage_home: Path) -> None:
    """First-run case: no file on disk → empty StoredSettings, no crash."""
    assert not (triage_home / "settings.json").exists()
    stored = settings_store.load()
    assert stored.projects == {}


def test_load_returns_empty_when_file_is_blank(triage_home: Path) -> None:
    """An empty / whitespace-only file is treated like a missing file."""
    (triage_home / "settings.json").write_text("   \n", encoding="utf-8")
    stored = settings_store.load()
    assert stored.projects == {}


def test_load_returns_empty_on_invalid_json(triage_home: Path) -> None:
    """Garbage on disk doesn't crash the backend — we log + ignore.

    We deliberately don't assert on caplog here because logger
    propagation/levels can be reconfigured by other tests (e.g. ones
    that swap out the root logger config). The behavior contract — load
    returns empty rather than raising — is the load-bearing assertion.
    """
    (triage_home / "settings.json").write_text("not json {{{", encoding="utf-8")
    stored = settings_store.load()
    assert stored.projects == {}


# --- save() + load() round-trip ------------------------------------------


def test_save_then_load_round_trip(triage_home: Path) -> None:
    """Persisting an override and re-reading returns identical content."""
    payload = settings_store.StoredSettings(
        projects={
            "ra_avm": settings_store.ProjectSettings(
                project_id="ra_avm", auto_merge=True
            )
        }
    )
    settings_store.save(payload)

    reloaded = settings_store.load()
    assert "ra_avm" in reloaded.projects
    assert reloaded.projects["ra_avm"].auto_merge is True
    assert reloaded.projects["ra_avm"].project_id == "ra_avm"


def test_save_writes_human_readable_json(triage_home: Path) -> None:
    """The on-disk shape matches the documented schema exactly."""
    settings_store.save(
        settings_store.StoredSettings(
            projects={
                "ra_avm": settings_store.ProjectSettings(
                    project_id="ra_avm", auto_merge=False
                )
            }
        )
    )
    raw = (triage_home / "settings.json").read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert parsed == {"projects": {"ra_avm": {"auto_merge": False}}}


def test_save_uses_atomic_rename(triage_home: Path) -> None:
    """No leftover ``settings.json.*.tmp`` after a successful save."""
    settings_store.save(
        settings_store.StoredSettings(
            projects={
                "ra_avm": settings_store.ProjectSettings(
                    project_id="ra_avm", auto_merge=True
                )
            }
        )
    )
    leftovers = list(triage_home.glob("settings.json.*.tmp"))
    assert leftovers == []


# --- Atomic-write durability ---------------------------------------------


def test_failed_rename_does_not_corrupt_previous_file(
    triage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``os.replace`` fails after the temp file is written, the previous
    settings.json is left intact and the tmp file is cleaned up.

    Patching ``os.replace`` (rather than the file write itself) targets the
    failure mode the atomic-write pattern is meant to defend against:
    crash mid-rename on a full disk or permission flip.
    """
    # Establish a baseline good file.
    settings_store.save(
        settings_store.StoredSettings(
            projects={
                "ra_avm": settings_store.ProjectSettings(
                    project_id="ra_avm", auto_merge=False
                )
            }
        )
    )
    baseline_bytes = (triage_home / "settings.json").read_bytes()

    # Force the rename to blow up the next time it's called.
    def _raise(*args: object, **kwargs: object) -> None:
        raise OSError("simulated disk full")

    monkeypatch.setattr(settings_store.os, "replace", _raise)

    with pytest.raises(OSError, match="simulated disk full"):
        settings_store.save(
            settings_store.StoredSettings(
                projects={
                    "ra_avm": settings_store.ProjectSettings(
                        project_id="ra_avm", auto_merge=True
                    )
                }
            )
        )

    # Previous file untouched.
    assert (triage_home / "settings.json").read_bytes() == baseline_bytes
    # No stray tmp files.
    assert list(triage_home.glob("settings.json.*.tmp")) == []


# --- update_project() ----------------------------------------------------


def test_update_project_writes_new_override_when_file_missing(
    triage_home: Path,
) -> None:
    """First-time toggle creates the file with the new value."""
    result = settings_store.update_project("ra_avm", auto_merge=True)
    assert result.project_id == "ra_avm"
    assert result.auto_merge is True
    assert (triage_home / "settings.json").exists()
    reloaded = settings_store.load()
    assert reloaded.projects["ra_avm"].auto_merge is True


def test_update_project_preserves_other_projects(triage_home: Path) -> None:
    """Updating one project leaves untouched entries untouched.

    We seed the file directly (rather than through ``update_project``)
    with a synthetic second project so we can assert the read-modify-write
    path doesn't drop unknown-but-present overrides.
    """
    (triage_home / "settings.json").write_text(
        json.dumps(
            {
                "projects": {
                    "ra_avm": {"auto_merge": False},
                    "future_project": {"auto_merge": True},
                }
            }
        ),
        encoding="utf-8",
    )

    settings_store.update_project("ra_avm", auto_merge=True)

    reloaded_raw = json.loads(
        (triage_home / "settings.json").read_text(encoding="utf-8")
    )
    assert reloaded_raw["projects"]["ra_avm"]["auto_merge"] is True
    # The synthetic future entry survives the round-trip even though the
    # registry doesn't know about it (we re-serialize what load() saw).
    assert reloaded_raw["projects"]["future_project"]["auto_merge"] is True


def test_update_project_reads_then_modifies_existing(triage_home: Path) -> None:
    """Two updates in sequence keep the latest value, not append."""
    settings_store.update_project("ra_avm", auto_merge=True)
    settings_store.update_project("ra_avm", auto_merge=False)
    reloaded = settings_store.load()
    assert reloaded.projects["ra_avm"].auto_merge is False


def test_update_project_with_none_keeps_existing_value(triage_home: Path) -> None:
    """Passing ``auto_merge=None`` is a no-op for that field."""
    settings_store.update_project("ra_avm", auto_merge=True)
    result = settings_store.update_project("ra_avm", auto_merge=None)
    assert result.auto_merge is True


def test_update_project_raises_keyerror_for_unknown_project(
    triage_home: Path,
) -> None:
    """Refuse to persist overrides for projects not in the registry."""
    with pytest.raises(KeyError):
        settings_store.update_project("not_a_real_project", auto_merge=True)


# --- effective_auto_merge() ----------------------------------------------


def test_effective_auto_merge_falls_back_to_registry_default(
    triage_home: Path,
) -> None:
    """No override on disk → use the hardcoded registry value (False for AVM)."""
    assert settings_store.effective_auto_merge("ra_avm") is False


def test_effective_auto_merge_uses_override_when_set(triage_home: Path) -> None:
    """An override flips the effective value even though the registry default is False."""
    settings_store.update_project("ra_avm", auto_merge=True)
    assert settings_store.effective_auto_merge("ra_avm") is True


def test_registry_auto_merge_default_unchanged_by_overrides(
    triage_home: Path,
) -> None:
    """The registry default is read from the registry, not the file."""
    settings_store.update_project("ra_avm", auto_merge=True)
    assert settings_store.registry_auto_merge_default("ra_avm") is False


# --- Forward-compat: tolerate unknown keys -------------------------------


def test_load_tolerates_unknown_keys(triage_home: Path) -> None:
    """A future v2 toggle that an older backend doesn't recognize is ignored.

    The known field still parses; unknown keys are dropped (and a
    warning is logged at the module logger — we don't assert on caplog
    here because of cross-test logger reconfiguration).
    """
    (triage_home / "settings.json").write_text(
        json.dumps(
            {
                "projects": {
                    "ra_avm": {
                        "auto_merge": True,
                        "future_toggle": "v2-only",
                    }
                },
                "experimental_top_level": 42,
            }
        ),
        encoding="utf-8",
    )
    stored = settings_store.load()
    # Known field still parses.
    assert stored.projects["ra_avm"].auto_merge is True
    # Unknown values are not exposed on the public StoredSettings API.
    assert hasattr(stored.projects["ra_avm"], "future_toggle") is False
