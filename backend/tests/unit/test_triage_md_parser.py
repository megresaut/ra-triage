"""Tests for ``ra_triage.triage_md.parser`` + ``schema``."""

from __future__ import annotations

from pathlib import Path

from ra_triage.triage_md import parser
from ra_triage.triage_md.schema import CANONICAL_SECTIONS


def test_canonical_sections_contains_key_names() -> None:
    """Spot-check: the canonical list covers every section the Investigator,
    Fixer, Tester, and Integration Tester write in v1."""
    expected = {
        "Root Cause",
        "Scope",
        "Analysis",
        "Recommended Fix Approach",
        "Escalation Brief",
        "Cross-Repo Contract",
        "Fixer Notes",
        "Fixer-Frontend Notes",
        "Fixer-Backend Notes",
        "Tester Notes",
        "Integration Tester Notes",
    }
    assert expected.issubset(set(CANONICAL_SECTIONS))


def test_parse_missing_file_returns_empty_dict(tmp_path: Path) -> None:
    """Non-existent file → {}, not an error."""
    assert parser.parse(tmp_path / "nope.md") == {}


def test_parse_happy_path(tmp_path: Path) -> None:
    """Canonical multi-section TRIAGE.md parses in order with stripped bodies."""
    path = tmp_path / "TRIAGE.md"
    path.write_text(
        "## Root Cause\n\n"
        "src/handler.py:42\n\n"
        "## Scope\n\n"
        "scope: single-repo\n"
        'affected_repos: ["a"]\n'
        "deny_list_hit: false\n\n"
        "## Analysis\n\nBlah blah.\n",
        encoding="utf-8",
    )
    result = parser.parse(path)
    assert list(result.keys()) == ["Root Cause", "Scope", "Analysis"]
    assert result["Root Cause"] == "src/handler.py:42"
    assert result["Analysis"] == "Blah blah."
    assert result["Scope"].startswith("scope: single-repo")


def test_parse_preserves_interior_blank_lines(tmp_path: Path) -> None:
    """Interior whitespace inside a section body survives parsing."""
    path = tmp_path / "TRIAGE.md"
    path.write_text(
        "## Root Cause\n\nline one\n\nline two\n",
        encoding="utf-8",
    )
    result = parser.parse(path)
    assert "\n\n" in result["Root Cause"]
    assert "line one" in result["Root Cause"]
    assert "line two" in result["Root Cause"]


def test_parse_captures_unknown_sections(tmp_path: Path) -> None:
    """Unknown section names are still captured — tolerance by design."""
    path = tmp_path / "TRIAGE.md"
    path.write_text(
        "## Custom Draft\n\nsomething\n\n## Root Cause\n\nreal one\n",
        encoding="utf-8",
    )
    result = parser.parse(path)
    assert "Custom Draft" in result
    assert result["Custom Draft"] == "something"
    assert result["Root Cause"] == "real one"


def test_parse_ignores_deeper_headings(tmp_path: Path) -> None:
    """``###`` is body content, not a new section."""
    path = tmp_path / "TRIAGE.md"
    path.write_text(
        "## Analysis\n\n### subheading\n\ndetail\n",
        encoding="utf-8",
    )
    result = parser.parse(path)
    assert set(result.keys()) == {"Analysis"}
    assert "subheading" in result["Analysis"]


def test_append_creates_file_when_missing(tmp_path: Path) -> None:
    """``append`` is a create-if-missing operation."""
    path = tmp_path / "TRIAGE.md"
    parser.append(path, "Root Cause", "x\ny")
    assert path.exists()
    assert "## Root Cause" in path.read_text(encoding="utf-8")


def test_append_round_trip(tmp_path: Path) -> None:
    """``parse(append(empty, "Root Cause", "x\\ny")) == {"Root Cause": "x\\ny"}``."""
    path = tmp_path / "TRIAGE.md"
    parser.append(path, "Root Cause", "x\ny")
    parsed = parser.parse(path)
    assert parsed == {"Root Cause": "x\ny"}


def test_append_adds_blank_line_between_sections(tmp_path: Path) -> None:
    """Two successive appends render as two well-separated sections."""
    path = tmp_path / "TRIAGE.md"
    parser.append(path, "Root Cause", "cause")
    parser.append(path, "Analysis", "detail")
    parsed = parser.parse(path)
    assert parsed == {"Root Cause": "cause", "Analysis": "detail"}


def test_append_handles_file_without_trailing_newline(tmp_path: Path) -> None:
    """Appending to a file missing trailing newline still produces a clean structure."""
    path = tmp_path / "TRIAGE.md"
    path.write_text("preamble", encoding="utf-8")
    parser.append(path, "Root Cause", "body")
    parsed = parser.parse(path)
    assert parsed["Root Cause"] == "body"


def test_get_scope_parses_single_repo(tmp_path: Path) -> None:
    """Single-repo scope extracts correctly with JSON affected_repos."""
    sections = {
        "Scope": (
            "scope: single-repo\n"
            'affected_repos: ["avm-backend"]\n'
            "deny_list_hit: false\n"
        )
    }
    got = parser.get_scope(sections)
    assert got["scope"] == "single-repo"
    assert got["affected_repos"] == ["avm-backend"]
    assert got["deny_list_hit"] is False


def test_get_scope_parses_cross_repo_with_comma_list() -> None:
    """Comma-separated ``affected_repos`` also parses (tolerance)."""
    sections = {
        "Scope": (
            "scope: cross-repo\n"
            "affected_repos: avm-backend, builder-frontend\n"
            "deny_list_hit: true\n"
        )
    }
    got = parser.get_scope(sections)
    assert got["scope"] == "cross-repo"
    assert got["affected_repos"] == ["avm-backend", "builder-frontend"]
    assert got["deny_list_hit"] is True


def test_get_scope_missing_returns_safe_defaults() -> None:
    """Missing Scope section → scope=None, empty repos, False deny flag."""
    got = parser.get_scope({})
    assert got["scope"] is None
    assert got["affected_repos"] == []
    assert got["deny_list_hit"] is False


def test_get_scope_malformed_value_for_scope_key_returns_none() -> None:
    """Unknown scope value (e.g., typo) → None so caller can flag malformed."""
    got = parser.get_scope({"Scope": "scope: somewhere-else\n"})
    assert got["scope"] is None


def test_has_escalation_true_when_non_empty() -> None:
    """An Escalation Brief with body content trips has_escalation."""
    sections = {"Escalation Brief": "TL;DR: auth change"}
    assert parser.has_escalation(sections) is True


def test_has_escalation_false_when_missing() -> None:
    """Missing section → False."""
    assert parser.has_escalation({}) is False


def test_has_escalation_false_when_empty_body() -> None:
    """Empty-body section → False (drafting agents shouldn't trigger blocking)."""
    assert parser.has_escalation({"Escalation Brief": "   \n\n"}) is False
