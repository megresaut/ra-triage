# Notion property schema for AVM bug DB (captured by scripts/inspect_notion_db.py)

Status: NOT CAPTURED

Megha must run `python backend/scripts/inspect_notion_db.py` after placing
`NOTION_TOKEN` + `NOTION_BUGS_DB_ID` in `~/code/ra-triage/.env`. Run the
script, paste the JSON output below, commit in a follow-up.

## How to populate

```bash
# From the monorepo root, with ~/code/ra-triage/.env populated:
set -a; source ~/code/ra-triage/.env; set +a
cd backend
uv run python scripts/inspect_notion_db.py
```

The script prints both a human-readable table and a JSON blob. Copy the
full output into the sections below, commit as a follow-up.

## Heuristic defaults in code

Until this file is populated,
`ra_triage.integrations.notion.normalize_page` uses these fallback property
names:

| Logical field | Default Notion property | Type |
|---|---|---|
| Title       | auto-detected (any property with `type == "title"`; default "Name") | title |
| Description | `Description`   | rich_text |
| Repro Steps | `Repro Steps`   | rich_text |
| Severity    | `Severity`      | select (optional) |
| Reporter    | `Reporter`      | people (optional; extracts name/email) |
| Status      | `Status`        | status |

If the real DB's property names differ, override them at startup time
without code changes by setting `NOTION_PROPERTY_OVERRIDES` to a JSON blob
in `.env`, e.g.:

```
NOTION_PROPERTY_OVERRIDES={"description":"Summary","repro_steps":"Steps to Reproduce"}
```

(The keys on the left are fixed logical names; the values on the right are
whatever the DB actually uses.)

## Captured schema

(empty — paste output of `inspect_notion_db.py` here once run)

### Human-readable

```text
(fill in)
```

### JSON

```json
(fill in)
```
