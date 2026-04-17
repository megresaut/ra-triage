# Contributing to RA Triage

Short guide for humans and for the build-agent pipeline that assembles this
tool.

## Branches

- `main` is the integration branch. Never push directly — open a PR.
- Build agents use `build/agent-<n>-<short-slug>`, e.g.
  `build/agent-1-scaffold`, `build/agent-5-investigator`.
- Regular feature work uses `feat/<slug>`; bugfixes `fix/<slug>`; chores
  `chore/<slug>`.

## Commits

Conventional Commits. Common types:

- `feat:` a new user-facing capability
- `fix:` a bug fix
- `chore:` tooling, configs, build infra
- `refactor:` behaviour-preserving code changes
- `test:` test-only additions
- `docs:` docs-only additions

One logical change per commit; ~5–12 commits per agent PR is typical.

## Workflow

```bash
# First time
make install
cp .env.example .env       # fill in placeholders
make db-init

# Everyday loop
make dev                   # overmind: backend :5000 + frontend :1313
make test                  # backend pytest
make lint                  # backend ruff+mypy; frontend eslint+tsc --noEmit
```

## Where settings live

The backend and the frontend both read config from the **root-level
`.env`** at `~/code/ra-triage/.env`. There is no per-subproject `.env`.

- The backend's `pydantic-settings` is configured in
  `backend/src/ra_triage/settings.py` to point at this file explicitly.
- `./dev.sh` sources the same file before launching overmind so both
  children see the same env.

Secrets never get committed. `.env.example` documents every key with
placeholder values.

## Type safety

- Backend: `mypy` must pass in strict-ish mode (see `pyproject.toml`). New
  code should annotate public functions; use `Any` only with a comment.
- Frontend: `tsc --noEmit` must pass in `strict` mode. The
  `openapi-typescript` codegen pulls real types from the running backend at
  `http://127.0.0.1:5000/openapi.json` via the `predev` and `prebuild`
  scripts; the generation step falls through non-fatally if the backend is
  not running (Vite still starts, types remain stale).

To regenerate types explicitly:

```bash
cd frontend && npm run gen:types
```

## Testing

Backend: pytest is required for new Python code. Minimum bar is an
import-level smoke test plus one meaningful test per public function.

Frontend: v1 has no Jest/Vitest setup — typecheck + lint + manual smoke
against the running backend are the bar. Later agents may add Vitest when
they add something complex enough to need it.

## Local bind

All listening sockets are bound to `127.0.0.1`. Never bind `0.0.0.0`; never
open a public port; never use a CORS wildcard.
