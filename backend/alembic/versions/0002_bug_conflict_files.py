"""Add bugs.conflict_files column for rebase-sweeper conflict surface.

Agent 11 (Rebase-on-Merge Sweeper) surfaces rebase-conflict state on each
in-flight bug via a ``conflict_files`` JSON-encoded text column on
``bugs``. The sweeper sets this column when ``git rebase origin/<main>``
fails in an in-flight bug's worktree; ``BugResponse`` reads it back out
for the frontend's conflict indicator + panel.

Why a column (not an entirely separate table): the data is one-to-one
with the bug's current in-flight state and the frontend already fetches
``BugResponse`` heavily. Adding a JSON-text column keeps the hot path
a single query.

Schema change:
  - ``bugs.conflict_files TEXT NULL`` — JSON-encoded list[str] of file
    paths that collided during the most recent rebase attempt. NULL
    means "no outstanding conflict" (the normal steady state).

The historical ``rebase_attempts`` table still records every individual
attempt with its own ``conflict_files`` column for audit purposes. The
bug-level column is a redundant denorm for UI reads.

Revision ID: 0002_bug_conflict_files
Revises: 0001_initial_schema
Create Date: 2026-04-17
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_bug_conflict_files"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite can't ADD COLUMN with a non-constant default inside a regular
    # ALTER TABLE — so we add it nullable with no default. Existing rows
    # get NULL, which the Python side treats as "no conflict". New rows
    # are inserted by the ORM with ``conflict_files=None`` by default.
    with op.batch_alter_table("bugs") as batch_op:
        batch_op.add_column(
            sa.Column("conflict_files", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("bugs") as batch_op:
        batch_op.drop_column("conflict_files")
