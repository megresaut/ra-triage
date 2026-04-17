#!/bin/bash
# Regenerate src/lib/types.ts from the running backend's OpenAPI schema.
#
# The backend must be running at http://127.0.0.1:5000 for this to succeed.
# This is the first real use of `openapi-typescript` as the
# backend-contract bridge (tech plan §13.4).
#
# `npm run dev` and `npm run build` also run this step via predev/prebuild —
# both variants swallow failure (|| true) so the dev loop keeps working when
# the backend is down. Use this script to force a regen and surface errors.

set -euo pipefail
cd "$(dirname "$0")/.."

npx openapi-typescript http://127.0.0.1:5000/openapi.json -o src/lib/types.ts
echo "Regenerated src/lib/types.ts"
