"""FastAPI app factory and lifespan hooks for RA Triage backend.

v1 scaffold: minimal app exposing only a health endpoint. Later agents mount
bug/project/test-instance routers and WebSocket channels on this app.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ra_triage.settings import get_settings


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    settings = get_settings()
    app = FastAPI(
        title="RA Triage",
        version="0.1.0",
        description="RA Triage backend — multi-agent local bug-fix ops tool.",
    )

    # Localhost-only CORS — the Vite dev server at 127.0.0.1:1313 calls us from
    # the browser, so we must allow that specific origin. No wildcards.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.triage_frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        """Liveness probe for Triage itself (not the AVM app)."""
        return {"status": "ok"}

    return app


app = create_app()
