"""Smoke tests for the Agent 10 prompt templates.

Renders each new prompt template with a minimal context dict and asserts
the key section names and completion tokens appear in the output. This
catches accidental template edits that would silently drop the required
sections (since the orchestrator routes on section presence + completion
token).
"""

from __future__ import annotations

from ra_triage.agents.base import _jinja_env


def _render(template: str, context: dict[str, object]) -> str:
    return _jinja_env.get_template(template).render(**context)


# --- fix_designer.md.j2 ---------------------------------------------------


def test_fix_designer_template_renders_minimal_context() -> None:
    """Fix Designer template: Cross-Repo Contract + completion token."""
    out = _render(
        "fix_designer.md.j2",
        {
            "bug": {"id": 42, "title": "broken shape"},
            "triage_md_path": "/tmp/TRIAGE.md",
            "repos": [
                {
                    "name": "avm-backend",
                    "role": "backend",
                    "worktree_path": "/tmp/wt/42/avm-backend",
                },
                {
                    "name": "builder-frontend",
                    "role": "frontend",
                    "worktree_path": "/tmp/wt/42/builder-frontend",
                },
            ],
            "designer_concerns": None,
        },
    )
    assert "## Cross-Repo Contract" in out
    assert "### Shared Data Shape" in out
    assert "### Backend Responsibilities" in out
    assert "### Frontend Responsibilities" in out
    assert "### Integration Test Scenario" in out
    assert "FIX_DESIGNER_COMPLETE" in out
    assert "FIX_DESIGNER_FAILED" in out
    # No designer_concerns block on first cycle.
    assert "PRIOR FIXER CONTRACT CONCERNS" not in out


def test_fix_designer_template_renders_designer_concerns_block() -> None:
    """When designer_concerns is supplied, the concerns block renders."""
    out = _render(
        "fix_designer.md.j2",
        {
            "bug": {"id": 42, "title": "broken shape"},
            "triage_md_path": "/tmp/TRIAGE.md",
            "repos": [
                {
                    "name": "avm-backend",
                    "role": "backend",
                    "worktree_path": "/tmp/wt/42/avm-backend",
                },
            ],
            "designer_concerns": "Frontend Fixer: email nullability mismatch",
        },
    )
    assert "PRIOR FIXER CONTRACT CONCERNS" in out
    assert "email nullability mismatch" in out


# --- fixer_frontend.md.j2 / fixer_backend.md.j2 ---------------------------


def test_fixer_frontend_template_renders_minimal_context() -> None:
    """Fixer-Frontend template: side-specific sections + completion tokens."""
    out = _render(
        "fixer_frontend.md.j2",
        {
            "bug": {"id": 42, "title": "broken shape"},
            "side": "Frontend",
            "worktree_path": "/tmp/wt/42/builder-frontend",
            "build_command": "npx tsc --noEmit",
            "deny_list": {
                "paths": ["**/auth/**"],
                "categories": [],
            },
            "triage_md_path": "/tmp/wt/42/avm-backend/TRIAGE.md",
            "escalation_context": None,
            "conflict_context": None,
        },
    )
    assert "## Fixer-Frontend Notes" in out
    assert "## Fixer-Frontend Contract Concern" in out
    assert "FIXER_FRONTEND_COMPLETE" in out
    assert "FIXER_FRONTEND_FAILED" in out
    # No escalation_context block by default.
    assert "HUMAN-APPROVED ESCALATION CONTEXT" not in out


def test_fixer_backend_template_renders_minimal_context() -> None:
    """Fixer-Backend template: side-specific sections + completion tokens."""
    out = _render(
        "fixer_backend.md.j2",
        {
            "bug": {"id": 42, "title": "broken shape"},
            "side": "Backend",
            "worktree_path": "/tmp/wt/42/avm-backend",
            "build_command": "cd api && go build ./...",
            "deny_list": {
                "paths": ["**/migrations/**"],
                "categories": [],
            },
            "triage_md_path": "/tmp/wt/42/avm-backend/TRIAGE.md",
            "escalation_context": None,
            "conflict_context": None,
        },
    )
    assert "## Fixer-Backend Notes" in out
    assert "## Fixer-Backend Contract Concern" in out
    assert "FIXER_BACKEND_COMPLETE" in out
    assert "FIXER_BACKEND_FAILED" in out


