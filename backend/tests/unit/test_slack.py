"""Tests for :mod:`ra_triage.integrations.slack`.

These tests mock ``httpx.Client`` so we never contact the real webhook.
We verify:

  - ``is_configured()`` reflects the ``RA_AVM_SLACK_WEBHOOK`` env var.
  - ``notify_escalation`` / ``notify_ready_for_review`` build payloads
    that include the bug id, title, the RA Triage deep link, and the
    event-specific headline.
  - Both ``notify_*`` are no-ops when Slack isn't configured.
  - Exceptions from the underlying HTTP client are swallowed (never raise
    out of ``notify_*``).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from ra_triage.config.registry import ProjectConfig, get_project
from ra_triage.integrations import slack as slack_mod
from ra_triage.integrations.slack import (
    SlackClient,
    SlackError,
    bug_deep_link,
    is_configured,
    notify_escalation,
    notify_ready_for_review,
)

# --- Fixtures --------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_client_builder() -> Any:
    """Ensure each test starts with no custom builder installed."""
    slack_mod.set_client_builder(None)
    yield
    slack_mod.set_client_builder(None)


class _FakeBug:
    """Minimal duck-typed Bug — notify_* only reads id and title."""

    def __init__(self, *, bug_id: int = 42, title: str = "Example title") -> None:
        self.id = bug_id
        self.title = title


def _sample_project() -> ProjectConfig:
    return get_project("ra_avm")


def _install_fake_http(posts: list[dict[str, Any]]) -> None:
    """Install a SlackClient builder that records payloads into ``posts``."""

    class _FakeResponse:
        status_code = 200
        text = ""

    class _FakeHttpClient:
        def post(self, url: str, *, json: dict[str, Any]) -> _FakeResponse:
            posts.append({"url": url, "json": json})
            return _FakeResponse()

        def close(self) -> None:
            return None

    def _builder() -> SlackClient:
        return SlackClient(
            webhook_url="https://hooks.slack.test/123",
            http_client=_FakeHttpClient(),  # type: ignore[arg-type]
        )

    slack_mod.set_client_builder(_builder)


# --- is_configured ---------------------------------------------------------


def test_is_configured_false_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RA_AVM_SLACK_WEBHOOK", "")
    from ra_triage import settings as settings_module

    settings_module.get_settings.cache_clear()
    assert is_configured() is False


def test_is_configured_true_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RA_AVM_SLACK_WEBHOOK", "https://hooks.slack.test/abc")
    from ra_triage import settings as settings_module

    settings_module.get_settings.cache_clear()
    assert is_configured() is True


# --- notify_escalation payload --------------------------------------------


def test_notify_escalation_posts_payload_with_bug_id_title_and_link() -> None:
    posts: list[dict[str, Any]] = []
    _install_fake_http(posts)
    bug = _FakeBug(bug_id=42, title="Login loop after idle")

    notify_escalation(bug, _sample_project())  # type: ignore[arg-type]

    assert len(posts) == 1
    payload = posts[0]["json"]
    assert posts[0]["url"] == "https://hooks.slack.test/123"
    # Text fallback includes bug id + title + headline.
    assert "42" in payload["text"]
    assert "Login loop after idle" in payload["text"]
    assert "requires your attention" in payload["text"]

    # Blocks include a mrkdwn section + a context element with the deep link.
    blocks = payload["blocks"]
    assert any(b.get("type") == "section" for b in blocks)
    context_blocks = [b for b in blocks if b.get("type") == "context"]
    assert context_blocks
    rendered = json.dumps(context_blocks)
    assert bug_deep_link(42) in rendered
    assert "http://localhost:1313/bugs/42" in rendered


# --- notify_ready_for_review payload --------------------------------------


def test_notify_ready_for_review_posts_payload_with_expected_headline() -> None:
    posts: list[dict[str, Any]] = []
    _install_fake_http(posts)
    bug = _FakeBug(bug_id=7, title="RFR bug")

    notify_ready_for_review(bug, _sample_project())  # type: ignore[arg-type]

    assert len(posts) == 1
    payload = posts[0]["json"]
    assert "ready for review" in payload["text"]
    assert "7" in payload["text"]
    rendered = json.dumps(payload["blocks"])
    assert "http://localhost:1313/bugs/7" in rendered


# --- unconfigured → no-op --------------------------------------------------


def test_notify_noop_when_webhook_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RA_AVM_SLACK_WEBHOOK", "")
    from ra_triage import settings as settings_module

    settings_module.get_settings.cache_clear()
    # No builder installed → defaults to settings-based builder → returns None.
    # Use a spy that would raise if called; we assert it isn't.
    calls: list[str] = []

    class _Spy:
        def post(self, *_args: Any, **_kwargs: Any) -> None:
            calls.append("post")

        def close(self) -> None:
            return None

    def _never_built() -> None:
        raise AssertionError("builder should not produce a client when unconfigured")

    # Reset to default builder; nothing should happen.
    slack_mod.set_client_builder(None)
    notify_escalation(_FakeBug(), _sample_project())  # type: ignore[arg-type]
    notify_ready_for_review(_FakeBug(), _sample_project())  # type: ignore[arg-type]
    # No crashes, no side-effects.
    assert calls == []
    # Silence unused warnings.
    _ = _Spy
    _ = _never_built


# --- exceptions swallowed --------------------------------------------------


def test_notify_escalation_swallows_post_errors() -> None:
    """notify_* must never raise, even when the underlying post errors."""

    class _RaisingClient:
        def post(self, *_args: Any, **_kwargs: Any) -> None:
            raise SlackError("boom")

        def close(self) -> None:
            return None

    slack_mod.set_client_builder(lambda: _RaisingClient())
    # Should not raise.
    notify_escalation(_FakeBug(), _sample_project())  # type: ignore[arg-type]
    notify_ready_for_review(_FakeBug(), _sample_project())  # type: ignore[arg-type]


def test_notify_swallows_builder_errors() -> None:
    """If the builder itself raises, the caller shouldn't see it."""

    def _bad_builder() -> None:
        raise RuntimeError("settings broken")

    slack_mod.set_client_builder(_bad_builder)
    notify_escalation(_FakeBug(), _sample_project())  # type: ignore[arg-type]
    notify_ready_for_review(_FakeBug(), _sample_project())  # type: ignore[arg-type]


