"""Unit tests for `ra_triage.integrations.notion`.

Mocks the `notion_client.Client` SDK with `unittest.mock`. Covers:
  - `list_open_bugs` returns [] when the API returns no results
  - Pagination is followed (has_more=true, next_cursor=x → second page)
  - `normalize_page` extracts title / description / severity / reporter /
    status / repro_steps from a fixture page dict
  - `set_status` raises on unknown status
  - `append_block` converts markdown headings + bullets + paragraphs

All tests run without a NOTION_TOKEN.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ra_triage.integrations import notion as notion_mod
from ra_triage.integrations.notion import (
    AVM_NOTION_STATUSES,
    NotionClient,
    configure_property_mapping,
    normalize_page,
)

# --- Fixtures ----------------------------------------------------------------


def _make_page(
    *,
    page_id: str = "page-1",
    title_text: str = "A crashing invoice",
    description_text: str = "Clicking save crashes",
    repro_text: str | None = "Open /invoices, click save",
    severity: str | None = "High",
    reporter_name: str | None = "Megha",
    reporter_email: str | None = None,
    status: str | None = "Open",
    title_prop_name: str = "Name",
) -> dict[str, Any]:
    """Build a Notion page dict that mirrors the real SDK output shape."""
    properties: dict[str, Any] = {
        title_prop_name: {
            "type": "title",
            "title": [{"type": "text", "plain_text": title_text}],
        },
        "Description": {
            "type": "rich_text",
            "rich_text": [{"type": "text", "plain_text": description_text}],
        },
    }
    if repro_text is not None:
        properties["Repro Steps"] = {
            "type": "rich_text",
            "rich_text": [{"type": "text", "plain_text": repro_text}],
        }
    if severity is not None:
        properties["Severity"] = {
            "type": "select",
            "select": {"name": severity},
        }
    if reporter_name or reporter_email:
        person_payload: dict[str, Any] = {}
        if reporter_name:
            person_payload["name"] = reporter_name
        if reporter_email:
            person_payload["person"] = {"email": reporter_email}
        properties["Reporter"] = {
            "type": "people",
            "people": [person_payload],
        }
    if status is not None:
        properties["Status"] = {
            "type": "status",
            "status": {"name": status},
        }
    return {"id": page_id, "properties": properties}


@pytest.fixture(autouse=True)
def reset_property_mapping() -> Any:
    """Ensure each test starts with the default property mapping."""
    configure_property_mapping(
        {
            "title": "Name",
            "description": "Description",
            "repro_steps": "Repro Steps",
            "severity": "Severity",
            "reporter": "Reporter",
            "status": "Status",
        }
    )
    yield
    configure_property_mapping(
        {
            "title": "Name",
            "description": "Description",
            "repro_steps": "Repro Steps",
            "severity": "Severity",
            "reporter": "Reporter",
            "status": "Status",
        }
    )


# --- NotionClient construction ----------------------------------------------


def test_empty_token_rejected() -> None:
    with pytest.raises(ValueError):
        NotionClient(token="")


# --- list_open_bugs ----------------------------------------------------------


def test_list_open_bugs_empty_result() -> None:
    with patch.object(notion_mod, "Client") as MockClient:
        sdk = MockClient.return_value
        sdk.data_sources.query.return_value = {"results": [], "has_more": False}
        client = NotionClient(token="secret_test")
        result = client.list_open_bugs("db-1")
        assert result == []
        sdk.data_sources.query.assert_called_once()


def test_list_open_bugs_follows_pagination() -> None:
    page_a = _make_page(page_id="p-a", title_text="A")
    page_b = _make_page(page_id="p-b", title_text="B")
    page_c = _make_page(page_id="p-c", title_text="C")

    responses = [
        {"results": [page_a, page_b], "has_more": True, "next_cursor": "cursor-2"},
        {"results": [page_c], "has_more": False, "next_cursor": None},
    ]

    with patch.object(notion_mod, "Client") as MockClient:
        sdk = MockClient.return_value
        sdk.data_sources.query.side_effect = responses

        client = NotionClient(token="secret_test")
        result = client.list_open_bugs("db-1")

        assert [r.page_id for r in result] == ["p-a", "p-b", "p-c"]
        # Two calls: first without cursor, second with `start_cursor=cursor-2`.
        assert sdk.data_sources.query.call_count == 2
        second_kwargs = sdk.data_sources.query.call_args_list[1].kwargs
        assert second_kwargs.get("start_cursor") == "cursor-2"


def test_list_open_bugs_filter_uses_status_property() -> None:
    with patch.object(notion_mod, "Client") as MockClient:
        sdk = MockClient.return_value
        sdk.data_sources.query.return_value = {"results": [], "has_more": False}
        client = NotionClient(token="secret_test")
        client.list_open_bugs("db-1")
        kwargs = sdk.data_sources.query.call_args.kwargs
        filter_body = kwargs["filter"]
        assert filter_body["property"] == "Status"
        assert filter_body["status"] == {"equals": "Open"}
        assert kwargs["data_source_id"] == "db-1"


def test_list_open_bugs_empty_database_id_rejected() -> None:
    with patch.object(notion_mod, "Client"):
        client = NotionClient(token="secret_test")
        with pytest.raises(ValueError):
            client.list_open_bugs("")


# --- normalize_page ----------------------------------------------------------


def test_normalize_page_extracts_all_fields() -> None:
    page = _make_page(
        page_id="p-1",
        title_text="Button is broken",
        description_text="It crashes on click",
        repro_text="1. Open /foo\n2. Click",
        severity="Critical",
        reporter_name="Megha",
        status="Open",
    )
    normalized = normalize_page(page)
    assert normalized.notion_page_id == "p-1"
    assert normalized.title == "Button is broken"
    assert normalized.description == "It crashes on click"
    assert normalized.repro_steps == "1. Open /foo\n2. Click"
    assert normalized.severity == "Critical"
    assert normalized.reporter == "Megha"
    assert normalized.notion_status == "Open"


def test_normalize_page_detects_title_by_type_not_name() -> None:
    """If the title property is called something other than 'Name' we still
    find it because `_extract_title_from_properties` looks for type=='title'."""
    page = _make_page(title_prop_name="Issue", title_text="Auto-detected")
    normalized = normalize_page(page)
    assert normalized.title == "Auto-detected"


def test_normalize_page_handles_missing_optional_fields() -> None:
    page = _make_page(
        repro_text=None, severity=None, reporter_name=None, status=None
    )
    normalized = normalize_page(page)
    assert normalized.repro_steps is None
    assert normalized.severity is None
    assert normalized.reporter is None
    # Default when missing — filter guarantees open on the real API, but
    # fetch_page may return other statuses; default to Open for safety.
    assert normalized.notion_status == "Open"


def test_normalize_page_reporter_email_fallback() -> None:
    page = _make_page(reporter_name=None, reporter_email="megha@ra.com")
    normalized = normalize_page(page)
    assert normalized.reporter == "megha@ra.com"


def test_normalize_page_empty_title_defaults_to_untitled() -> None:
    page = {"id": "p-2", "properties": {}}
    normalized = normalize_page(page)
    assert normalized.title == "(untitled)"
    assert normalized.description == ""


def test_normalize_page_respects_property_overrides() -> None:
    configure_property_mapping({"description": "Summary"})
    page = _make_page()
    # Rename Description → Summary in the payload.
    page["properties"]["Summary"] = page["properties"].pop("Description")
    normalized = normalize_page(page)
    assert normalized.description == "Clicking save crashes"


# --- set_status --------------------------------------------------------------


def test_set_status_rejects_unknown_status() -> None:
    with patch.object(notion_mod, "Client"):
        client = NotionClient(token="secret_test")
        with pytest.raises(ValueError):
            client.set_status("page-1", "Bogus")


def test_set_status_calls_pages_update_with_status_property() -> None:
    with patch.object(notion_mod, "Client") as MockClient:
        sdk = MockClient.return_value
        client = NotionClient(token="secret_test")
        client.set_status("page-1", "Triaged")
        sdk.pages.update.assert_called_once()
        kwargs = sdk.pages.update.call_args.kwargs
        assert kwargs["page_id"] == "page-1"
        assert kwargs["properties"] == {
            "Status": {"status": {"name": "Triaged"}},
        }


@pytest.mark.parametrize("status", sorted(AVM_NOTION_STATUSES))
def test_set_status_accepts_every_avm_status(status: str) -> None:
    with patch.object(notion_mod, "Client"):
        client = NotionClient(token="secret_test")
        client.set_status("page-1", status)  # must not raise


# --- fetch_page --------------------------------------------------------------


def test_fetch_page_returns_raw_page() -> None:
    page = _make_page()
    with patch.object(notion_mod, "Client") as MockClient:
        sdk = MockClient.return_value
        sdk.pages.retrieve.return_value = page
        client = NotionClient(token="secret_test")
        result = client.fetch_page("page-1")
        assert result.page_id == "page-1"
        sdk.pages.retrieve.assert_called_once_with(page_id="page-1")


# --- append_block ------------------------------------------------------------


def test_append_block_converts_headings_and_bullets() -> None:
    markdown = (
        "## Heading 2\n"
        "\n"
        "A paragraph spanning\n"
        "two lines.\n"
        "\n"
        "### Heading 3\n"
        "- first bullet\n"
        "- second bullet\n"
    )
    with patch.object(notion_mod, "Client") as MockClient:
        sdk = MockClient.return_value
        client = NotionClient(token="secret_test")
        client.append_block("page-1", markdown)

        sdk.blocks.children.append.assert_called_once()
        kwargs = sdk.blocks.children.append.call_args.kwargs
        assert kwargs["block_id"] == "page-1"
        children = kwargs["children"]
        types = [c["type"] for c in children]
        assert types == [
            "heading_2",
            "paragraph",
            "heading_3",
            "bulleted_list_item",
            "bulleted_list_item",
        ]
        assert (
            children[0]["heading_2"]["rich_text"][0]["text"]["content"]
            == "Heading 2"
        )
        assert (
            children[1]["paragraph"]["rich_text"][0]["text"]["content"]
            == "A paragraph spanning two lines."
        )
        assert (
            children[3]["bulleted_list_item"]["rich_text"][0]["text"]["content"]
            == "first bullet"
        )


def test_append_block_noop_on_empty_input() -> None:
    with patch.object(notion_mod, "Client") as MockClient:
        sdk = MockClient.return_value
        client = NotionClient(token="secret_test")
        client.append_block("page-1", "")
        sdk.blocks.children.append.assert_not_called()


def test_append_block_hash_heading_maps_to_h2() -> None:
    """# Headings map to heading_2 because Notion reserves H1 for page title."""
    with patch.object(notion_mod, "Client") as MockClient:
        sdk = MockClient.return_value
        client = NotionClient(token="secret_test")
        client.append_block("p", "# Top")
        children = sdk.blocks.children.append.call_args.kwargs["children"]
        assert children[0]["type"] == "heading_2"


# --- RawNotionBug smoke ------------------------------------------------------


def test_rawnotionbug_page_id_property() -> None:
    from ra_triage.integrations.notion import RawNotionBug

    raw = RawNotionBug(page={"id": "xyz"})
    assert raw.page_id == "xyz"


def test_rawnotionbug_page_id_empty_when_missing() -> None:
    from ra_triage.integrations.notion import RawNotionBug

    raw = RawNotionBug(page={})
    assert raw.page_id == ""


# --- Mocked SDK hygiene ------------------------------------------------------


def test_notion_client_smoke_mock_object_type() -> None:
    """Sanity check — NotionClient must use the SDK we patched, not a real one."""
    with patch.object(notion_mod, "Client", MagicMock()) as MockClient:
        client = NotionClient(token="secret_test")
        assert client._sdk is MockClient.return_value  # noqa: SLF001
