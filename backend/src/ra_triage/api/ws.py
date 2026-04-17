"""WebSocket channels for live updates.

Channels:
  - /ws/bugs            — queue-wide state-change broadcast (Agent 2)
  - /ws/bugs/{bug_id}/logs — per-bug agent stdout (Agent 5)
  - /ws/test-instance   — test-instance log stream (placeholder; Agent 7)

Every frame is JSON with a `type` discriminator per tech plan §7.2.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module
from ra_triage.test_runner.instance import (
    get_test_instance_manager,
    most_recent_instance_snapshot,
)
from ra_triage.utils.events import (
    bus,
    topic_bug_logs,
    topic_bugs_queue,
    topic_test_instance_logs,
    topic_test_instance_status,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websockets"])


#: Max number of historical log lines replayed on a fresh WS connection.
#: Gives reconnecting clients immediate context without dumping gigabytes.
LOG_BACKFILL_LINES: int = 200


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

    Subscribes to the per-bug logs topic and forwards every
    ``agent_stdout`` event to the client. On connect, also replays the
    last ``LOG_BACKFILL_LINES`` lines from the most recent agent_run's
    log file (if one exists) so reconnecting clients see context
    immediately rather than blank space until the next event.
    """
    await websocket.accept()
    await websocket.send_json({"type": "hello", "bug_id": bug_id})

    # Subscribe FIRST so we don't race-lose events between backfill and
    # live streaming.
    queue = await bus.subscribe(topic_bug_logs(bug_id))

    try:
        await _send_log_backfill(websocket, bug_id)
    except Exception:
        # Backfill failures should never sever the live connection.
        logger.exception("ws_bug_logs: backfill failed for bug %d", bug_id)

    try:
        await _forward_until_disconnect(websocket, queue)
    except WebSocketDisconnect:
        pass
    finally:
        await bus.unsubscribe(topic_bug_logs(bug_id), queue)


async def _send_log_backfill(websocket: WebSocket, bug_id: int) -> None:
    """Tail the last N lines from the most recent agent_run's log file.

    Emits them as ``agent_stdout_backfill`` frames with the same shape as
    live ``agent_stdout`` frames plus a ``backfill: true`` marker, so the
    frontend can dedupe / style them differently if it wants.
    """
    run_info = await asyncio.to_thread(_find_most_recent_run, bug_id)
    if run_info is None:
        return
    role, attempt, log_path_str = run_info
    log_path = Path(log_path_str)
    if not log_path.exists():
        return

    try:
        text = await asyncio.to_thread(log_path.read_text, "utf-8")
    except OSError:
        return
    lines = text.splitlines()
    tail = lines[-LOG_BACKFILL_LINES:] if len(lines) > LOG_BACKFILL_LINES else lines
    for line in tail:
        if not line:
            continue
        try:
            await websocket.send_json(
                {
                    "type": "agent_stdout",
                    "bug_id": bug_id,
                    "role": role,
                    "attempt": attempt,
                    "text": line,
                    "backfill": True,
                }
            )
        except Exception:
            return


def _find_most_recent_run(
    bug_id: int,
) -> tuple[str, int, str] | None:
    """Return ``(role, attempt, log_path)`` for the latest agent run, or None."""
    session = engine_module.SessionLocal()
    try:
        runs = repo_module.list_agent_runs(session, bug_id)
        if not runs:
            return None
        last = runs[-1]
        return (last.agent_role, last.attempt, last.log_path)
    finally:
        session.close()


#: Max log-file tail lines replayed from disk per repo on a fresh
#: ``/ws/test-instance`` connect. Per the Agent 7 brief: last 200 lines.
TEST_INSTANCE_BACKFILL_LINES: int = 200


