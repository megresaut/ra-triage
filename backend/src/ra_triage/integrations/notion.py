"""Notion integration for RA Triage.

Wraps the official `notion-client` Python SDK with the few operations the
Triage pipeline actually needs:

  - `list_open_bugs(database_id)` — poll the AVM bug DB for rows with
    `Status == "Open"`. Handles pagination.
  - `fetch_page(page_id)` — one-shot page fetch for the manual-bypass
    `/poller/enqueue` endpoint (Agent 3 wires this in).
  - `set_status(page_id, notion_status)` — update a page's Status property.
    Used by the write-back hook in `db.repo.update_bug_state`.
  - `append_block(page_id, markdown_text)` — append a markdown fragment as
    Notion block children. Used by Agent 8 (Fixed PR link) and Agent 9
    (Escalation Brief).

Property-name strategy
----------------------

Agent 3 cannot run a real `databases.retrieve` without `NOTION_TOKEN`
available in this environment, so the exact property names in the AVM bug
DB are not captured yet. `normalize_page` uses heuristic defaults that
match Notion's conventional names:

  - Title:       detected automatically by looking for the property whose
                 `type` is `"title"` (property name varies; "Name" is
                 Notion's default for new DBs).
  - Description: `Description`   (rich_text)
  - Repro Steps: `Repro Steps`   (rich_text; falls back to Description)
  - Severity:    `Severity`      (select; optional)
  - Reporter:    `Reporter`      (people; optional; extract name/email)
  - Status:      `Status`        (status type; the filter is on this)

Once Megha runs `backend/scripts/inspect_notion_db.py` against the real DB,
any mismatches can be overridden without code edits by setting the
`NOTION_PROPERTY_OVERRIDES` env var (JSON blob, e.g.
`{"description": "Summary", "repro_steps": "Steps to Reproduce"}`) or by
calling `configure_property_mapping(...)` before the poller starts.

Known limitation: notion-client v3.0 moved `databases.query` to
`data_sources.query` per Notion's 2025-09 API revision. For single-source
databases (the common case) passing the database ID to `data_sources.query`
"just works" because the database ID is also the data source ID. We
document this and rely on it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from notion_client import Client

logger = logging.getLogger(__name__)


# --- AVM Notion status values ------------------------------------------------

#: The six statuses the AVM bug DB uses. Source: product plan §Notion
#: Integration. We validate inputs to `set_status` against this set.
AVM_NOTION_STATUSES: frozenset[str] = frozenset(
    {
        "Open",
        "Triaged",
        "In progress",
        "Blocked",
        "Fixed",
        "Won't Fix",
    }
)


# --- Property mapping --------------------------------------------------------

#: Default property name lookup — overridable per-process via
#: `configure_property_mapping` or the `NOTION_PROPERTY_OVERRIDES` env var.
_DEFAULT_PROPERTY_MAPPING: dict[str, str] = {
    # NOTE: `title` is resolved dynamically from the property schema
    # (whichever property has type=='title'). The entry below is only a
    # fallback if type detection fails.
    "title": "Name",
    "description": "Description",
    "repro_steps": "Repro Steps",
    "severity": "Severity",
    "reporter": "Reporter",
    "status": "Status",
}

_property_mapping: dict[str, str] = dict(_DEFAULT_PROPERTY_MAPPING)


def configure_property_mapping(mapping: dict[str, str]) -> None:
    """Override the logical-name → Notion-property-name mapping at runtime.

    Only keys present in `mapping` are updated; unspecified keys retain their
    defaults. Intended to be called once at startup after Megha inspects the
    real DB schema and documents the mapping in
    `backend/docs/notion-schema-for-avm.md`.
    """
    for key, value in mapping.items():
        if key not in _DEFAULT_PROPERTY_MAPPING:
            logger.warning(
                "configure_property_mapping: ignoring unknown key %r", key
            )
            continue
        _property_mapping[key] = value


def _load_env_overrides() -> None:
    """Apply overrides from `NOTION_PROPERTY_OVERRIDES` if set.

    Called at import-time and on every `NotionClient(...)` construction so
    that late-imported env (e.g. via pydantic-settings loading `.env`) still
    takes effect.
    """
    raw = os.environ.get("NOTION_PROPERTY_OVERRIDES", "").strip()
    if not raw:
        return
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "NOTION_PROPERTY_OVERRIDES is not valid JSON; ignoring (value=%r)", raw
        )
        return
    if not isinstance(parsed, dict):
        logger.warning(
            "NOTION_PROPERTY_OVERRIDES must be a JSON object; ignoring (type=%s)",
            type(parsed).__name__,
        )
        return
    configure_property_mapping({str(k): str(v) for k, v in parsed.items()})


_load_env_overrides()


# --- Dataclasses -------------------------------------------------------------


@dataclass(frozen=True)
class RawNotionBug:
    """Raw Notion page payload as returned by the SDK.

    Kept as a thin wrapper so call sites don't touch the untyped dict
    directly. The `page` dict is the exact SDK output (still a dict for
    max forward compatibility with Notion API additions).
    """

    page: dict[str, Any]

    @property
    def page_id(self) -> str:
        return str(self.page.get("id", ""))


@dataclass(frozen=True)
class RawNotionPage:
    """Single-page payload for `fetch_page`."""

    page: dict[str, Any]

    @property
    def page_id(self) -> str:
        return str(self.page.get("id", ""))


@dataclass(frozen=True)
class NormalizedBug:
    """Triage-shaped view of a Notion bug page.

    Matches the columns `db.repo.create_bug` accepts.
    """

    notion_page_id: str
    title: str
    description: str
    repro_steps: str | None
    severity: str | None
    reporter: str | None
    notion_status: str


# --- Property extraction helpers --------------------------------------------


def _extract_rich_text(prop: dict[str, Any] | None) -> str:
    """Join all `.plain_text` fragments of a rich_text property."""
    if not prop:
        return ""
    pieces = prop.get("rich_text") or prop.get("title") or []
    if not isinstance(pieces, list):
        return ""
    return "".join(str(p.get("plain_text", "")) for p in pieces if isinstance(p, dict))


def _extract_title_from_properties(properties: dict[str, Any]) -> tuple[str, str]:
    """Find the title property (type=='title') and return (prop_name, text).

    Falls back to the configured title name or empty string if none found.
    """
    for name, prop in properties.items():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return name, _extract_rich_text(prop)
    # Fallback: the configured default name.
    fallback_name = _property_mapping.get("title", "Name")
    prop = properties.get(fallback_name)
    if isinstance(prop, dict):
        return fallback_name, _extract_rich_text(prop)
    return fallback_name, ""


def _extract_select(prop: dict[str, Any] | None) -> str | None:
    """Extract a `select` property's option name, or None."""
    if not prop:
        return None
    sel = prop.get("select")
    if isinstance(sel, dict):
        name = sel.get("name")
        return str(name) if name else None
    return None


