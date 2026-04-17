"""Pipeline resume skeleton — logs-only for Agent 5.

Agent 12 implements the real re-spawn logic (tech plan §16): on backend
restart, walk non-terminal bugs and either resume the pipeline by
dispatching the next agent, or mark them NEEDS_MANUAL_REVIEW if the
state is indeterminate.

THIS scaffold only INSPECTS and LOGS. It does not re-spawn any
pipelines. That keeps Agent 5's surface minimal while still giving
operators visible evidence on startup that non-terminal bugs exist.

Wired into ``main._lifespan`` so the log lines appear at server boot.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from ra_triage.db import engine as engine_module
from ra_triage.db import repo as repo_module

logger = logging.getLogger(__name__)


async def resume_inflight(
    session_factory: Callable[[], Session] | None = None,
) -> list[int]:
    """Log non-terminal bugs + their most recent agent_run.

    Args:
      session_factory: optional callable that returns a new ``Session``.
        Defaults to ``engine_module.SessionLocal``. Provided so tests can
        inject a rebound sessionmaker pointed at a fixture DB.

    Returns:
      The list of non-terminal bug IDs observed (in the same order
      ``repo.list_bugs`` returned them — newest first).

    Does NOT re-spawn pipelines. Agent 12 will replace this body.
    """
    factory = session_factory or engine_module.SessionLocal
    observed: list[int] = []

    session = factory()
    try:
        bugs = repo_module.list_bugs(session)
        for bug in bugs:
            runs = repo_module.list_agent_runs(session, bug.id)
            last = runs[-1] if runs else None
            last_info: dict[str, Any] = (
                {
                    "agent_role": last.agent_role,
                    "attempt": last.attempt,
                    "completion_token": last.completion_token,
                    "exit_code": last.exit_code,
                }
                if last is not None
                else {}
            )
            logger.info(
                "resume_inflight: bug_id=%d state=%s last_run=%s "
                "(Agent 12 TODO: real re-spawn)",
                bug.id,
                bug.state,
                last_info or "none",
            )
            observed.append(bug.id)
    finally:
        session.close()

    if observed:
        logger.info(
            "resume_inflight: %d non-terminal bug(s) observed — no re-spawn (Agent 12 scope)",
            len(observed),
        )
    else:
        logger.info("resume_inflight: no non-terminal bugs to inspect")
    return observed


__all__ = ["resume_inflight"]
