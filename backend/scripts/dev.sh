#!/usr/bin/env bash
# Launch the RA Triage backend bound to 127.0.0.1 on port ${TRIAGE_BACKEND_PORT:-5000}.
# Invoked by `make dev` at the backend level and (via `make backend`) at the repo root.

set -euo pipefail
cd "$(dirname "$0")/.."

exec uv run uvicorn ra_triage.main:app \
    --port "${TRIAGE_BACKEND_PORT:-5000}" \
    --host 127.0.0.1 \
    --reload