@router.websocket("/ws/test-instance")
async def ws_test_instance(websocket: WebSocket) -> None:
    """Current test-instance log + status stream.

    On connect:
      1. Send a ``hello`` frame.
      2. Send a ``test_instance_status`` snapshot of the manager's current
         view (or the most recent DB row if nothing is live, so the UI can
         tell 'crashed' from 'never launched').
      3. Send up to :data:`TEST_INSTANCE_BACKFILL_LINES` recent log lines
         per repo as ``test_log`` backfill frames.

    After that, forward every ``test_log`` and ``test_instance_status``
    event published on the bus. Reconnect-friendly: every connect replays
    fresh backfill.
    """
    await websocket.accept()
    await websocket.send_json({"type": "hello"})

    # Subscribe to the status topic FIRST so transitions that happen during
    # backfill aren't missed. Same pattern as ws_bug_logs.
    logs_queue = await bus.subscribe(topic_test_instance_logs())
    status_queue = await bus.subscribe(topic_test_instance_status())

    try:
        await _send_test_instance_status_snapshot(websocket)
        await _send_test_instance_log_backfill(websocket)
    except Exception:
        logger.exception("test-instance backfill failed")

    try:
        await _forward_test_instance(websocket, logs_queue, status_queue)
    except WebSocketDisconnect:
        pass
    finally:
        await bus.unsubscribe(topic_test_instance_logs(), logs_queue)
        await bus.unsubscribe(topic_test_instance_status(), status_queue)


async def _send_test_instance_status_snapshot(websocket: WebSocket) -> None:
    """Send the manager's current view, or the most recent row, as a status frame."""
    manager = get_test_instance_manager()
    snapshot = await manager.current()
    if snapshot is None:
        # Fall back to DB for last-known state so the UI can paint
        # 'crashed'/'stopped' hints rather than a blank panel.
        snapshot = await asyncio.to_thread(most_recent_instance_snapshot)
    frame = {"type": "test_instance_status"}
    if snapshot is not None:
        frame.update(snapshot)
    else:
        frame["status"] = "none"
    try:
        await websocket.send_json(frame)
    except Exception:
        pass


async def _send_test_instance_log_backfill(websocket: WebSocket) -> None:
    """Tail each active instance's per-repo log file and emit backfill frames.

    We look at the manager's live view (if any) to know which log files to
    tail. If nothing is live, we skip backfill — the DB row only carries
    pid/port maps, not log paths, and we'd rather show an empty panel than
    fabricate a log path.
    """
    manager = get_test_instance_manager()
    snapshot = await manager.current()
    if snapshot is None:
        return
    inst = manager._current  # noqa: SLF001 — intentional cross-module peek
    if inst is None:
        return
    for repo_name, sp in inst.procs.items():
        try:
            if not sp.log_path.exists():
                continue
            text = await asyncio.to_thread(sp.log_path.read_text, "utf-8")
        except OSError:
            continue
        lines = text.splitlines()
        tail = (
            lines[-TEST_INSTANCE_BACKFILL_LINES:]
            if len(lines) > TEST_INSTANCE_BACKFILL_LINES
            else lines
        )
        for line in tail:
            if not line:
                continue
            try:
                await websocket.send_json(
                    {
                        "type": "test_log",
                        "bug_id": inst.bug_id,
                        "repo": repo_name,
                        "line": line,
                        "backfill": True,
                    }
                )
            except Exception:
                return


async def _forward_test_instance(
    websocket: WebSocket,
    logs_queue: asyncio.Queue[dict[str, Any]],
    status_queue: asyncio.Queue[dict[str, Any]],
) -> None:
    """Forward both the log + status queues to ``websocket`` until it disconnects.

    Mirrors ``_forward_until_disconnect`` but pulls from two queues
    concurrently. Uses a short timeout on each recv so the drain task can
    still surface a disconnect within a second.
    """

    async def _drain_client() -> None:
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            return
        except Exception:
            return

    reader = asyncio.create_task(_drain_client())
    try:
        while not reader.done():
            get_logs = asyncio.create_task(logs_queue.get())
            get_status = asyncio.create_task(status_queue.get())
            done, pending = await asyncio.wait(
                {get_logs, get_status},
                timeout=1.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()
            for d in done:
                try:
                    payload = d.result()
                except (asyncio.CancelledError, Exception):
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
