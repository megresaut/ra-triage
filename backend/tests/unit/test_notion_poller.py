"""Unit tests for `ra_triage.integrations.notion_poller`.

The poller uses a mocked NotionClient (injected via `client_factory`) so no
real Notion traffic happens. Tests cover:

  - poll_once creates bugs in OPEN state for each fetched page
  - Re-polling is idempotent (same page IDs → no duplicates)
  - Consecutive failures increment; the 3rd failure triggers the repeated-
    failure event; a success resets the counter
  - Disabled when NOTION_TOKEN is missing (no exception, no registration)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from ra_triage.db import repo
from ra_triage.integrations import notion_poller as poller_mod
from ra_triage.integrations.notion import NormalizedBug
from ra_triage.integrations.notion_poller import NotionPoller
from ra_triage.orchestrator.states import BugState
from ra_triage.settings import Settings


def _bug_page(page_id: str, title: str = "t", status: str = "Open") -> dict[str, Any]:
    """Minimal page dict satisfying `normalize_page`."""
    return {
        "id": page_id,
        "properties": {
            "Name": {
                "type": "title",
                "title": [{"type": "text", "plain_text": title}],
            },
            "Description": {
                "type": "rich_text",
                "rich_text": [{"type": "text", "plain_text": f"desc for {page_id}"}],
            },
            "Status": {"type": "status", "status": {"name": status}},
        },
    }


def _raw_page(page_id: str, title: str = "t") -> Any:
    """Wrap a page dict in a RawNotionBug (has `.page` and `.page_id`)."""
    from ra_triage.integrations.notion import RawNotionBug

    return RawNotionBug(page=_bug_page(page_id, title))


def _make_settings(*, token: str, db_id: str) -> Settings:
    """Build a Settings instance with injected values (bypasses env)."""
    # pydantic-settings uses env/init; init kwargs win. `.env` may still
    # override, so we also patch by just constructing directly.
    return Settings(notion_token=token, notion_bugs_db_id=db_id)


# --- enable / disable based on creds -----------------------------------------


def test_poller_disabled_without_token() -> None:
    """Without NOTION_TOKEN the poller skips registration entirely.

    We rely on `is_registered` as the observable behaviour; the warning
    log line is exercised manually (and in the acceptance-criteria live
    backend boot). caplog + pytest-asyncio can race across tests, so
    we don't assert on it here."""
    settings = _make_settings(token="", db_id="db-1")
    poller = NotionPoller(settings=settings)
    poller.start()
    try:
        assert poller.is_registered is False
    finally:
        poller.stop()


def test_poller_disabled_without_db_id() -> None:
    """Without NOTION_BUGS_DB_ID the poller skips registration entirely."""
    settings = _make_settings(token="secret_test", db_id="")
    poller = NotionPoller(settings=settings)
    poller.start()
    try:
        assert poller.is_registered is False
    finally:
        poller.stop()


