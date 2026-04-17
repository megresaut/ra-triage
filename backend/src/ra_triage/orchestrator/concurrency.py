"""Pipeline concurrency controls.

One asyncio.Semaphore caps the number of bugs whose pipelines can run
concurrently. Tech plan §6.1 pins this at 3 (product plan §Constraints
corroborates). It is declared module-level so every call site acquires
the same semaphore — tests can still override via monkeypatch on the
module attribute.

Future: a second "migration lane" semaphore of 1 per tech plan §6.1
comment. v1 always escalates migrations, so that lane is not needed yet;
when it is, it lands here.
"""

from __future__ import annotations

import asyncio

#: Maximum number of bug pipelines that may run concurrently.
PIPELINE_CONCURRENCY_LIMIT: int = 3

#: Single process-wide semaphore — every ``run_pipeline`` call acquires
#: this before doing any work. Tests can monkeypatch
#: ``concurrency.PIPELINE_SEMAPHORE`` to a wider/narrower semaphore.
PIPELINE_SEMAPHORE: asyncio.Semaphore = asyncio.Semaphore(
    PIPELINE_CONCURRENCY_LIMIT
)


__all__ = ["PIPELINE_CONCURRENCY_LIMIT", "PIPELINE_SEMAPHORE"]