def _extract_status(prop: dict[str, Any] | None) -> str | None:
    """Extract a `status` property's option name, or None.

    Notion's `status` property type is distinct from `select` at the API
    level even though they look the same in the UI.
    """
    if not prop:
        return None
    status = prop.get("status")
    if isinstance(status, dict):
        name = status.get("name")
        return str(name) if name else None
    return None


def _extract_people(prop: dict[str, Any] | None) -> str | None:
    """Extract a `people` property: prefer first person's name, fall back to email.

    Returns None if the property is absent or empty.
    """
    if not prop:
        return None
    people = prop.get("people")
    if not isinstance(people, list) or not people:
        return None
    first = people[0]
    if not isinstance(first, dict):
        return None
    name = first.get("name")
    if name:
        return str(name)
    person = first.get("person")
    if isinstance(person, dict):
        email = person.get("email")
        if email:
            return str(email)
    return None


def normalize_page(page: dict[str, Any]) -> NormalizedBug:
    """Convert a raw Notion page dict into a `NormalizedBug`.

    Property names are resolved through the configurable mapping so Megha
    can override defaults once the real AVM DB schema is captured. The title
    property is additionally detected by type (any property with
    `type=='title'`) so DBs that rename `Name` still work without override.
    """
    _load_env_overrides()  # pick up late .env loads

    page_id = str(page.get("id", ""))
    properties = page.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}

    _, title = _extract_title_from_properties(properties)

    desc_prop = properties.get(_property_mapping["description"])
    description = _extract_rich_text(desc_prop) if isinstance(desc_prop, dict) else ""

    repro_prop = properties.get(_property_mapping["repro_steps"])
    repro_text = (
        _extract_rich_text(repro_prop) if isinstance(repro_prop, dict) else ""
    )
    repro_steps: str | None = repro_text if repro_text else None

    severity = _extract_select(properties.get(_property_mapping["severity"]))
    reporter = _extract_people(properties.get(_property_mapping["reporter"]))

    status_prop = properties.get(_property_mapping["status"])
    status_value = _extract_status(status_prop) if isinstance(status_prop, dict) else None
    # Default to "Open" when absent — the query filter guarantees it, but
    # `fetch_page` is also called for pages outside the query result set.
    notion_status = status_value or "Open"

    return NormalizedBug(
        notion_page_id=page_id,
        title=title or "(untitled)",
        description=description or "",
        repro_steps=repro_steps,
        severity=severity,
        reporter=reporter,
        notion_status=notion_status,
    )


