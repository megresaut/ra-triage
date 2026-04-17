# Agent 7 — Manual end-to-end runbook

Launch-for-Testing can't be meaningfully automated: it needs real RA AVM
clones on disk, a real Postgres dev DB, real `go` / `npm` toolchains, and a
real browser. This document is the checklist Megha runs before declaring
Agent 7 done on her machine.

## Prereqs (one-time)

1. Postgres 15+ running locally with dev DB at
   `postgres://megkrish@localhost:5432/ra_dev?sslmode=disable` (trust auth).
2. `pg_dump`, `pg_restore`, `dropdb`, `createdb`, `psql` on `$PATH`.
3. RA AVM clones in place:
   - `/Users/megkrish/Desktop/ra-avm/dev/avm-backend` (Go backend).
   - `/Users/megkrish/Desktop/ra-avm/dev/builder_frontend` (Vite frontend).
4. Both clones are on `main` and `git status` is clean.
5. Node 20+ and Go 1.22+ installed and reachable from a fresh shell.

## The test

1. Start the triage backend: `make dev` in `~/code/ra-triage`. Confirm
   `http://127.0.0.1:5000/health` returns `{"status":"ok"}`.
2. Start the triage frontend: `make frontend`. Confirm
   `http://127.0.0.1:1313` loads the queue view.
3. Enqueue a bug that reaches `READY_FOR_REVIEW` through the pipeline (or
   use a fixture bug set to `READY_FOR_REVIEW` via direct SQL for this
   runbook only).
4. Open the bug's detail view.
5. Click **Launch for Testing**. Observe:
   - Within ~2 seconds: status badge flips `none → starting`.
   - Backend log (via `/ws/test-instance`) shows "Dumping dev DB…" (first
     run only) then readiness polling.
   - Within ~10s for backend / ~30s for frontend: status flips to
     `running`.
   - Browser auto-opens `http://127.0.0.1:15173` (the builder-frontend test
     port).
   - The Test Instance Panel shows two tabs (`avm-backend`,
     `builder-frontend`) with live logs.
6. Exercise the fix in the browser — do whatever verifies it works.
7. Click **Stop test instance**. Observe:
   - Status flips `running → stopping → stopped` within 6 seconds.
   - `lsof -i :18080` and `lsof -i :15173` return nothing (ports freed).
   - Row in `test_instances` has `status='stopped'` and `stopped_at` set.

## Switching bugs

Expected behavior (no automation):

1. Launch bug A (steps 3–5 above). It becomes running.
2. Navigate to bug B's detail page. Click **Launch for Testing**.
3. Dialog: "This will stop the test instance for bug #A and launch bug #B."
4. Click **Stop & launch**. Observe:
   - Status goes `running (A) → stopping → stopped`.
   - New row inserts for B: `starting → running`.
   - Browser does not re-open unless a tab was closed (Chrome focuses the
     existing tab because the URL is identical).

## Crash path

1. Launch a bug. Identify the backend pid via the logs panel (or
   `GET /test-instances/current`).
2. `kill -9 <backend_pid>`. Observe:
   - Within ~2s: status flips `running → crashed`.
   - The WS panel shows "Crashed — see logs below."
   - `POST /test-instances/current` returns 204 (lock released).
   - Launch button is enabled again.

## `npm ci` cache

1. After the first successful launch, inspect
   `~/ra-triage/npm-cache/builder-frontend/<hash>` — the marker file exists.
2. Stop, then launch the same bug again. Observe:
   - Backend log no longer runs `npm ci` (you won't see its output).
   - Readiness still succeeds; frontend comes up quickly.
3. Touch `builder_frontend/package-lock.json` in the worktree so the hash
   changes, then launch again. Observe:
   - `npm ci` runs once. New marker replaces the old.

## Database safety

1. Connect to dev DB: `psql ra_dev`. Note some table count.
2. Launch a bug that modifies that table.
3. Exercise the modification in the browser.
4. Stop the instance. Reconnect to `ra_dev`: the count is unchanged.
5. Reconnect to `ra_triage_test`: the count reflects the agent's writes.
6. Relaunch: `ra_triage_test` is reset from the dev snapshot.
