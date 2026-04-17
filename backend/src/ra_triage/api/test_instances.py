"""REST endpoints for Launch-for-Testing.

Surface:

    POST /test-instances/launch   body: {bug_id: int}  → {status:"running", ...}
    POST /test-instances/stop                         → {status:"stopped" | "none"}
    GET  /test-instances/current                      → snapshot or 204

Error handling: launch failures (readiness timeout, snapshot failure,
missing worktree, lock contention) return ``409 Conflict`` with a
structured body ``{"detail": "...", "kind": "launch_failed"}``. Everything
else surfaces as ``500``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from ra_triage.test_runner.instance import (
    LaunchFailed,
    get_test_instance_manager,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/test-instances", tags=["test-instances"])


class LaunchRequest(BaseModel):
    """Body for POST /test-instances/launch."""

    bug_id: int


class LaunchResponse(BaseModel):
    """Serializable view of a running instance (matches manager.current())."""

    bug_id: int
    status: str
    ports: dict[str, int]
    pids: dict[str, int]
    db_snapshot_hash: str | None = None
    launched_at: str
    browser_port: int | None = None


class StopResponse(BaseModel):
    """Body for POST /test-instances/stop."""

    status: str  # "stopped" | "none"


@router.post(
    "/launch",
    response_model=LaunchResponse,
    responses={
        409: {
            "description": "Launch failed — readiness timeout, snapshot failure, "
            "or a partial-spawn that was cleaned up.",
        }
    },
)
async def launch_test_instance(body: LaunchRequest) -> LaunchResponse:
    """Launch a test instance for ``body.bug_id``.

    If a different bug's instance is currently running, it is stopped first
    before the new one is launched — the lock inside
    :class:`TestInstanceManager` serializes everything.

    Returns the running snapshot. On failure returns 409 with a structured
    detail body.
    """
    manager = get_test_instance_manager()
    try:
        instance = await manager.launch(body.bug_id)
    except LaunchFailed as exc:
        logger.warning("launch failed for bug %d: %s", body.bug_id, exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "kind": "launch_failed",
                "bug_id": body.bug_id,
                "message": str(exc),
            },
        ) from exc

    return LaunchResponse(
        bug_id=instance.bug_id,
        status="running",
        ports={name: sp.port for name, sp in instance.procs.items()},
        pids={name: sp.pid for name, sp in instance.procs.items()},
        db_snapshot_hash=instance.snapshot_hash or None,
        launched_at=instance.launched_at.isoformat(),
        browser_port=instance.browser_port,
    )


@router.post("/stop", response_model=StopResponse)
async def stop_test_instance() -> StopResponse:
    """Stop the current test instance. Returns ``status="none"`` if nothing was running."""
    manager = get_test_instance_manager()
    was_running = (await manager.current()) is not None
    await manager.stop()
    return StopResponse(status="stopped" if was_running else "none")


@router.get(
    "/current",
    responses={
        200: {"description": "Running instance snapshot."},
        204: {"description": "No instance currently running."},
    },
)
async def get_current_test_instance() -> Any:
    """Return the current running instance or ``204 No Content`` if none."""
    manager = get_test_instance_manager()
    snapshot = await manager.current()
    if snapshot is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return JSONResponse(snapshot)


__all__ = ["router"]