# --- Markdown → Notion block conversion --------------------------------------


def _rich_text_block(text: str) -> dict[str, Any]:
    """Wrap plain text as a Notion rich_text fragment."""
    return {"type": "text", "text": {"content": text}}


def _markdown_to_blocks(markdown_text: str) -> list[dict[str, Any]]:
    """Convert a simple markdown fragment to Notion block children.

    v1 supports:
      - `# heading` (we map to heading_2 — Notion H1 is reserved for page title)
      - `## heading` → heading_2
      - `### heading` → heading_3
      - `- bullet` → bulleted_list_item
      - blank-line-separated paragraphs → paragraph

    Deliberate limitations (v1):
      - No code fences. Triple-backtick blocks land as plain paragraphs.
      - No tables, images, numbered lists, nested bullets, or inline
        formatting (bold/italic/code). Later agents can extend.
    """
    blocks: list[dict[str, Any]] = []
    lines = markdown_text.splitlines()

    paragraph_buffer: list[str] = []

    def _flush_paragraph() -> None:
        if not paragraph_buffer:
            return
        text = " ".join(s.strip() for s in paragraph_buffer).strip()
        paragraph_buffer.clear()
        if not text:
            return
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [_rich_text_block(text)]},
            }
        )

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            _flush_paragraph()
            continue
        if line.startswith("### "):
            _flush_paragraph()
            blocks.append(
                {
                    "object": "block",
                    "type": "heading_3",
                    "heading_3": {
                        "rich_text": [_rich_text_block(line[4:].strip())]
                    },
                }
            )
            continue
        if line.startswith("## "):
            _flush_paragraph()
            blocks.append(
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {
                        "rich_text": [_rich_text_block(line[3:].strip())]
                    },
                }
            )
            continue
        if line.startswith("# "):
            # Notion H1 is reserved for the page title, so "#" in appended
            # content maps to heading_2 (matches heading_2 used for "##").
            _flush_paragraph()
            blocks.append(
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {
                        "rich_text": [_rich_text_block(line[2:].strip())]
                    },
                }
            )
            continue
        if line.startswith("- "):
            _flush_paragraph()
            blocks.append(
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [_rich_text_block(line[2:].strip())]
                    },
                }
            )
            continue
        paragraph_buffer.append(line)

    _flush_paragraph()
    return blocks


# --- The client --------------------------------------------------------------


