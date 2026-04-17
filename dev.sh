#!/usr/bin/env bash
# Start backend (127.0.0.1:5000) and frontend (127.0.0.1:1313) together via
# overmind, sourcing env from the repo-root .env if present.

set -euo pipefail

cd "$(dirname "$0")"

if [[ -f .env ]]; then
    # Export every key in .env so both children see them.
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

exec overmind start -f Procfile
