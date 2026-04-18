"""Add bugs.needs_manual_review column for resumability inconsistency surface.

Agent 12 (Resumability + Retry/Cancel Polish) introduces the concept of
a bug being resumed on backend startup into an *inconsistent* state — for
example, a bug whose ``state == READY_FOR_REVIEW`` but whose ``agent_runs``
table has no Tester row. Such bugs are marked ``needs_manual_review=True``
so the frontend can surface a yellow banner pointing the operator at
TRIAGE.md, and so :func:`orchestrator.resume.resume_inflight` can avoid
auto-restarting their pipelines.

Why a column (not a sub-state of BLOCKED): the bug may legitimately be
in any non-terminal state. Forcing a transition to BLOCKED on resume
would lose information ("we didn't know what to do"); a side-channel
boolean flag preserves the original state while still showing the
operator that human attention is required.

Schema change:
  - ``bugs.needs_manual_review BOOLEAN NOT NULL DEFAULT 0``

The flag is cleared by ``POST /bugs/:id/clear-manual-review-flag`` (the
yellow banner's dismiss button) once the operator has acknowledged the
inconsistency. Cleared flag is durable — Agent 12's resume only sets it,
never clears it on subsequent boots.

Revision ID: 0003_bug_needs_manual_review
Revises: 0002_bug_conflict_files
Create Date: 2026-04-17
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_bug_needs_manual_review"
down_revision: Union[str, None] = "0002_bug_conflict_files"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite needs a constant default for ADD COLUMN with NOT NULL. ``0``
    # is the SQLite-side representation of ``False``, matching the ORM's
    # default. ``server_default`` is set so existing rows backfill cleanly
    # to ``0`` (False).
    with op.batch_alter_table("bugs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "needs_manual_review",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("bugs") as batch_op:
        batch_op.drop_column("needs_manual_review")
