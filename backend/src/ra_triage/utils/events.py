"""In-process async pub/sub event bus.

Single-process only; this is the substrate for WebSocket fan-out inside one
FastAPI process. Agent 5 extends this for agent-stdout streaming; Agent 2
uses it only for bug state changes.

Design:
- Subscribers receive their own `asyncio.Queue` per topic subscription.
- Publishers are non-blocking: a publish that has no subscribers is a no-op.
- `publish()` is async and meant to be awaited from within the event loop.
- `publish_sync()` is provided for synchronous callers (DB repo). It first
  tries the currently-running loop (works when the caller is on the same
  thread as asyncio). If there isn't one, it falls back to a loop that was
  registered via `attach_loop(loop)` — the FastAPI lifespan attaches the
  server loop so sync DB-repo code on the threadpool can still publish.
  Without either, the publish is silently dropped (unit tests without a
  loop).

Topics used in this PR:
  - "bugs:queue" — queue-wide broadcast for state changes
  - "bugs:{bug_id}:state" — per-bug state change
Agent 5 will add:
  - "bugs:{bug_id}:logs" — per-bug agent stdout
"""

from __future__ import annotations

import asyncio
from typing import Any


class EventBus:
    """Topic-keyed asyncio pub/sub."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()
        # Loop registered via attach_loop() for cross-thread publishing.
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember an event loop for cross-thread `publish_sync` calls."""
        self._loop = loop

    def detach_loop(self) -> None:
        """Forget the registered loop (app shutdown / test teardown)."""
        self._loop = None

    def get_loop(self) -> asyncio.AbstractEventLoop | None:
        """Return the registered loop (or None).

        Public accessor so sync callers outside the bus can dispatch
        coroutines onto the FastAPI app loop without reaching into the
        bus's private ``_loop`` attribute. Returns ``None`` if no loop is
        attached or the attached loop is closed.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return None
        return loop

    async def subscribe(self, topic: str) -> asyncio.Queue[dict[str, Any]]:
        """Register a new subscriber on `topic` and return its queue.

        Also refreshes the loop reference on every subscribe — tests that run
        sequentially often create and close their own loop per test, so the
        bus must re-learn it each time.
        """
        async with self._lock:
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            self._subs.setdefault(topic, set()).add(queue)
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
            return queue

    async def unsubscribe(
        self, topic: str, queue: asyncio.Queue[dict[str, Any]]
    ) -> None:
        """Remove a subscriber. Silently no-ops if already gone."""
        async with self._lock:
            subs = self._subs.get(topic)
            if not subs:
                return
            subs.discard(queue)
            if not subs:
                self._subs.pop(topic, None)

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Fan out `payload` to every current subscriber of `topic`."""
        async with self._lock:
            targets = list(self._subs.get(topic, ()))
        for queue in targets:
            queue.put_nowait(payload)

    def publish_sync(self, topic: str, payload: dict[str, Any]) -> None:
        """Synchronous publisher for non-async callers.

        Order of preference for finding a loop to schedule on:
          1. A loop currently running on this thread.
          2. A loop registered via `attach_loop()` or captured by subscribe().
          3. Drop the publish (unit tests without a subscriber).

        If the registered loop has since been closed (common across repeated
        TestClient invocations using the module-global bus), we drop it and
        silently return — the next `subscribe()` on a live loop will refresh it.
        """
        loop: asyncio.AbstractEventLoop | None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = self._loop
        if loop is None:
            return
        if loop.is_closed():
            self._loop = None
            return
        try:
            loop.call_soon_threadsafe(
                lambda: loop.create_task(self.publish(topic, payload))
            )
        except RuntimeError:
            # Race: loop may have just been closed. Clear and give up.
            self._loop = None


# Module-global bus. Agents and WS handlers share this single instance.
bus = EventBus()


def topic_bugs_queue() -> str:
    """Queue-wide topic name (state-change broadcasts)."""
    return "bugs:queue"


def topic_bug_state(bug_id: int) -> str:
    """Per-bug state-change topic name."""
    return f"bugs:{bug_id}:state"


def topic_bug_logs(bug_id: int) -> str:
    """Per-bug agent-stdout topic name."""
    return f"bugs:{bug_id}:logs"


def publish_agent_stdout(
    bug_id: int, role: str, attempt: int, text: str
) -> None:
    """Publish one agent stdout line onto ``bugs:{bug_id}:logs``.

    Thin wrapper so every caller emits the same payload shape — the WS
    handler at ``/ws/bugs/:id/logs`` and the frontend ``useLiveLogs`` hook
    both assume ``type=="agent_stdout"`` with the exact keys below. Keep the
    shape stable.

    Uses ``publish_sync`` so the base-class subprocess streamer can call this
    from an async reader task without awaiting (matches existing
    state-change publishers in ``db.repo``).
    """
    payload: dict[str, Any] = {
        "type": "agent_stdout",
        "bug_id": bug_id,
        "role": role,
        "attempt": attempt,
        "text": text,
    }
    bus.publish_sync(topic_bug_logs(bug_id), payload)
