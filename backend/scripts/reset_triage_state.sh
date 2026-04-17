#!/usr/bin/env bash
# Wipe local RA Triage state — triage.db, worktrees/, logs/, prompts/.
# The snapshots/ directory is preserved (it's expensive to rebuild and safe to keep).
# Use `--yes` for unattended calls.

set -euo pipefail

TRIAGE_HOME="${TRIAGE_HOME:-$HOME/ra-triage}"

confirm="no"
if [[ "${1:-}" == "--yes" ]]; then
    confirm="yes"
fi

if [[ "$confirm" != "yes" ]]; then
    echo "This will delete:"
    echo "  $TRIAGE_HOME/triage.db"
    echo "  $TRIAGE_HOME/worktrees/"
    echo "  $TRIAGE_HOME/logs/"
    echo "  $TRIAGE_HOME/prompts/"
    echo "Snapshots will be kept."
    read -r -p "Proceed? [y/N] " response
    case "$response" in
        [yY]|[yY][eE][sS]) ;;
        *) echo "Aborted."; exit 1 ;;
    esac
fi

rm -f "$TRIAGE_HOME/triage.db"
rm -rf "$TRIAGE_HOME/worktrees"
rm -rf "$TRIAGE_HOME/logs"
rm -rf "$TRIAGE_HOME/prompts"

echo "Triage state reset. Run 'make db-init' to re-create the DB."
