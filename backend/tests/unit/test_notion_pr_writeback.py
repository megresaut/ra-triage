"""Unit tests for ``integrations.notion.append_pr_link``.

Exercises the PR-link writeback hook that :func:`orchestrator.merge.merge_bug`
calls on a successful merge. Mocks ``NotionClient.append_block`` to confirm
the markdown line renders as the expected shape and is never written on
unconfigured / failing clients.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from ra_triage.integrations import notion as notion_module


@pytest.fixture(autouse=True)
def reset_client_builder() -> Any:
    notion_module.set_client_builder(None)
    yield
    notion_module.set_client_builder(None)


def test_append_pr_link_invokes_append_block_with_default_note() -> None:
    """Default note is "merged"; renders `- PR (<repo>): <url> — merged`."""
    fake_client = MagicMock()
    notion_module.set_client_builder(lambda: fake_client)

    notion_module.append_pr_link(
        page_id="page-123",
        pr_url="https://github.com/megresaut/avm-backend/pull/42",
        repo_name="avm-backend",
    )

    fake_client.append_block.assert_called_once()
    kwargs = fake_client.append_block.call_args.kwargs
    assert kwargs["page_id"] == "page-123"
    md = kwargs["markdown_text"]
    assert "- PR (avm-backend):" in md
    assert "https://github.com/megresaut/avm-backend/pull/42" in md
    assert "— merged" in md


def test_append_pr_link_with_custom_note() -> None:
    fake_client = MagicMock()
    notion_module.set_client_builder(lambda: fake_client)

    notion_module.append_pr_link(
        page_id="p",
        pr_url="https://github.com/o/r/pull/1",
        repo_name="r",
        note="open",
    )
    md = fake_client.append_block.call_args.kwargs["markdown_text"]
    assert "— open" in md


def test_append_pr_link_noop_when_client_unconfigured() -> None:
    """Builder returning None → function returns without raising."""
    notion_module.set_client_builder(lambda: None)
    # No raise — defensive behavior.
    notion_module.append_pr_link(
        page_id="p", pr_url="https://github.com/o/r/pull/1", repo_name="r"
    )


def test_append_pr_link_noop_on_empty_page_id() -> None:
    fake_client = MagicMock()
    notion_module.set_client_builder(lambda: fake_client)
    notion_module.append_pr_link(
        page_id="", pr_url="https://github.com/o/r/pull/1", repo_name="r"
    )
    fake_client.append_block.assert_not_called()


def test_append_pr_link_swallows_client_exceptions() -> None:
    """If append_block raises, the helper never propagates — the merge
    must not be reverted by a Notion outage."""
    fake_client = MagicMock()
    fake_client.append_block.side_effect = RuntimeError("notion down")
    notion_module.set_client_builder(lambda: fake_client)

    # No raise.
    notion_module.append_pr_link(
        page_id="p", pr_url="https://github.com/o/r/pull/1", repo_name="r"
    )


def test_append_pr_link_swallows_builder_exceptions() -> None:
    """Same contract when the client builder itself raises."""

    def _bad_builder() -> Any:
        raise RuntimeError("settings broken")

    notion_module.set_client_builder(_bad_builder)
    notion_module.append_pr_link(
        page_id="p", pr_url="https://github.com/o/r/pull/1", repo_name="r"
    )