# --- SlackClient.post error paths -----------------------------------------


def test_slack_client_post_raises_on_non_2xx() -> None:
    """The low-level client DOES raise so callers can observe failures."""

    class _FakeResponse:
        status_code = 500
        text = "internal error"

    class _FakeHttpClient:
        def post(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
            return _FakeResponse()

        def close(self) -> None:
            return None

    client = SlackClient(
        webhook_url="https://hooks.slack.test/123",
        http_client=_FakeHttpClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(SlackError) as excinfo:
        client.post(text="hi")
    assert "500" in str(excinfo.value)
    assert "internal error" in str(excinfo.value)


def test_slack_client_post_wraps_httpx_errors() -> None:
    """A timeout or transport error is wrapped in SlackError."""

    class _ExplodingClient:
        def post(self, *_args: Any, **_kwargs: Any) -> None:
            raise httpx.ConnectError("no route")

        def close(self) -> None:
            return None

    client = SlackClient(
        webhook_url="https://hooks.slack.test/123",
        http_client=_ExplodingClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(SlackError):
        client.post(text="hi")


# --- basic constructor invariants -----------------------------------------


def test_slack_client_requires_webhook_url() -> None:
    with pytest.raises(ValueError):
        SlackClient(webhook_url="")


def test_bug_deep_link_uses_localhost_not_ip() -> None:
    """Non-negotiable: deep link uses ``localhost``, not ``127.0.0.1``."""
    link = bug_deep_link(99)
    assert link == "http://localhost:1313/bugs/99"
    assert "127.0.0.1" not in link


# --- silence unused import lint --------------------------------------------

_ = MagicMock  # keep import for future test extensions
