"""FastAPI app factory and lifespan hooks for RA Triage backend.

v1 scaffold: minimal app. Agent 2 mounts the bug/project routers plus the
WebSocket channels. Agent 3 adds the Notion poller lifecycle. Later agents
layer in test-instance, escalations, and settings routers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ra_triage.api.bugs import router as bugs_router
from ra_triage.api.projects import router as projects_router
from ra_triage.api.test_instances import router as test_instances_router
from ra_triage.api.ws import router as ws_router
from ra_triage.integrations.notion_poller import NotionPoller
from ra_triage.orchestrator.resume import resume_inflight
from ra_triage.settings import get_settings
from ra_triage.test_runner.instance import get_test_instance_manager
from ra_triage.utils.events import bus

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: start + stop the Notion poller around serving.

    The poller is a no-op if credentials aren't configured — so this hook
    always runs successfully, even in dev without a `.env`.
    """
    # Register the running loop on the event bus so sync-thread publishers
    # (repo.update_bug_state on FastAPI's threadpool) can schedule onto it.
    try:
        bus.attach_loop(asyncio.get_running_loop())
    except RuntimeError:
        pass

    # Agent 5: log any non-terminal bugs that existed before restart.
    # Agent 12 will replace this with real pipeline re-spawn.
    try:
        await resume_inflight()
    except Exception:
        logger.exception("resume_inflight failed during lifespan startup")

    poller = NotionPoller()
    poller.start()
    app.state.notion_poller = poller

    # Agent 7: reap crashed test-instance dev-servers every 1s. The manager
    # is idempotent; reaper_forever loops until shutdown() is called below.
    manager = get_test_instance_manager()
    manager.start_reaper()
    app.state.test_instance_manager = manager
    try:
        yield
    finally:
        try:
            await manager.shutdown()
        except Exception:
            logger.exception("test instance manager shutdown failed")
        poller.stop()
        bus.detach_loop()


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    settings = get_settings()
    app = FastAPI(
        title="RA Triage",
        version="0.1.0",
        description="RA Triage backend — multi-agent local bug-fix ops tool.",
        lifespan=_lifespan,
    )

    # Localhost-only CORS — the Vite dev server at 127.0.0.1:1313 calls us from
    # the browser, so we must allow that specific origin. No wildcards.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.triage_frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        """Liveness probe for Triage itself (not the AVM app)."""
        return {"status": "ok"}

    app.include_router(projects_router)
    app.include_router(bugs_router)
    app.include_router(test_instances_router)
    app.include_router(ws_router)

    return app


app = create_app()
