# RA Triage

**Our own AI on-call engineer.**

When a client submits a bug report, RA Triage takes it from there:
- it reads and documents the report,
- checks the logs,
- tracks down the root cause,
- fixes the bug in an isolated worktree,
- tests the fix.

When the fix is ready, a developer gets a running copy to check. One click approves it, and RA Triage merges and redeploys it. If a bug is too risky or too hard to fix on its own, it doesn't guess. It writes up what it found and alerts the developer.

The first project it covers is **RA AVM** (`avm-backend` + `builder_frontend`). Adding another client project takes one config entry.

---

## Why it matters

Bug fixing is the part of client work that doesn't scale. Every report pulls a developer off whatever they're building. They have to:
1. reproduce the bug,
2. dig through logs and code,
3. write a fix and a test,
4. ship it.

Most of those steps are routine.

RA Triage does the routine part, around the clock and for every client at once. By the time a developer looks at a bug, the investigation is done, the fix is written, and the tests pass. What's left is a few minutes of checking and one click. The developer stays in charge of anything that ships, but stops spending hours on every report.

## What it automates

### 1. Picks up and documents every report
- Watches the client's bug tracker in Notion and picks up new reports within minutes. Urgent bugs can be queued by hand.
- Turns each report into a structured record: title, description, repro steps, severity, reporter, screenshots, and affected user.
- Deduplicates reports and keeps the client-facing status current (**Open → Triaged → In progress → Fixed**). Clients always see where their bug stands.

### 2. Investigates and finds the root cause
- An **Investigator** agent checks the logs and reads the code to find the actual root cause, not just the symptom.
- It works out how much of the system the fix touches: frontend, backend, or both.
- Its findings go into a `TRIAGE.md` document that travels with the bug. Every step is written down.

### 3. Assesses difficulty and escalates when it should
RA Triage knows what it shouldn't fix on its own. These bugs are escalated instead of patched automatically:
- changes to auth, payments, database migrations, schema, or production config,
- bugs with unclear scope,
- bugs that need a judgment call.

The developer gets a **Slack alert** and an **Escalation Brief** they can act on in under two minutes:
- a one-sentence summary,
- why it was escalated,
- the root cause,
- a recommended fix and ranked alternatives, with effort for each,
- the files that would change,
- a test plan.

The developer can **approve the recommended fix** with one click, choose an alternative, or take over.

### 4. Fixes it in its own worktree
- Each bug gets its own git worktree and branch (`triage/<bug_id>`), so several bugs can be in progress at once without interfering with each other or with the developer's own work.
- **Single-repo bugs:** a **Fixer** agent writes the fix.
- **Bugs that span frontend and backend:** a **Fix Designer** first writes a contract for how the two sides fit together. Then two Fixers build each side **in parallel**.

### 5. Tests it
- A **Tester** agent runs the existing test suite and adds a regression test for the bug. It can't change production code to make tests pass.
- For cross-repo fixes, an **Integration Tester** starts both services and checks the fix end to end.

### 6. Opens a port for the developer to check the work
Click **Launch for Testing** and RA Triage starts the fixed version of the app on its own ports, with a fresh copy of the dev database, and opens it in the browser. Server logs stream live in the app. The developer clicks through the bug themselves before anything ships.

### 7. One-click approval, merge, and redeploy
- **Approve:** RA Triage opens the pull request(s), merges them with the backend first, and redeploys. It then marks the bug **Fixed** in Notion with a link to the PR. Other in-progress fixes are automatically rebased onto the new code.
- **Reject:** the developer writes one line about what's still wrong, and the agents try again with that feedback. After three rejections the bug is handed to the developer.

## Built to be trusted
- **The developer approves everything that ships.** Agents never merge without that approval.
- **Isolation.** Agents work in separate worktrees with a disposable database copy. Neither the developer's working copy nor real data is touched.
- **Fully documented.** Every bug carries its investigation, fix notes, and test results. All of it is included in the pull request.
- **Resilient.** In-progress work survives restarts and crashes and picks up where it left off.
- **Configurable.** Auto-merge can be switched off so fixes stop at an open PR.

## Tech

- **Backend:** Python, FastAPI, SQLite (Alembic), and APScheduler. A Python orchestrator runs each agent as a Claude Code session.
- **Frontend:** React 18, TypeScript, Vite, Tailwind, shadcn/ui.
- **Integrations:** Notion (bug intake and status), GitHub via the `gh` CLI (pull requests), Slack (alerts).
- Runs locally, with state in `~/ra-triage/` (database, worktrees, logs, DB snapshots).

---

## Setup

```bash
make install            # backend (uv) + frontend (npm) dependencies
cp .env.example .env    # Notion, Slack, GitHub settings
make db-init            # creates ~/ra-triage/ and the SQLite DB
make dev                # backend :5000 + frontend :1313
```

Open http://127.0.0.1:1313. `make test` and `make lint` run the checks.
