"""Initial RA Triage v1 schema.

All seven tables from technical plan §3 in a single migration:
bugs, agent_runs, escalations, rejections, pull_requests, test_instances,
rebase_attempts.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-04-17
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- bugs ---
    op.create_table(
        "bugs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("notion_page_id", sa.Text(), nullable=False, unique=True),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("repro_steps", sa.Text(), nullable=True),
        sa.Column("severity", sa.Text(), nullable=True),
        sa.Column("reporter", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("affected_repos", sa.Text(), nullable=True),
        sa.Column(
            "deny_list_hit",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "rejection_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("terminated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("idx_bugs_state", "bugs", ["state"])
    op.create_index("idx_bugs_project", "bugs", ["project_id"])

    # --- agent_runs ---
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "bug_id",
            sa.Integer(),
            sa.ForeignKey("bugs.id"),
            nullable=False,
        ),
        sa.Column("agent_role", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("completion_token", sa.Text(), nullable=True),
        sa.Column("log_path", sa.Text(), nullable=False),
        sa.Column("prompt_path", sa.Text(), nullable=False),
        sa.Column("worktree_repo", sa.Text(), nullable=True),
        sa.Column("triage_md_snapshot", sa.Text(), nullable=True),
    )
    op.create_index("idx_agent_runs_bug", "agent_runs", ["bug_id"])

    # --- escalations ---
    op.create_table(
        "escalations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "bug_id",
            sa.Integer(),
            sa.ForeignKey("bugs.id"),
            nullable=False,
        ),
        sa.Column("triggered_rule", sa.Text(), nullable=True),
        sa.Column("brief_markdown", sa.Text(), nullable=False),
        sa.Column("options", sa.Text(), nullable=False),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("resolved_option", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )

    # --- rejections ---
    op.create_table(
        "rejections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "bug_id",
            sa.Integer(),
            sa.ForeignKey("bugs.id"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # --- pull_requests ---
    op.create_table(
        "pull_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "bug_id",
            sa.Integer(),
            sa.ForeignKey("bugs.id"),
            nullable=False,
        ),
        sa.Column("repo_name", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("merged_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # --- test_instances ---
    op.create_table(
        "test_instances",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "bug_id",
            sa.Integer(),
            sa.ForeignKey("bugs.id"),
            nullable=False,
        ),
        sa.Column("pids", sa.Text(), nullable=False),
        sa.Column("ports", sa.Text(), nullable=False),
        sa.Column("db_snapshot_hash", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "launched_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("stopped_at", sa.DateTime(), nullable=True),
    )

    # --- rebase_attempts ---
    op.create_table(
        "rebase_attempts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "bug_id",
            sa.Integer(),
            sa.ForeignKey("bugs.id"),
            nullable=False,
        ),
        sa.Column("repo_name", sa.Text(), nullable=False),
        sa.Column(
            "trigger_bug_id",
            sa.Integer(),
            sa.ForeignKey("bugs.id"),
            nullable=False,
        ),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("conflict_files", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    op.drop_table("rebase_attempts")
    op.drop_table("test_instances")
    op.drop_table("pull_requests")
    op.drop_table("rejections")
    op.drop_table("escalations")
    op.drop_index("idx_agent_runs_bug", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("idx_bugs_project", table_name="bugs")
    op.drop_index("idx_bugs_state", table_name="bugs")
    op.drop_table("bugs")
