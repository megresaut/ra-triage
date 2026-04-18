"""Graceful-shutdown coordination for the orchestrator (Agent 12).

The backend runs many long-lived asyncio tasks: per-bug ``run_pipeline``
coroutines, the test-instance reaper, the Notion poller, escalation hooks.
On a clean SIGTERM (the normal way Megha stops the dev server, and the way
``make dev``'s process supervisor fires shutdown), we want to:

  1. Stop accepting new pipeline admissions.
  2. Let already-spawned agent subprocesses finish their current step
     within a budget (default 30s). If they exceed it, force-cancel.
  3. Tell every connected WS client we're shutting down so they can
     pause polling instead of seeing a wall of disconnect errors.
  4. Tear down the test-instance manager (which itself stops dev servers).

Implementation strategy: a single :class:`ShutdownFlag` per process. The
pipeline coroutines check ``shutdown.is_shutting_down()`` between state
transitions and bail cleanly (leaving the bug in its current state — the
next boot's :func:`orchestrator.resume.resume_inflight` re-spawns from
there). The flag is set by:

  - The SIGTERM handler installed by ``main.py`` lifespan.
  - FastAPI's lifespan-exit hook (covers SIGINT/Ctrl-C and uvicorn's
    own shutdown sequence).
  - Tests that want to exercise the interrupt path.

The flag is NEVER cleared in production; once set, the process is
shutting down and no further work should be admitted. The
:func:`reset_for_tests` helper is the only legal way to clear it (and
exists solely so test cases don't bleed into each other).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)


class ShutdownFlag:
    """Process-wide flag set by SIGTERM handler / lifespan exit.

    Pipeline coroutines and other long-lived tasks check
    :meth:`is_shutting_down` between checkpoints and short-circuit out
    cleanly when it returns ``True``. The flag is monotonic: once set,
    it stays set for the life of the process (only :func:`reset_for_tests`
    clears it, and that helper is unit-test scaffolding only).
    """

    def __init__(self) -> None:
        self._flag: bool = False
        # asyncio.Event lets callers ``await`` shutdown — used by the
        # graceful-shutdown helper for "wait for trigger from any source".
        self._event: asyncio.Event | None = None

    def is_shutting_down(self) -> bool:
        """Return ``True`` once :meth:`trigger` has been called."""
        return self._flag

    def trigger(self) -> None:
        """Set the shutdown flag. Idempotent.

        Safe to call from a signal handler (synchronous, no allocations
        beyond a bool flip + Event.set). The async-flavored Event is
        only updated when the loop is alive; signal handlers may fire
        before the loop is started, in which case the Event lazily
        materializes on the next ``ensure_event()`` call.
        """
        if self._flag:
            return
        self._flag = True
        if self._event is not None:
            try:
                self._event.set()
            except RuntimeError:  # pragma: no cover - defensive
                # Event tied to a closed loop; nothing to do.
                pass
        logger.info("ShutdownFlag.trigger(): shutdown initiated")

    def ensure_event(self) -> asyncio.Event:
        """Materialize an :class:`asyncio.Event` bound to the running loop.

        Lazy because the flag is constructed at module-import time but the
        loop doesn't exist until FastAPI's lifespan starts it. Re-creates
        the Event if the previous one was bound to a now-closed loop
        (test fixtures cycle loops between cases).
        """
        loop_alive = False
        if self._event is not None:
            try:
                # If the Event was bound to a closed loop, accessing _loop
                # raises; rebuild on any failure.
                loop_alive = not self._event._loop.is_closed()  # type: ignore[attr-defined]
            except Exception:
                loop_alive = False
        if self._event is None or not loop_alive:
            self._event = asyncio.Event()
            if self._flag:
                self._event.set()
        return self._event


# Module-global flag. Pipeline + main.py share this instance.
shutdown = ShutdownFlag()


def reset_for_tests() -> None:
    """Clear the global flag — UNIT TESTS ONLY.

    Any production caller that "needs to clear the flag" is wrong: the
    process should restart instead. Tests that exercise the trigger
    path call this in teardown so the next test starts from a clean slate.

    We mutate the existing :data:`shutdown` object in place rather than
    rebinding the module attribute — other modules import ``shutdown``
    as a named reference at import time (e.g.
    ``from ...shutdown import shutdown as shutdown_flag``), so a rebind
    wouldn't reach them. Mutating the existing object keeps those
    references live.
    """
    shutdown._flag = False
    shutdown._event = None


async def graceful_shutdown(
    *,
    test_manager: Any | None = None,
    agent_tasks: Iterable[asyncio.Task[Any]] | None = None,
    deadline_s: float = 30.0,
) -> None:
    """Run the full graceful-shutdown sequence.

    Steps:
      1. ``shutdown.trigger()`` — sets the global flag so pipeline
         coroutines bail at their next checkpoint.
      2. Broadcast ``{"type": "backend_shutting_down"}`` on the queue
         topic so connected WS clients can show a "backend is restarting"
         banner instead of disconnect errors.
      3. ``await asyncio.wait(agent_tasks, timeout=deadline_s)`` — let
         in-flight agent tasks finish their current step (bounded by the
         deadline). The pipeline body checks ``is_shutting_down()`` between
         transitions, so a clean run will exit at the next checkpoint.
      4. Cancel anything still running past the deadline. Cancellation
         propagates through ``async with concurrency.PIPELINE_SEMAPHORE``
         so the semaphore is correctly released.
      5. Stop the test-instance manager (which stops the reaper + any
         live dev servers under it).

    Never raises: every step is wrapped so the worst failure is a logged
    exception, not a crash that strands resources.
    """
    shutdown.trigger()

    # 2. Tell WS clients we're going down. Best-effort; if the bus has no
    # subscribers (tests), this is a no-op.
    try:
        from ra_triage.utils.events import bus, topic_bugs_queue

        await bus.publish(
            topic_bugs_queue(),
            {"type": "backend_shutting_down"},
        )
    except Exception:
        logger.exception("graceful_shutdown: WS broadcast failed (continuing)")

    # 3. Wait for agent tasks within the deadline.
    tasks = [t for t in (agent_tasks or ()) if not t.done()]
    if tasks:
        logger.info(
            "graceful_shutdown: waiting up to %.1fs for %d agent task(s)",
            deadline_s,
            len(tasks),
        )
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=deadline_s
            )
        except Exception:
            logger.exception(
                "graceful_shutdown: asyncio.wait raised (force-cancelling)"
            )
            done, pending = set(), set(tasks)

        # 4. Cancel still-running tasks.
        for t in pending:
            logger.warning(
                "graceful_shutdown: cancelling task %r past deadline",
                t.get_name(),
            )
            t.cancel()
        if pending:
            try:
                await asyncio.wait(pending, timeout=5.0)
            except Exception:
                logger.exception(
                    "graceful_shutdown: post-cancel wait raised"
                )
        logger.info(
            "graceful_shutdown: %d task(s) finished cleanly, %d cancelled",
            len(done),
            len(pending),
        )

    # 5. Stop the test-instance manager (its own shutdown is async-safe).
    if test_manager is not None:
        try:
            await test_manager.shutdown()
        except Exception:
            logger.exception("graceful_shutdown: test_manager.shutdown raised")


__all__ = [
    "ShutdownFlag",
    "graceful_shutdown",
    "reset_for_tests",
    "shutdown",
]
