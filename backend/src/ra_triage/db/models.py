"""SQLAlchemy ORM models for RA Triage.

Mirrors the Alembic initial schema at
`backend/alembic/versions/0001_initial_schema.py` verbatim: column names,
types, nullability, defaults, and indices. If the two ever drift, this file
is wrong — the migration is authoritative for the DB, these models are
authoritative for the Python API surface.

Uses SQLAlchemy 2.x typed `Mapped[...]` / `mapped_column(...)` style.

Module-level note on FK declaration site: `rebase_attempts` has two FKs to
`bugs.id` (the affected bug and the trigger bug). Both are declared here; the
migration declares them the same way.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all RA Triage ORM models."""


class Bug(Base):
    """One row per Notion bug we've ingested."""

    __tablename__ = "bugs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notion_page_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    project_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    repro_steps: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str | None] = mapped_column(Text, nullable=True)
    reporter: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    affected_repos: Mapped[str | None] = mapped_column(Text, nullable=True)
    deny_list_hit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    rejection_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    agent_runs: Mapped[list[AgentRun]] = relationship(
        back_populates="bug", cascade="all, delete-orphan", lazy="selectin"
    )
    rejections: Mapped[list[Rejection]] = relationship(
        back_populates="bug", cascade="all, delete-orphan", lazy="selectin"
    )
    pull_requests: Mapped[list[PullRequest]] = relationship(
        back_populates="bug", cascade="all, delete-orphan", lazy="selectin"
    )
    escalations: Mapped[list[Escalation]] = relationship(
        back_populates="bug", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        Index("idx_bugs_state", "state"),
        Index("idx_bugs_project", "project_id"),
    )


class AgentRun(Base):
    """One row per agent subprocess invocation (including retries)."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bug_id: Mapped[int] = mapped_column(Integer, ForeignKey("bugs.id"), nullable=False)
    agent_role: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_path: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_path: Mapped[str] = mapped_column(Text, nullable=False)
    worktree_repo: Mapped[str | None] = mapped_column(Text, nullable=True)
    triage_md_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)

    bug: Mapped[Bug] = relationship(back_populates="agent_runs")

    __table_args__ = (Index("idx_agent_runs_bug", "bug_id"),)


class Escalation(Base):
    """Structured Escalation Brief per bug (one row per escalation event)."""

    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bug_id: Mapped[int] = mapped_column(Integer, ForeignKey("bugs.id"), nullable=False)
    triggered_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    brief_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_option: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    bug: Mapped[Bug] = relationship(back_populates="escalations")


class Rejection(Base):
    """One row per 'Reject' click in the frontend review UI."""

    __tablename__ = "rejections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bug_id: Mapped[int] = mapped_column(Integer, ForeignKey("bugs.id"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    bug: Mapped[Bug] = relationship(back_populates="rejections")


class PullRequest(Base):
    """One PR per bug per affected repo."""

    __tablename__ = "pull_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bug_id: Mapped[int] = mapped_column(Integer, ForeignKey("bugs.id"), nullable=False)
    repo_name: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str] = mapped_column(Text, nullable=False)
    pr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    merged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    bug: Mapped[Bug] = relationship(back_populates="pull_requests")


class TestInstance(Base):
    """One row per 'Launch for Testing' click."""

    __tablename__ = "test_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bug_id: Mapped[int] = mapped_column(Integer, ForeignKey("bugs.id"), nullable=False)
    pids: Mapped[str] = mapped_column(Text, nullable=False)
    ports: Mapped[str] = mapped_column(Text, nullable=False)
    db_snapshot_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    launched_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RebaseAttempt(Base):
    """Audit row per rebase attempted after a peer bug's PR merge."""

    __tablename__ = "rebase_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bug_id: Mapped[int] = mapped_column(Integer, ForeignKey("bugs.id"), nullable=False)
    repo_name: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_bug_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bugs.id"), nullable=False
    )
    result: Mapped[str] = mapped_column(Text, nullable=False)
    conflict_files: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
