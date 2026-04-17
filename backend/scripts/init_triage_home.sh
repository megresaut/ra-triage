#!/usr/bin/env bash
# Ensure the $TRIAGE_HOME directory tree exists before Alembic runs.
# Called by `make db-init`.

set -euo pipefail

TRIAGE_HOME="${TRIAGE_HOME:-$HOME/ra-triage}"

mkdir -p "$TRIAGE_HOME"
mkdir -p "$TRIAGE_HOME/worktrees"
mkdir -p "$TRIAGE_HOME/logs"
mkdir -p "$TRIAGE_HOME/snapshots"
mkdir -p "$TRIAGE_HOME/prompts"

echo "triage_home: $TRIAGE_HOME"