def test_fixer_frontend_template_renders_escalation_context() -> None:
    """Escalation context block (Agent 9 approve flow) renders when provided."""
    out = _render(
        "fixer_frontend.md.j2",
        {
            "bug": {"id": 42, "title": "broken shape"},
            "side": "Frontend",
            "worktree_path": "/tmp/wt",
            "build_command": "npx tsc --noEmit",
            "deny_list": {"paths": [], "categories": []},
            "triage_md_path": "/tmp/TRIAGE.md",
            "escalation_context": "Reviewer picked Alternative 2: use soft-delete instead of hard-delete.",
            "conflict_context": None,
        },
    )
    assert "HUMAN-APPROVED ESCALATION CONTEXT" in out
    assert "Alternative 2" in out


def test_fixer_frontend_template_renders_conflict_context() -> None:
    """Agent 11: conflict context block renders when provided."""
    out = _render(
        "fixer_frontend.md.j2",
        {
            "bug": {"id": 42, "title": "broken shape"},
            "side": "Frontend",
            "worktree_path": "/tmp/wt",
            "build_command": "npx tsc --noEmit",
            "deny_list": {"paths": [], "categories": []},
            "triage_md_path": "/tmp/TRIAGE.md",
            "escalation_context": None,
            "conflict_context": (
                "Repo: builder-frontend\nConflicting files:\n  - src/InvoiceForm.tsx"
            ),
        },
    )
    assert "PRIOR FIX HAD A MERGE CONFLICT" in out
    assert "InvoiceForm.tsx" in out


def test_fixer_backend_template_renders_conflict_context() -> None:
    """Agent 11: conflict context block renders for the backend Fixer too."""
    out = _render(
        "fixer_backend.md.j2",
        {
            "bug": {"id": 42, "title": "broken shape"},
            "side": "Backend",
            "worktree_path": "/tmp/wt",
            "build_command": "go build ./...",
            "deny_list": {"paths": [], "categories": []},
            "triage_md_path": "/tmp/TRIAGE.md",
            "escalation_context": None,
            "conflict_context": (
                "Repo: avm-backend\nConflicting files:\n  - api/handlers.go"
            ),
        },
    )
    assert "PRIOR FIX HAD A MERGE CONFLICT" in out
    assert "api/handlers.go" in out


# --- integration_tester.md.j2 ---------------------------------------------


def test_integration_tester_template_renders_minimal_context() -> None:
    """Integration Tester template: AVM readiness adaptation + completion tokens."""
    out = _render(
        "integration_tester.md.j2",
        {
            "bug": {"id": 42, "title": "broken shape"},
            "fe": {
                "worktree_path": "/tmp/wt/42/builder-frontend",
                "dev_command": "npm run dev -- --port 25173 --host 127.0.0.1",
                "integration_port": 25173,
            },
            "be": {
                "worktree_path": "/tmp/wt/42/avm-backend",
                "dev_command": "cd api && PORT=28080 go run ./cmd/server",
                "integration_port": 28080,
            },
            "test_database_url": "postgres://megkrish@localhost:5432/ra_triage_test?sslmode=disable",
            "triage_md_path": "/tmp/wt/42/avm-backend/TRIAGE.md",
        },
    )
    # Completion tokens always present.
    assert "INTEGRATION_TESTER_COMPLETE" in out
    assert "INTEGRATION_TESTER_FAILED" in out
    # AVM readiness adaptation (TCP + <500 HTTP GET, not /health poll).
    assert "AVM has no /health endpoints" in out
    assert "TCP:" in out
    assert "HTTP:" in out
    assert "strictly less than 500" in out
    # Ports + dev commands appear in the rendered prompt.
    assert "28080" in out
    assert "25173" in out
    assert "npm run dev -- --port 25173" in out
    # Output section always present.
    assert "## Integration Tester Notes" in out
    assert "## Integration Tester Failure" in out
