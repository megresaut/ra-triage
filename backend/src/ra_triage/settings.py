"""Typed settings for RA Triage backend.

Values are sourced from the monorepo-root `.env` file (at
`~/code/ra-triage/.env`), which is also where the overmind Procfile sources
env from. `.env.example` at the same location documents every key.

Secrets default to empty strings so the backend can boot without Notion /
Slack configured (health endpoint still works, tests pass). Agents that need
those secrets assert them at call time, not at import time.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Backend lives at <repo_root>/backend/src/ra_triage/settings.py, so the repo
# root — and the .env we want to load — is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENV_FILE = _REPO_ROOT / ".env"


class Settings(BaseSettings):
    """Process-wide configuration for RA Triage."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Notion ---
    notion_token: str = Field(default="", description="Notion API token (SDK default env var)")
    notion_bugs_db_id: str = Field(default="", description="Notion DB ID for the bug tracker")

    # --- Slack ---
    ra_avm_slack_webhook: str = Field(
        default="", description="Incoming webhook URL for RA AVM Slack channel"
    )

    # --- Local filesystem ---
    triage_home: Path = Field(
        default=Path("~/ra-triage").expanduser(),
        description="Root for triage.db, worktrees/, logs/, snapshots/, prompts/",
    )

    # --- Triage tool itself ---
    triage_backend_port: int = Field(default=5000, description="Uvicorn bind port for this backend")
    triage_frontend_origin: str = Field(
        default="http://127.0.0.1:1313",
        description="Allowed CORS origin for the Vite dev server",
    )

    @property
    def triage_db_path(self) -> Path:
        """Absolute path to the SQLite DB file."""
        return Path(self.triage_home).expanduser() / "triage.db"

    @property
    def worktrees_dir(self) -> Path:
        return Path(self.triage_home).expanduser() / "worktrees"

    @property
    def logs_dir(self) -> Path:
        return Path(self.triage_home).expanduser() / "logs"

    @property
    def snapshots_dir(self) -> Path:
        return Path(self.triage_home).expanduser() / "snapshots"

    @property
    def prompts_dir(self) -> Path:
        return Path(self.triage_home).expanduser() / "prompts"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings instance.

    Using a cached accessor (rather than a module-level singleton) means tests
    can monkeypatch env vars before the first access without import-order
    headaches.
    """
    return Settings()