class NotionClient:
    """Thin wrapper over `notion_client.Client`.

    Stateless w.r.t. the DB; callers pass the database_id each time.
    Re-uses one underlying HTTP client per NotionClient instance.
    """

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("NotionClient requires a non-empty token.")
        self._token = token
        self._sdk = Client(auth=token)
        _load_env_overrides()

    # -- reads --

    def list_open_bugs(self, database_id: str) -> list[RawNotionBug]:
        """Query the DB for all pages with Status == "Open", paginating fully.

        Uses `data_sources.query` (notion-client v3 / Notion API 2025-09).
        For databases with a single data source (the common case) the
        database ID works directly as a data source ID.
        """
        if not database_id:
            raise ValueError("list_open_bugs requires a non-empty database_id.")
        status_prop = _property_mapping["status"]
        filter_body: dict[str, Any] = {
            "property": status_prop,
            "status": {"equals": "Open"},
        }

        results: list[RawNotionBug] = []
        next_cursor: str | None = None
        # Hard cap iterations to avoid infinite loops if the SDK ever
        # returns has_more=True with no cursor advance. Realistically the
        # AVM bug DB is tiny, but defence in depth is cheap.
        for _ in range(100):
            query_kwargs: dict[str, Any] = {
                "data_source_id": database_id,
                "filter": filter_body,
            }
            if next_cursor:
                query_kwargs["start_cursor"] = next_cursor
            response = self._sdk.data_sources.query(**query_kwargs)
            if not isinstance(response, dict):
                break
            pages = response.get("results") or []
            for p in pages:
                if isinstance(p, dict):
                    results.append(RawNotionBug(page=p))
            if not response.get("has_more"):
                break
            next_cursor = response.get("next_cursor")
            if not next_cursor:
                break
        return results

    def fetch_page(self, page_id: str) -> RawNotionPage:
        """Retrieve a single Notion page by id."""
        if not page_id:
            raise ValueError("fetch_page requires a non-empty page_id.")
        page = self._sdk.pages.retrieve(page_id=page_id)
        if not isinstance(page, dict):
            raise RuntimeError(
                f"Notion pages.retrieve returned unexpected type: {type(page).__name__}"
            )
        return RawNotionPage(page=page)

    # -- writes --

    def set_status(self, page_id: str, notion_status: str) -> None:
        """Patch the Status property of a page.

        Raises `ValueError` if `notion_status` is not one of the six known
        AVM statuses — the calling hook is expected to already have mapped
        via `STATE_TO_NOTION_STATUS`, so this is a defence-in-depth check.
        """
        if not page_id:
            raise ValueError("set_status requires a non-empty page_id.")
        if notion_status not in AVM_NOTION_STATUSES:
            raise ValueError(
                f"Unknown AVM Notion status: {notion_status!r}. "
                f"Allowed: {sorted(AVM_NOTION_STATUSES)}"
            )
        status_prop = _property_mapping["status"]
        self._sdk.pages.update(
            page_id=page_id,
            properties={
                status_prop: {"status": {"name": notion_status}},
            },
        )

    def append_block(self, page_id: str, markdown_text: str) -> None:
        """Append a markdown fragment as block children on a page."""
        if not page_id:
            raise ValueError("append_block requires a non-empty page_id.")
        blocks = _markdown_to_blocks(markdown_text)
        if not blocks:
            return
        self._sdk.blocks.children.append(block_id=page_id, children=blocks)


# --- Internal-state → Notion-status write-back ------------------------------

# Lazy import to avoid a circular import when repo imports this module.
from ra_triage.orchestrator.states import BugState  # noqa: E402

#: Mapping from internal `BugState` values to AVM Notion status strings.
#: Tests assert this table directly, so it is the source of truth.
STATE_TO_NOTION_STATUS: dict[BugState, str] = {
    BugState.TRIAGED: "Triaged",
    BugState.INVESTIGATING: "In progress",
    BugState.DESIGNING: "In progress",
    BugState.FIXING: "In progress",
    BugState.FIXING_PARALLEL: "In progress",
    BugState.TESTING: "In progress",
    BugState.INTEGRATION_TESTING: "In progress",
    BugState.BLOCKED: "Blocked",
    BugState.FIXED: "Fixed",
    BugState.WONT_FIX: "Won't Fix",
    BugState.CANCELLED: "Won't Fix",
}


# Dependency-injection hooks for tests. Production code uses the defaults;
# `test_notion_writeback.py` substitutes `_client_builder` to verify the
# hook without a real NotionClient. Kept at module scope (not instance) so
# repo.update_bug_state can call the hook with no extra plumbing.
_client_builder: Any = None  # callable[[], NotionClient | None] | None


def _build_client_from_settings() -> NotionClient | None:
    """Return a NotionClient from `settings.notion_token`, or None if unconfigured.

    Deferred import of `ra_triage.settings` avoids a circular import chain
    between integrations.notion ↔ settings ↔ db.
    """
    from ra_triage.settings import get_settings

    settings = get_settings()
    if not settings.notion_token:
        return None
    return NotionClient(token=settings.notion_token)