async def test_poller_registers_when_creds_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registering requires a running loop (APScheduler's AsyncIOScheduler);
    the real lifespan hook runs inside the server loop, so we exercise the
    same path here via an async test (pytest-asyncio auto mode)."""
    settings = _make_settings(token="secret_test", db_id="db-1")
    fake_client = MagicMock()
    fake_client.list_open_bugs.return_value = []

    def _factory(token: str) -> Any:
        assert token == "secret_test"
        return fake_client

    monkeypatch.setenv("NOTION_BUGS_DB_ID", "db-1")
    poller = NotionPoller(settings=settings, client_factory=_factory)
    poller.start()
    try:
        assert poller.is_registered is True
    finally:
        poller.stop()


# --- poll_once happy path -----------------------------------------------------


async def test_poll_once_ingests_pages_in_open_state(
    alembic_db: object, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = alembic_db
    # repo.create_bug uses engine_module.SessionLocal — that's rebound by the
    # alembic_db fixture, so the poller and the test share the same DB.
    monkeypatch.setenv("NOTION_BUGS_DB_ID", "db-1")
    settings = _make_settings(token="secret_test", db_id="db-1")

    fake_client = MagicMock()
    fake_client.list_open_bugs.return_value = [
        _raw_page("p-1", "First bug"),
        _raw_page("p-2", "Second bug"),
    ]

    poller = NotionPoller(
        settings=settings,
        client_factory=lambda _t: fake_client,
    )

    count = await poller.poll_once()

    assert count == 2
    bugs = repo.list_bugs(db_session)
    states = {b.notion_page_id: b.state for b in bugs}
    assert states == {"p-1": BugState.OPEN.value, "p-2": BugState.OPEN.value}


async def test_poll_once_is_idempotent_on_repeated_polls(
    alembic_db: object, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = alembic_db
    monkeypatch.setenv("NOTION_BUGS_DB_ID", "db-1")
    settings = _make_settings(token="secret_test", db_id="db-1")

    fake_client = MagicMock()
    fake_client.list_open_bugs.return_value = [_raw_page("same-1", "t")]

    poller = NotionPoller(
        settings=settings,
        client_factory=lambda _t: fake_client,
    )

    await poller.poll_once()
    await poller.poll_once()
    await poller.poll_once()

    bugs = repo.list_bugs(db_session, include_terminal=True)
    assert len(bugs) == 1
    assert bugs[0].notion_page_id == "same-1"


# --- consecutive-failure tracking --------------------------------------------


async def test_three_failures_trigger_repeated_failure_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each failed tick increments the counter; the 3rd publishes the
    repeated-failure event on both the system and queue topics."""
    monkeypatch.setenv("NOTION_BUGS_DB_ID", "db-1")
    settings = _make_settings(token="secret_test", db_id="db-1")

    # Capture events by patching bus.publish_sync.
    published: list[tuple[str, dict[str, Any]]] = []

    def _fake_publish_sync(topic: str, payload: dict[str, Any]) -> None:
        published.append((topic, payload))

    monkeypatch.setattr(poller_mod.bus, "publish_sync", _fake_publish_sync)

    fake_client = MagicMock()
    fake_client.list_open_bugs.side_effect = RuntimeError("boom")

    poller = NotionPoller(
        settings=settings,
        client_factory=lambda _t: fake_client,
    )

    for _ in range(3):
        await poller.poll_once()

    assert poller._consecutive_failures == 3  # noqa: SLF001

    # Event bus got exactly one `system`-topic publish.
    system_events = [p for p in published if p[0] == poller_mod.SYSTEM_TOPIC]
    assert len(system_events) == 1
    _, payload = system_events[0]
    assert payload["type"] == "notion_poll_repeated_failure"
    assert payload["consecutive_failures"] == 3
    assert "boom" in payload["error"]

    # And a queue-topic copy.
    queue_events = [p for p in published if p[0] == "bugs:queue"]
    assert len(queue_events) == 1



async def test_failure_counter_resets_on_success(
    alembic_db: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = alembic_db
    monkeypatch.setenv("NOTION_BUGS_DB_ID", "db-1")
    settings = _make_settings(token="secret_test", db_id="db-1")

    # Two failures, then success, then one more failure → counter should
    # have been 2 before success, 0 after, 1 after the trailing failure.
    fake_client = MagicMock()
    fake_client.list_open_bugs.side_effect = [
        RuntimeError("boom-1"),
        RuntimeError("boom-2"),
        [],
        RuntimeError("boom-3"),
    ]

    poller = NotionPoller(
        settings=settings,
        client_factory=lambda _t: fake_client,
    )

    await poller.poll_once()
    await poller.poll_once()
    assert poller._consecutive_failures == 2  # noqa: SLF001
    await poller.poll_once()
    assert poller._consecutive_failures == 0  # noqa: SLF001
    await poller.poll_once()
    assert poller._consecutive_failures == 1  # noqa: SLF001


# --- bad page tolerance ------------------------------------------------------


async def test_one_malformed_page_does_not_kill_tick(
    alembic_db: object, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = alembic_db
    monkeypatch.setenv("NOTION_BUGS_DB_ID", "db-1")
    settings = _make_settings(token="secret_test", db_id="db-1")

    from ra_triage.integrations.notion import RawNotionBug

    # First page is a valid raw; second page_id is empty → skipped;
    # third is valid.
    fake_client = MagicMock()
    fake_client.list_open_bugs.return_value = [
        RawNotionBug(page=_bug_page("good-1")),
        RawNotionBug(page={"id": "", "properties": {}}),
        RawNotionBug(page=_bug_page("good-2")),
    ]

    poller = NotionPoller(
        settings=settings,
        client_factory=lambda _t: fake_client,
    )

    count = await poller.poll_once()

    assert count == 2
    bugs = repo.list_bugs(db_session)
    assert sorted(b.notion_page_id for b in bugs) == ["good-1", "good-2"]


# --- direct NormalizedBug round-trip (smoke) --------------------------------


def test_normalizedbug_feeds_create_bug(
    alembic_db: object, db_session: Session
) -> None:
    _ = alembic_db
    # Sanity check: a NormalizedBug's fields align with create_bug's kwargs.
    normalized = NormalizedBug(
        notion_page_id="x-1",
        title="T",
        description="D",
        repro_steps="R",
        severity="High",
        reporter="Megha",
        notion_status="Open",
    )
    bug = repo.create_bug(
        db_session,
        notion_page_id=normalized.notion_page_id,
        project_id="ra_avm",
        title=normalized.title,
        description=normalized.description,
        repro_steps=normalized.repro_steps,
        severity=normalized.severity,
        reporter=normalized.reporter,
    )
    db_session.commit()
    assert bug.state == BugState.OPEN.value
    assert bug.severity == "High"
    assert bug.reporter == "Megha"
