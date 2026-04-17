"""WebSocket channels for live updates.

Channels:
  - /ws/bugs            — queue-wide state-change broadcast (Agent 2)
  - /ws/bugs/{bug_id}/logs — per-bug agent stdout (placeholder; Agent 5)
  - /ws/test-instance   — test-instance log stream (placeholder; Agent 7)

Every frame is JSON with a `type` discriminator per tech plan §7.2.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ra_triage.utils.events import bus, topic_bugs_queue

router = APIRouter(tags=["websockets"])


async def _forward_until_disconnect(
    websocket: WebSocket, queue: asyncio.Queue[dict[str, Any]]
) -> None:
    """Forward every event on `queue` to `websocket` until it disconnects.

    Runs a small concurrent reader that drains client messages — without it,
    `receive_*` errors on disconnect take longer to surface.
    """

    async def _drain_client() -> None:
        """Read and discard any inbound frames; exit on disconnect."""
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            return
        except Exception:
            return

    reader = asyncio.create_task(_drain_client())
    try:
        while True:
            if reader.done():
                return
            try:
                # Short timeout so we can periodically check the reader task.
                payload = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            try:
                await websocket.send_json(payload)
            except Exception:
                return
    finally:
        reader.cancel()
        try:
            await reader
        except (asyncio.CancelledError, Exception):
            pass


@router.websocket("/ws/bugs")
async def ws_bugs(websocket: WebSocket) -> None:
    """Queue-wide bug updates. Emits a `hello` frame on connect, then
    forwards every `state_change` event published to `bugs:queue`."""
    await websocket.accept()
    await websocket.send_json({"type": "hello"})
    queue = await bus.subscribe(topic_bugs_queue())
    try:
        await _forward_until_disconnect(websocket, queue)
    except WebSocketDisconnect:
        pass
    finally:
        await bus.unsubscribe(topic_bugs_queue(), queue)


@router.websocket("/ws/bugs/{bug_id}/logs")
async def ws_bug_logs(websocket: WebSocket, bug_id: int) -> None:
    """Per-bug live log stream.

    Placeholder — accepts connection, emits a `hello` frame including the
    bug_id, then idles. Agent 5 wires up real agent-stdout forwarding here.
    """
    await websocket.accept()
    await websocket.send_json({"type": "hello", "bug_id": bug_id})
    try:
        while True:
            # Drain any client frames so disconnects surface promptly.
            await websocket.receive_text()
    except WebSocketDisconnect:
        return
    except Exception:
        return


@router.websocket("/ws/test-instance")
async def ws_test_instance(websocket: WebSocket) -> None:
    """Current test-instance log stream.

    Placeholder — accepts connection, emits a `hello` frame, then idles.
    Agent 7 wires up real dev-server stdout forwarding here.
    """
    await websocket.accept()
    await websocket.send_json({"type": "hello"})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        return
    except Exception:
        return
