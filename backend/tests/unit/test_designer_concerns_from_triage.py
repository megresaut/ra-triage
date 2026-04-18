"""Tests for Agent 12's TRIAGE.md-driven designer concerns reconstruction.

Agent 10's pipeline kept ``designer_concerns`` text in a process-local
``_designer_concerns_holder`` dict. That dict dies on backend restart,
so cross-repo bugs that were mid-cycle when the backend died would lose
the contract concerns / integration failure context on resume.

Agent 12's fix: re-derive concerns from TRIAGE.md whenever the
in-memory holder is empty. The pipeline's helper
``_reconstruct_designer_concerns_from_triage`` does the parse and
formatting; this test exercises it directly.
"""

from __future__ import annotations

from pathlib import Path

from ra_triage.orchestrator import pipeline as pipeline_module


def test_no_triage_md_returns_none(tmp_path: Path) -> None:
    """Missing TRIAGE.md → None."""
    triage_md = tmp_path / "TRIAGE.md"
    assert (
        pipeline_module._reconstruct_designer_concerns_from_triage(triage_md)
        is None
    )


def test_empty_triage_md_returns_none(tmp_path: Path) -> None:
    """TRIAGE.md exists but has no relevant sections → None."""
    triage_md = tmp_path / "TRIAGE.md"
    triage_md.write_text("## Root Cause\n\nThe usual.\n", encoding="utf-8")
    assert (
        pipeline_module._reconstruct_designer_concerns_from_triage(triage_md)
        is None
    )


def test_fe_concern_only_reconstructs_blob(tmp_path: Path) -> None:
    """A Fixer-Frontend Contract Concern alone produces a single-side blob."""
    triage_md = tmp_path / "TRIAGE.md"
    triage_md.write_text(
        "## Cross-Repo Contract\n\n- backend: GET /api/x\n\n"
        "## Fixer-Frontend Contract Concern\n\n"
        "The contract's response shape doesn't match what useInvoices() needs.\n",
        encoding="utf-8",
    )
    blob = pipeline_module._reconstruct_designer_concerns_from_triage(triage_md)
    assert blob is not None
    assert "Frontend Fixer flagged the contract" in blob
    assert "useInvoices" in blob


def test_be_concern_only_reconstructs_blob(tmp_path: Path) -> None:
    """A Fixer-Backend Contract Concern alone produces a single-side blob."""
    triage_md = tmp_path / "TRIAGE.md"
    triage_md.write_text(
        "## Cross-Repo Contract\n\n- frontend: POST /api/y\n\n"
        "## Fixer-Backend Contract Concern\n\n"
        "The contract demands a synchronous flow but our worker is async.\n",
        encoding="utf-8",
    )
    blob = pipeline_module._reconstruct_designer_concerns_from_triage(triage_md)
    assert blob is not None
    assert "Backend Fixer flagged the contract" in blob
    assert "async" in blob


def test_both_concerns_reconstructs_combined_blob(tmp_path: Path) -> None:
    """Both Fixer concerns produce a combined blob (FE + BE)."""
    triage_md = tmp_path / "TRIAGE.md"
    triage_md.write_text(
        "## Fixer-Frontend Contract Concern\n\nFE problem.\n\n"
        "## Fixer-Backend Contract Concern\n\nBE problem.\n",
        encoding="utf-8",
    )
    blob = pipeline_module._reconstruct_designer_concerns_from_triage(triage_md)
    assert blob is not None
    assert "FE problem" in blob
    assert "BE problem" in blob
    assert "Frontend Fixer" in blob
    assert "Backend Fixer" in blob


def test_integration_failure_falls_back_to_failure_blob(tmp_path: Path) -> None:
    """When no Fixer concerns but an Integration Tester Failure → integration blob."""
    triage_md = tmp_path / "TRIAGE.md"
    triage_md.write_text(
        "## Cross-Repo Contract\n\n- backend: GET /a\n\n"
        "## Integration Tester Failure\n\n"
        "- end-to-end test never saw the new field on the response.\n",
        encoding="utf-8",
    )
    blob = pipeline_module._reconstruct_designer_concerns_from_triage(triage_md)
    assert blob is not None
    assert "Integration Tester reported failure" in blob
    assert "new field" in blob


def test_fixer_concerns_take_precedence_over_integration_failure(
    tmp_path: Path,
) -> None:
    """Fixer concerns are preferred over integration-failure body when both exist."""
    triage_md = tmp_path / "TRIAGE.md"
    triage_md.write_text(
        "## Fixer-Frontend Contract Concern\n\nFE problem.\n\n"
        "## Integration Tester Failure\n\nold failure (resolved).\n",
        encoding="utf-8",
    )
    blob = pipeline_module._reconstruct_designer_concerns_from_triage(triage_md)
    assert blob is not None
    assert "FE problem" in blob
    assert "Integration Tester reported failure" not in blob


def test_holder_path_uses_reconstruction_when_holder_empty(
    tmp_path: Path,
) -> None:
    """The holder dict is the in-memory cache; emptying it forces reconstruction.

    This is a unit-level test of the integration: simulate a fresh
    pipeline boot (holder cleared) for a bug whose TRIAGE.md still
    has Fixer concerns from the previous cycle, and verify the
    reconstruction helper reads the concerns out.
    """
    triage_md = tmp_path / "TRIAGE.md"
    triage_md.write_text(
        "## Fixer-Backend Contract Concern\n\n"
        "Survived the restart.\n",
        encoding="utf-8",
    )
    # Empty the holder — simulating a fresh-boot pipeline body.
    pipeline_module._designer_concerns_clear()
    assert pipeline_module._designer_concerns_holder.get(99) is None

    blob = pipeline_module._reconstruct_designer_concerns_from_triage(triage_md)
    assert blob is not None
    assert "Survived the restart" in blob
