"""FastAPI app factory and lifespan hooks for RA Triage backend.

v1 scaffold: minimal app. Agent 2 mounts the bug/project routers plus the
WebSocket channels. Agent 3 adds the Notion poller lifecycle. Later agents
layer in test-instance, escalations, and settings routers.

Agent 12 wires:
  - real ``resume_inflight`` on startup; per-report dispatch:
      RESUME_PIPELINE → ``asyncio.create_task(run_pipeline(bug_id))``
      RESUME_POST_RECOVERY → schedule the merge probe coroutine
      NEEDS_MANUAL_REVIEW → log only (flag was set inside resume)
  - SIGTERM handler that calls :func:`graceful_shutdown` so in-flight
    agent subprocesses get a chance to finish within a 30s budget
    before being cancelled.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ra_triage.api.bugs import router as bugs_router
from ra_triage.api.escalations import router as escalations_router
from ra_triage.api.projects import router as projects_router
from ra_triage.api.test_instances import router as test_instances_router
from ra_triage.api.ws import router as ws_router
from ra_triage.integrations.notion_poller import NotionPoller
from ra_triage.orchestrator.pipeline import run_pipeline
from ra_triage.orchestrator.resume import (
    ResumeDecision,
    ResumeReport,
    resume_inflight,
)
from ra_triage.orchestrator.shutdown import graceful_shutdown, shutdown
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

    # Materialize the shutdown event on the running loop so the SIGTERM
    # handler can safely .set() it.
    shutdown.ensure_event()

    # Agent 12: real resume — re-spawn pipelines for non-terminal bugs.
    # The lifespan keeps a handle on every dispatched task so the
    # graceful_shutdown helper can wait on them at the end.
    app.state.agent_tasks = set()
    try:
        reports = await resume_inflight()
        for report in reports:
            _dispatch_resume_report(app, report)
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

    # Agent 12: install a SIGTERM handler that triggers graceful_shutdown.
    # SIGINT (Ctrl-C) is handled by uvicorn's own loop signal handler;
    # we register SIGTERM only so external supervisors (Procfile/foreman,
    # systemd, etc.) get clean shutdowns. Best-effort — on platforms that
    # don't support add_signal_handler (unlikely on macOS but defensive)
    # we fall back to lifespan-only cleanup.
    loop = asyncio.get_running_loop()
    sigterm_installed = False
    try:
        loop.add_signal_handler(signal.SIGTERM, _on_sigterm, app)
        sigterm_installed = True
    except (NotImplementedError, RuntimeError):
        logger.warning(
            "SIGTERM handler not installable on this platform; relying on "
            "FastAPI lifespan exit for graceful shutdown"
        )

    try:
        yield
    finally:
        # Lifespan exit also runs the graceful_shutdown sequence so
        # uvicorn's own SIGINT path produces the same clean teardown.
        try:
            await graceful_shutdown(
                test_manager=manager,
                agent_tasks=list(app.state.agent_tasks),
                deadline_s=30.0,
            )
        except Exception:
            logger.exception("graceful_shutdown raised during lifespan exit")
        if sigterm_installed:
            try:
                loop.remove_signal_handler(signal.SIGTERM)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        try:
            poller.stop()
        except Exception:
            logger.exception("notion poller stop raised")
        bus.detach_loop()


def _on_sigterm(app: FastAPI) -> None:
    """Signal handler entry point — must be sync, must not block.

    Schedules :func:`graceful_shutdown` on the running loop and trips
    the global flag immediately so any pipeline coroutine that
    checkpoints between now and the helper running sees the flag set.
    """
    logger.info("SIGTERM received — initiating graceful shutdown")
    shutdown.trigger()
    loop = asyncio.get_event_loop()
    manager = getattr(app.state, "test_instance_manager", None)
    tasks = list(getattr(app.state, "agent_tasks", []))
    loop.create_task(
        graceful_shutdown(
            test_manager=manager,
            agent_tasks=tasks,
            deadline_s=30.0,
        ),
        name="graceful_shutdown",
    )


def _dispatch_resume_report(app: FastAPI, report: ResumeReport) -> None:
    """Dispatch one ResumeReport per Agent 12's lifespan contract.

    - RESUME_PIPELINE → ``asyncio.create_task(run_pipeline(bug_id))``
    - RESUME_POST_RECOVERY → schedule the merge probe coroutine. Imported
      lazily to keep the module's import surface small.
    - NEEDS_MANUAL_REVIEW → log only; the flag was already set by
      ``resume_inflight``.
    - ALREADY_TERMINAL → no-op (terminal bugs aren't in the iteration
      anyway, but defensive).
    """
    if report.decision == ResumeDecision.RESUME_PIPELINE:
        task = asyncio.create_task(
            run_pipeline(report.bug_id),
            name=f"resume:run_pipeline:{report.bug_id}",
        )
        _track_task(app, task)
        logger.info(
            "lifespan: scheduled run_pipeline(%d) — %s",
            report.bug_id,
            report.notes,
        )
        return
    if report.decision == ResumeDecision.RESUME_POST_RECOVERY:
        from ra_triage.orchestrator.resume_recovery import (
            recover_merging_bug,
        )

        task = asyncio.create_task(
            recover_merging_bug(report.bug_id),
            name=f"resume:recover_merging:{report.bug_id}",
        )
        _track_task(app, task)
        logger.info(
            "lifespan: scheduled merge recovery for bug %d — %s",
            report.bug_id,
            report.notes,
        )
        return
    if report.decision == ResumeDecision.NEEDS_MANUAL_REVIEW:
        logger.warning(
            "lifespan: bug %d needs manual review — %s",
            report.bug_id,
            report.notes,
        )
        return


def _track_task(app: FastAPI, task: asyncio.Task) -> None:
    """Add the task to ``app.state.agent_tasks`` and arrange auto-removal."""
    bag: set[asyncio.Task] = app.state.agent_tasks
    bag.add(task)
    task.add_done_callback(bag.discard)


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
    app.include_router(escalations_router)
    app.include_router(test_instances_router)
    app.include_router(ws_router)

    return app


app = create_app()
