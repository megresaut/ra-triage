"""Slack integration for RA Triage — OUTBOUND-ONLY incoming webhooks.

Scope
-----

Per product plan (Slack Integration) and tech plan §12.3:

  - Slack is notification-only. No Events API, no interactive components,
    no signing secret required. All approval happens in the RA Triage
    frontend.
  - Two events ping Slack:
      1. Escalation (bug enters BLOCKED with a populated Escalation Brief).
      2. Ready-for-review (bug enters READY_FOR_REVIEW).
  - Successful auto-merges do NOT ping (product plan: "too noisy"). Cancel
    and Won't-Fix also do not ping.
  - The deep link uses ``http://localhost:1313/bugs/<id>`` (not
    ``127.0.0.1`` — matches the product plan wording and aligns with the
    frontend dev server port preference; see
    ``feedback_dev_ports.md`` in Megha's notes).

Configuration
-------------

The webhook URL lives in the ``RA_AVM_SLACK_WEBHOOK`` env var (see
``config.registry.ProjectConfig.slack_webhook_env``). :func:`is_configured`
returns ``False`` when the var is empty / unset — :func:`notify_escalation`
and :func:`notify_ready_for_review` become no-ops in that case, which is
the normal dev / CI state. Production / Megha's laptop will have it set.

Failure mode
------------

The two high-level ``notify_*`` helpers NEVER raise. A Slack outage or a
misconfigured webhook must never roll back a bug state transition. Errors
are logged and swallowed.

Low-level :class:`SlackClient.post` DOES raise :class:`SlackError` so the
tests can observe the exact failure shape; the wrappers catch it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from ra_triage.settings import get_settings

if TYPE_CHECKING:
    from ra_triage.config.registry import ProjectConfig
    from ra_triage.db.models import Bug

logger = logging.getLogger(__name__)


# --- Constants --------------------------------------------------------------

#: Base URL for deep links into the frontend. Hardcoded per product plan;
#: if Megha ever moves the frontend off 1313 we'll parameterize this from
#: settings.
_FRONTEND_BASE_URL = "http://localhost:1313"

#: Max seconds per Slack POST. Slack webhooks are usually < 500ms; 10s is
#: generous without letting a hung endpoint stall the pipeline indefinitely.
_SLACK_TIMEOUT_SECONDS = 10.0


# --- Errors -----------------------------------------------------------------


class SlackError(Exception):
    """Raised when the Slack webhook returns a non-2xx response.

    The message includes a short excerpt of the response body so the log
    line is actionable without having to re-POST manually.
    """


# --- Low-level client -------------------------------------------------------


class SlackClient:
    """Thin wrapper over an HTTPX client for Slack incoming webhooks.

    Holding a reference to an ``httpx.Client`` in the instance keeps
    connection reuse possible when we post multiple messages back-to-back
    (not common today, but cheap to preserve). The client is used as a
    dependency-injection seam: tests pass a ``MagicMock`` spec'd against
    ``httpx.Client`` so the JSON payload can be asserted without any real
    network traffic.
    """

    def __init__(
        self,
        webhook_url: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not webhook_url:
            raise ValueError("SlackClient requires a non-empty webhook_url")
        self._webhook_url = webhook_url
        # ``_owns_client`` tracks whether we should close a client we built
        # ourselves. Tests pass their own mock so we must not close it.
        self._owns_client = http_client is None
        self._http_client = http_client or httpx.Client(
            timeout=_SLACK_TIMEOUT_SECONDS
        )

    def post(
        self,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        """POST a message to the webhook.

        Body shape mirrors Slack's documented incoming-webhook schema:
        ``{"text": <fallback>, "blocks": [...]}``. The ``text`` field is
        the notification fallback used in mobile pushes and in clients
        that don't render blocks. ``blocks`` drives the rich render.

        Raises :class:`SlackError` on non-2xx with a short body excerpt.
        Timeouts surface as :class:`SlackError` too (httpx raises, we
        re-raise as ours).
        """
        payload: dict[str, Any] = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        try:
            response = self._http_client.post(self._webhook_url, json=payload)
        except httpx.HTTPError as exc:
            raise SlackError(f"Slack webhook request failed: {exc}") from exc
        if response.status_code < 200 or response.status_code >= 300:
            excerpt = (response.text or "")[:300]
            raise SlackError(
                f"Slack webhook returned {response.status_code}: {excerpt!r}"
            )

    def close(self) -> None:
        """Release the underlying HTTP client if we created it."""
        if self._owns_client:
            try:
                self._http_client.close()
            except Exception:  # pragma: no cover - defensive
                logger.exception("SlackClient.close: httpx client close failed")


# --- Configuration helpers --------------------------------------------------


def is_configured() -> bool:
    """Return True if ``RA_AVM_SLACK_WEBHOOK`` is set to a non-empty value."""
    return bool(get_settings().ra_avm_slack_webhook)


# Test seam: lets ``tests/unit/test_slack.py`` inject a client without
# monkeypatching httpx globally. Production leaves this as ``None`` and
# ``_build_client`` reads the webhook URL from settings.
_client_builder: Any = None  # callable[[], SlackClient | None] | None


def set_client_builder(builder: Any) -> None:
    """Install a custom client builder (tests only).

    Pass ``None`` to restore the default (settings-driven) builder.
    """
    global _client_builder
    _client_builder = builder


def _build_client() -> SlackClient | None:
    """Build a :class:`SlackClient` from settings, or return None if unset."""
    settings = get_settings()
    webhook = settings.ra_avm_slack_webhook
    if not webhook:
        return None
    return SlackClient(webhook_url=webhook)


def _current_client_builder() -> Any:
    return _client_builder or _build_client


# --- Deep links -------------------------------------------------------------


def bug_deep_link(bug_id: int) -> str:
    """Return the frontend deep link for a bug."""
    return f"{_FRONTEND_BASE_URL}/bugs/{bug_id}"


# --- High-level notifications -----------------------------------------------


def _render_notification_blocks(
    *,
    bug_id: int,
    title: str,
    project_display_name: str,
    headline: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Return the ``(text_fallback, blocks)`` payload for a notification.

    ``headline`` is the short verb-y phrase that differentiates the two
    notification types (e.g. "requires your attention" or "ready for
    review"). Both notifications share the same visual shape: a section
    block with the project, bug id + title, and headline; a context block
    with a link back to the RA Triage frontend.
    """
    deep_link = bug_deep_link(bug_id)
    text_fallback = (
        f"RA Triage — Bug #{bug_id} — {title} — {headline}"
    )
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*RA Triage — {project_display_name}*\n"
                    f"Bug #{bug_id} — {title} — {headline}"
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"<{deep_link}|Open in RA Triage>",
                }
            ],
        },
    ]
    return text_fallback, blocks