def set_client_builder(
    builder: Any,  # callable[[], NotionClient | None] | None
) -> None:
    """Install a custom client builder (tests only).

    Pass `None` to restore the default (settings-driven) builder.
    """
    global _client_builder
    _client_builder = builder


def _current_client_builder() -> Any:  # callable[[], NotionClient | None]
    return _client_builder or _build_client_from_settings


def status_writeback_for_state(bug: Any, new_state: BugState) -> None:
    """Best-effort write-back of the bug's internal state → Notion status.

    Contract:
      - Never raises. Any Notion / network failure is logged and swallowed
        so the caller's DB transition commit is not rolled back.
      - If NotionClient cannot be built (missing token), logs at DEBUG and
        returns — this is the expected case in dev / CI.
      - If the state has no Notion-status mapping (e.g. OPEN, MERGING,
        READY_FOR_REVIEW), returns silently.

    `bug` is typed `Any` to keep this module import-cycle-free: repo.py
    calls with a SQLAlchemy `Bug` ORM object, but we only read
    `notion_page_id` and `id`.
    """
    target_status = STATE_TO_NOTION_STATUS.get(new_state)
    if target_status is None:
        return

    page_id = getattr(bug, "notion_page_id", None)
    if not page_id:
        logger.debug(
            "Notion writeback: bug has no notion_page_id, skipping (state=%s)",
            new_state.value,
        )
        return

    builder = _current_client_builder()
    try:
        client = builder()
    except Exception:
        logger.exception("Notion writeback: failed to build NotionClient")
        return
    if client is None:
        logger.debug(
            "Notion writeback: NotionClient unconfigured, skipping "
            "(bug_id=%s, state=%s)",
            getattr(bug, "id", "?"),
            new_state.value,
        )
        return

    try:
        client.set_status(page_id=page_id, notion_status=target_status)
        logger.info(
            "Notion writeback: bug_id=%s page_id=%s state=%s → status=%s",
            getattr(bug, "id", "?"),
            page_id,
            new_state.value,
            target_status,
        )
    except Exception:
        logger.exception(
            "Notion writeback failed (bug_id=%s, page_id=%s, state=%s, status=%s)",
            getattr(bug, "id", "?"),
            page_id,
            new_state.value,
            target_status,
        )


# --- PR-link write-back (Agent 8) -------------------------------------------


def append_pr_link(
    page_id: str,
    pr_url: str,
    repo_name: str,
    note: str | None = None,
) -> None:
    """Append a PR link bullet to a Notion page.

    Renders a line shaped like ``- PR (<repo_name>): <pr_url> — merged`` by
    default, or with ``note`` substituted for "merged" if the caller wants
    something else. Uses the configured NotionClient (via the same builder
    plumbing as :func:`status_writeback_for_state`); silently no-ops if
    Notion is unconfigured. Never raises — merge success must not be
    reverted by a Notion outage.

    Signature mirrors :func:`NotionClient.append_block` but handles the
    markdown formatting + client construction so the merge driver can stay
    compact.
    """
    if not page_id:
        logger.debug("append_pr_link: empty page_id, skipping")
        return

    suffix = note if note is not None else "merged"
    markdown = f"- PR ({repo_name}): {pr_url} — {suffix}\n"

    builder = _current_client_builder()
    try:
        client = builder()
    except Exception:
        logger.exception("append_pr_link: failed to build NotionClient")
        return
    if client is None:
        logger.debug(
            "append_pr_link: NotionClient unconfigured, skipping (page_id=%s)",
            page_id,
        )
        return
    try:
        client.append_block(page_id=page_id, markdown_text=markdown)
        logger.info(
            "Notion append_pr_link: page_id=%s repo=%s url=%s",
            page_id,
            repo_name,
            pr_url,
        )
    except Exception:
        logger.exception(
            "Notion append_pr_link failed (page_id=%s, repo=%s)", page_id, repo_name
        )


__all__ = [
    "AVM_NOTION_STATUSES",
    "NormalizedBug",
    "NotionClient",
    "RawNotionBug",
    "RawNotionPage",
    "STATE_TO_NOTION_STATUS",
    "append_pr_link",
    "configure_property_mapping",
    "normalize_page",
    "set_client_builder",
    "status_writeback_for_state",
]
