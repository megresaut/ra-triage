#!/usr/bin/env python3
"""Dump the Notion bug DB's property schema for RA AVM.

Outputs a human-readable table AND a JSON blob so Megha can paste both into
`backend/docs/notion-schema-for-avm.md`. That document is the source of
truth for the property names `integrations.notion.normalize_page` looks up;
if the real DB deviates from the heuristic defaults, set
`NOTION_PROPERTY_OVERRIDES` (JSON in env) — no code edits required.

Usage:

    # With env vars in your shell:
    uv run python backend/scripts/inspect_notion_db.py

    # Or sourced from ~/code/ra-triage/.env:
    set -a; source ~/code/ra-triage/.env; set +a
    uv run python backend/scripts/inspect_notion_db.py

Exits 2 if NOTION_TOKEN or NOTION_BUGS_DB_ID is missing (so Megha sees a
clear error rather than a cryptic Notion 401).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# Allow running this script without `uv run` via direct python invocation —
# the `src/` layout isn't automatically on sys.path outside of the installed
# package. Makes the "did you set creds?" check friendlier.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _err(msg: str, code: int = 2) -> None:
    """Print an error and exit with the given code."""
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _format_property(name: str, prop: dict[str, Any]) -> dict[str, Any]:
    """Produce a JSON-friendly summary of one Notion property definition."""
    prop_type = prop.get("type", "unknown")
    summary: dict[str, Any] = {
        "name": name,
        "type": prop_type,
        "id": prop.get("id"),
    }
    # Pull option lists for select / status / multi_select properties.
    for option_field in ("select", "status", "multi_select"):
        value = prop.get(option_field)
        if isinstance(value, dict):
            options = value.get("options") or []
            summary["options"] = [
                str(o.get("name")) for o in options if isinstance(o, dict)
            ]
            # Notion's `status` also has `groups` — include names for ref.
            if option_field == "status":
                groups = value.get("groups") or []
                summary["status_groups"] = [
                    str(g.get("name")) for g in groups if isinstance(g, dict)
                ]
            break
    return summary


def main() -> int:
    token = os.environ.get("NOTION_TOKEN", "").strip()
    db_id = os.environ.get("NOTION_BUGS_DB_ID", "").strip()
    if not token or not db_id:
        _err("Set NOTION_TOKEN and NOTION_BUGS_DB_ID to run this.")

    try:
        from ra_triage.integrations.notion import NotionClient
    except Exception as exc:
        _err(
            "Failed to import NotionClient. Did you run `uv sync`? "
            f"Underlying error: {exc}"
        )
        return 2  # unreachable; appeases the type checker

    client = NotionClient(token=token)
    try:
        db = client._sdk.databases.retrieve(database_id=db_id)  # noqa: SLF001
    except Exception as exc:
        _err(f"databases.retrieve failed: {exc}")
        return 2  # unreachable

    if not isinstance(db, dict):
        _err(f"Unexpected response type from Notion: {type(db).__name__}")
        return 2  # unreachable

    properties = db.get("properties") or {}
    if not isinstance(properties, dict):
        _err("Notion response has no 'properties' dict.")
        return 2  # unreachable

    summaries = [
        _format_property(name, prop)
        for name, prop in properties.items()
        if isinstance(prop, dict)
    ]
    summaries.sort(key=lambda s: (str(s.get("type")), str(s.get("name"))))

    # Human-readable table.
    print("# Notion DB property schema")
    print(f"database_id: {db_id}")
    title_prop = next((s["name"] for s in summaries if s["type"] == "title"), None)
    print(f"title property (detected): {title_prop!r}")
    print()
    print(f"{'NAME':<30} {'TYPE':<16} OPTIONS / NOTES")
    print("-" * 80)
    for s in summaries:
        options = s.get("options")
        notes = ", ".join(options) if options else ""
        print(f"{str(s['name']):<30} {str(s['type']):<16} {notes}")

    # JSON blob suitable for pasting.
    print()
    print("# JSON")
    print(json.dumps({"database_id": db_id, "properties": summaries}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