def notify_escalation(bug: Bug, project: ProjectConfig) -> None:
    """Ping Slack that a bug has escalated (entered BLOCKED with a brief).

    Silent no-op when Slack isn't configured. Never raises — logs on any
    failure so the state transition isn't rolled back.
    """
    _notify(
        bug=bug,
        project=project,
        headline="requires your attention",
        event_name="escalation",
    )


def notify_ready_for_review(bug: Bug, project: ProjectConfig) -> None:
    """Ping Slack that a bug is READY_FOR_REVIEW.

    Silent no-op when Slack isn't configured. Never raises.
    """
    _notify(
        bug=bug,
        project=project,
        headline="ready for review",
        event_name="ready_for_review",
    )


def _notify(
    *,
    bug: Bug,
    project: ProjectConfig,
    headline: str,
    event_name: str,
) -> None:
    """Shared implementation for both notification types."""
    builder = _current_client_builder()
    try:
        client = builder()
    except Exception:
        logger.exception(
            "Slack %s: failed to build SlackClient (bug_id=%s)",
            event_name,
            getattr(bug, "id", "?"),
        )
        return
    if client is None:
        logger.debug(
            "Slack %s: webhook unconfigured; skipping (bug_id=%s)",
            event_name,
            getattr(bug, "id", "?"),
        )
        return

    text, blocks = _render_notification_blocks(
        bug_id=int(bug.id),
        title=str(bug.title or f"Bug #{bug.id}"),
        project_display_name=project.display_name,
        headline=headline,
    )

    try:
        client.post(text=text, blocks=blocks)
        logger.info(
            "Slack %s: posted for bug_id=%s", event_name, bug.id
        )
    except Exception:
        logger.exception(
            "Slack %s: post failed for bug_id=%s — swallowing "
            "(transitions must not roll back)",
            event_name,
            getattr(bug, "id", "?"),
        )
    finally:
        # Only close clients we built ourselves; mocks have no-op close.
        try:
            client.close()
        except Exception:  # pragma: no cover - defensive
            pass


__all__ = [
    "SlackClient",
    "SlackError",
    "bug_deep_link",
    "is_configured",
    "notify_escalation",
    "notify_ready_for_review",
    "set_client_builder",
]
