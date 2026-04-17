# RA Triage

Internal multi-agent local ops tool that resolves RA client bugs end-to-end:
polls Notion, runs a coordinated Investigator/Fixer/Tester pipeline against
client repos under `~/ra-triage/worktrees/`, and surfaces completed fixes
for human review before merging PRs via `gh`.

**v1 scope:** single client project (RA AVM). Runs entirely on the local
machine. No Docker, no cloud. See `~/Desktop/ra-triage/` for the full
design docs.

## Layout

```
backend/    FastAPI app, SQLite via Alembic, APScheduler, agents, workspaces
frontend/   Vite + React 18 + TypeScript + Tailwind + shadcn/ui
Makefile    Root dispatcher
Procfile    overmind entries for combined dev
dev.sh      `overmind start -f Procfile`
```

Persistent state lives outside the repo at `~/ra-triage/` (triage.db,
worktrees/, logs/, snapshots/, prompts/).

## Ports

| Surface                | Host       | Port   |
|------------------------|------------|--------|
| Triage backend         | 127.0.0.1  | 5000   |
| Triage frontend (Vite) | 127.0.0.1  | 1313   |

AVM's own dev ports (8080 backend, 5173 frontend) and test-launch ports
(18080/15173) are orthogonal — Triage spawns them on demand.

## First-run

```bash
# 1. Install deps (backend uv sync + frontend npm install).
make install

# 2. Copy env template and fill in real values.
cp .env.example .env
$EDITOR .env

# 3. Create ~/ra-triage/ + triage.db.
make db-init

# 4. Start both services under overmind (backend :5000, frontend :1313).
make dev
```

Then open <http://127.0.0.1:1313/> in a browser. The scaffold's `/` route
shows a backend connectivity indicator.

## Useful targets

| Command         | What                                               |
|-----------------|----------------------------------------------------|
| `make install`  | Install backend + frontend deps                    |
| `make db-init`  | Create `~/ra-triage/` tree and run Alembic upgrade |
| `make backend`  | Run the backend only                               |
| `make frontend` | Run the frontend only                              |
| `make dev`      | Run both via overmind                              |
| `make test`     | Backend pytest                                     |
| `make lint`     | Backend `ruff + mypy`, frontend `eslint + tsc`     |
| `make clean`    | Wipe `~/ra-triage/` state (prompts Y/N)            |

## Plan docs

- [`~/Desktop/ra-triage/ra-triage-product-plan.md`](../Desktop/ra-triage/ra-triage-product-plan.md)
  — source of truth for *what*.
- [`~/Desktop/ra-triage/ra-triage-technical-plan.md`](../Desktop/ra-triage/ra-triage-technical-plan.md)
  — source of truth for *how*.
- [`~/Desktop/ra-triage/ra-triage-orchestrator-instructions.md`](../Desktop/ra-triage/ra-triage-orchestrator-instructions.md)
  — build-orchestration playbook.
