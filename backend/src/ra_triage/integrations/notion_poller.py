"""Notion bug-tracker poller.

Architecture
------------

One `NotionPoller` instance lives on the FastAPI app's lifespan. It owns an
APScheduler `AsyncIOScheduler` that fires `poll_once()` every 5 minutes.

Design goals (from orchestrator instructions + tech plan §12.1, §16):

1. **Never crash the backend on a poll failure.** The scheduled job catches
   everything, logs, and continues. Only a fatal config problem raises —
   specifically, never at tick time.
2. **Disabled cleanly when creds are missing.** If `NOTION_TOKEN` or
   `NOTION_BUGS_DB_ID` is unset at startup, we log a warning and do NOT
   register the job at all. `make dev` must work without Notion creds.
3. **3-failure escalation stub.** Consecutive failures are counted; on the
   third consecutive failure we emit a structured log line
   `NOTION_POLL_REPEATED_FAILURE` and publish a `system` event on the event
   bus. Agent 9 replaces this with a real Slack webhook post.
4. **Idempotent ingestion.** Each tick creates bugs via
   `db.repo.create_bug`, which is already idempotent on `notion_page_id`.
   Re-polling the same open bugs is a no-op.

The poller is deliberately non-async-facing: `poll_once()` is `async def`
so APScheduler's AsyncIOScheduler can await it directly, but its body
dispatches to a sync helper that owns a DB session. This matches how
`repo.create_bug` and friends are written.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ra_triage.config.registry import ProjectConfig, get_project
from ra_triage.db import engine as engine_module
from ra_triage.db import repo
from ra_triage.integrations.notion import NotionClient, normalize_page
from ra_triage.settings import Settings, get_settings
from ra_triage.utils.events import bus, topic_bugs_queue

logger = logging.getLogger(__name__)


#: Tick interval for the APScheduler job. 5 minutes matches product plan §1.
POLL_INTERVAL_MINUTES: int = 5

#: Number of consecutive poll failures that triggers the escalation event.
FAILURE_THRESHOLD: int = 3

#: Topic emitted on repeated failure. Agent 9's Slack integration subscribes.
SYSTEM_TOPIC: str = "system"


class NotionPoller:
    """Owns the APScheduler job that ingests Notion bugs."""

    def __init__(
        self,
        *,
        project_id: str = "ra_avm",
        settings: Settings | None = None,
        client_factory: Any = None,
    ) -> None:
        """Build a poller for the given project.

        Args:
          project_id: which entry in the registry to poll. Defaults to
            `ra_avm` — v1 has only that one.
          settings: injected Settings for tests; falls back to `get_settings()`.
          client_factory: optional callable `(token) -> NotionClient`.
            Tests use this to swap in a fake. Defaults to `NotionClient`.
        """
        self._project_id = project_id
        self._settings = settings or get_settings()
        self._client_factory = client_factory or NotionClient
        self._scheduler: AsyncIOScheduler | None = None
        self._consecutive_failures: int = 0
        self._registered: bool = False

    # -- lifecycle --------------------------------------------------------

    @property
    def is_registered(self) -> bool:
        """True if start() actually registered the APScheduler job."""
        return self._registered

    def start(self) -> None:
        """Register the APScheduler job if Notion creds are present.

        No-op (with a warning) if `NOTION_TOKEN` or `NOTION_BUGS_DB_ID` is
        missing. That keeps `/health` and the rest of the backend working
        in dev without Notion credentials.
        """
        if not self._settings.notion_token:
            logger.warning(
                "Notion poller disabled: NOTION_TOKEN or NOTION_BUGS_DB_ID not set"
            )
            return
        if not self._settings.notion_bugs_db_id:
            logger.warning(
                "Notion poller disabled: NOTION_TOKEN or NOTION_BUGS_DB_ID not set"
            )
            return

        self._scheduler = AsyncIOScheduler()
        self._scheduler.add_job(
            self.poll_once,
            trigger="interval",
            minutes=POLL_INTERVAL_MINUTES,
            id="notion_poller",
            max_instances=1,  # don't stack ticks if one is slow
            coalesce=True,  # drop missed ticks, don't replay
            next_run_time=None,  # wait for first interval; operators can
            # call poll_once() directly for an immediate run
        )
        self._scheduler.start()
        self._registered = True
        logger.info(
            "Notion poller registered: interval=%dmin project_id=%s",
            POLL_INTERVAL_MINUTES,
            self._project_id,
        )

    def stop(self) -> None:
        """Shutdown the scheduler. Safe to call even if never started."""
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                logger.exception("Error shutting down Notion poller scheduler")
            self._scheduler = None
        self._registered = False

    # -- polling ----------------------------------------------------------

    async def poll_once(self) -> int:
        """Run one polling tick.

        Returns the number of pages ingested (0 on failure). Never raises.
        Failures are logged; repeated failures trigger the escalation event.
        """
        try:
            count = await asyncio.to_thread(self._poll_once_sync)
        except Exception as exc:
            # Belt-and-braces — _poll_once_sync already catches, but if the
            # thread itself fails (unlikely) we still don't crash.
            logger.exception("Notion poll tick failed unexpectedly")
            self._record_failure(exc)
            return 0
        return count

    def _poll_once_sync(self) -> int:
        """Synchronous body of a poll tick.

        Returns number of pages ingested on success, or 0 on failure (and
        records the failure for consecutive-failure tracking).
        """
        project: ProjectConfig
        try:
            project = get_project(self._project_id)
        except KeyError as exc:
            logger.error("Notion poller: unknown project_id=%s", self._project_id)
            self._record_failure(exc)
            return 0

        try:
            database_id = project.require_notion_db_id()
        except RuntimeError as exc:
            logger.warning("Notion poller: %s", exc)
            self._record_failure(exc)
            return 0

        try:
            client = self._client_factory(self._settings.notion_token)
        except Exception as exc:
            logger.exception("Notion poller: failed to build NotionClient")
            self._record_failure(exc)
            return 0

        try:
            raw_pages = client.list_open_bugs(database_id)
        except Exception as exc:
            logger.exception(
                "Notion poller: list_open_bugs failed (database_id=%s)", database_id
            )
            self._record_failure(exc)
            return 0

        ingested = 0
        for raw in raw_pages:
            try:
                normalized = normalize_page(raw.page)
            except Exception:
                logger.exception(
                    "Notion poller: normalize_page failed for page=%s",
                    raw.page_id,
                )
                continue
            if not normalized.notion_page_id:
                logger.warning(
                    "Notion poller: page with empty id, skipping"
                )
                continue
            try:
                self._ingest_one(project, normalized)
                ingested += 1
            except Exception:
                logger.exception(
                    "Notion poller: create_bug failed for page=%s",
                    normalized.notion_page_id,
                )
                # One bad page shouldn't kill the tick.
                continue

        self._record_success(ingested)
        return ingested

    def _ingest_one(self, project: ProjectConfig, normalized: Any) -> None:
        """Open a session, call `create_bug` idempotently, commit."""
        session = engine_module.SessionLocal()
        try:
            repo.create_bug(
                session,
                notion_page_id=normalized.notion_page_id,
                project_id=project.id,
                title=normalized.title,
                description=normalized.description,
                repro_steps=normalized.repro_steps,
                severity=normalized.severity,
                reporter=normalized.reporter,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- failure tracking -------------------------------------------------

    def _record_success(self, ingested: int) -> None:
        """Reset failure counter after a successful tick."""
        if self._consecutive_failures:
            logger.info(
                "Notion poller recovered after %d consecutive failures",
                self._consecutive_failures,
            )
        self._consecutive_failures = 0
        logger.debug("Notion poll tick succeeded: ingested=%d", ingested)

    def _record_failure(self, exc: BaseException) -> None:
        """Increment failure counter and fire the escalation event on the 3rd.

        We always log at WARNING on every failure and escalate only at
        `FAILURE_THRESHOLD`. Once escalated we don't re-fire until a
        success resets the counter.
        """
        self._consecutive_failures += 1
        logger.warning(
            "Notion poll tick failed (consecutive_failures=%d): %s",
            self._consecutive_failures,
            exc,
        )
        if self._consecutive_failures == FAILURE_THRESHOLD:
            logger.error(
                "NOTION_POLL_REPEATED_FAILURE consecutive_failures=%d last_error=%r",
                self._consecutive_failures,
                str(exc),
            )
            payload: dict[str, Any] = {
                "type": "notion_poll_repeated_failure",
                "consecutive_failures": self._consecutive_failures,
                "error": str(exc),
            }
            # Both `system` (Agent 9 listens) and the queue topic (visible
            # to the frontend's WS) get a copy so operators see it without
            # waiting on Agent 9.
            try:
                bus.publish_sync(SYSTEM_TOPIC, payload)
                bus.publish_sync(topic_bugs_queue(), payload)
            except Exception:
                logger.exception(
                    "Notion poller: failed to publish repeated-failure event"
                )


__all__ = [
    "FAILURE_THRESHOLD",
    "NotionPoller",
    "POLL_INTERVAL_MINUTES",
    "SYSTEM_TOPIC",
]
