"""Hardcoded project registry.

Adding a new client project = adding an entry to `PROJECTS`. Fields match the
tech plan §8 layout with the Agent-1 approved deviations applied:

- RA AVM is Go (backend) + Vite/React (frontend), not Python.
- Bug database IDs come from `NOTION_BUGS_DB_ID` (single shared env var for
  the only currently-active project), not a per-project env var.
- Health URLs are None — AVM has no /health endpoint; Agent 5's Integration
  Tester will fall back to TCP-open + <500 HTTP GET.
- GitHub origins are included on RepoConfig so Agent 8's gh CLI work can use
  them directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DenyList:
    """Paths and categories whose modification requires human approval."""

    paths: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RepoConfig:
    """One repository that belongs to a project."""

    name: str
    path: Path
    main_branch: str
    branch_prefix: str
    dev_command: str
    build_command: str
    test_command: str
    dev_port: int
    test_launch_port: int
    integration_port: int
    env_file: str
    role: str  # 'backend' | 'frontend'
    github_origin: str  # e.g. "github.com/megresaut/avm-backend"
    database_url: str | None = None
    test_database_url: str | None = None
    # AVM has no /health; Integration Tester falls back to TCP-open + <500 HTTP GET —
    # Agent 5 fills this in.
    health_url: str | None = None


@dataclass(frozen=True)
class ProjectConfig:
    """One client project in the registry."""

    id: str
    display_name: str
    repos: list[RepoConfig]
    deny_list: DenyList
    slack_webhook_env: str  # name of the env var, not its value
    auto_merge: bool = False

    @property
    def notion_db_id(self) -> str:
        """Resolve the Notion bugs DB ID from the env at call time.

        Deferred (not computed at import) so tests and the /health endpoint
        can run without Notion configured. Agents needing this value should
        surface a clear error via `require_notion_db_id()`.
        """
        return os.environ.get("NOTION_BUGS_DB_ID", "")

    def require_notion_db_id(self) -> str:
        value = self.notion_db_id
        if not value:
            raise RuntimeError(
                "NOTION_BUGS_DB_ID is not set; cannot interact with the Notion bug tracker."
            )
        return value

    def require_slack_webhook(self) -> str:
        value = os.environ.get(self.slack_webhook_env, "")
        if not value:
            raise RuntimeError(
                f"{self.slack_webhook_env} is not set; cannot post Slack notifications."
            )
        return value


_RA_AVM_DENY_LIST = DenyList(
    paths=[
        "**/auth/**",
        "**/security/**",
        "**/payments/**",
        "**/migrations/**",
        "**/.env*",
        "**/schema.sql",
    ],
    categories=["schema_change", "migration", "auth", "payments"],
)


PROJECTS: dict[str, ProjectConfig] = {
    "ra_avm": ProjectConfig(
        id="ra_avm",
        display_name="RA AVM",
        slack_webhook_env="RA_AVM_SLACK_WEBHOOK",
        auto_merge=False,
        deny_list=_RA_AVM_DENY_LIST,
        repos=[
            RepoConfig(
                name="avm-backend",
                path=Path("/Users/megkrish/Desktop/ra-avm/dev/avm-backend"),
                main_branch="main",
                branch_prefix="triage/",
                # AVM backend is Go, served from the ./api subdirectory.
                dev_command="cd api && PORT={port} go run ./cmd/server",
                build_command="cd api && go build ./... && go vet ./...",
                test_command="cd api && go test ./...",
                dev_port=8080,
                test_launch_port=18080,
                integration_port=28080,
                env_file=".env",
                database_url="postgres://megkrish@localhost:5432/ra_dev?sslmode=disable",
                test_database_url=(
                    "postgres://megkrish@localhost:5432/ra_triage_test?sslmode=disable"
                ),
                role="backend",
                health_url=None,
                github_origin="github.com/megresaut/avm-backend",
            ),
            RepoConfig(
                name="builder-frontend",
                path=Path("/Users/megkrish/Desktop/ra-avm/dev/builder_frontend"),
                main_branch="main",
                branch_prefix="triage/",
                dev_command="npm run dev -- --port {port} --host 127.0.0.1",
                build_command="npx tsc --noEmit",
                test_command="npm test",
                dev_port=5173,
                test_launch_port=15173,
                integration_port=25173,
                env_file=".env.local",
                role="frontend",
                health_url=None,
                github_origin="github.com/megresaut/builder_frontend",
            ),
        ],
    ),
}


def get_project(project_id: str) -> ProjectConfig:
    """Look up a project by id. Raises `KeyError` if unknown."""
    return PROJECTS[project_id]
