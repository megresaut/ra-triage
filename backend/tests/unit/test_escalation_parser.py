"""Tests for :func:`ra_triage.triage_md.parser.parse_escalation_brief`.

The parser handles the Investigator's structured Escalation Brief so the
Agent 9 API + frontend can render action buttons per alternative. These
tests pin the canonical (well-formed) brief, a sparse brief with only a
TL;DR + single alternative, a brief missing the Ranked alternatives block
entirely (only approve + take_over should be offered), and a fully-absent
section (parser returns ``None``).
"""

from __future__ import annotations

from ra_triage.triage_md import parser
from ra_triage.triage_md.parser import ParsedBrief, ParsedBriefOption

# --- canonical brief --------------------------------------------------------


_CANONICAL_BRIEF = """\
- **TL;DR:** Auth session mishandled; schema migration required.
- **Why blocked:** deny_list path auth/** matched; schema change is destructive.
- **The bug:** Users can't log in after 10-minute idle; root cause is token re-issue path.
- **Why the change is necessary:** The only way to keep legacy tokens valid is to add a new column.
- **Proposed approach:** Additive migration adding `legacy_token` column; medium risk.
- **Ranked alternatives:**
  - **Alternative 1:** Session rehydrate via headers only. Pros: no migration. Cons: breaks old mobile clients. Effort: 2h.
  - **Alternative 2:** Deprecate legacy tokens entirely. Pros: cleanest long-term. Cons: forces every user to re-log. Effort: 30m plus comms.
- **What I need from you:** Approve Recommended | Pick Alternative 1 | Pick Alternative 2 | Take Over Manually
- **Files that would be touched:**
  - backend/auth/token.py
  - backend/migrations/008_add_legacy_token.sql
- **Test plan:** Integration test for session rehydrate + migration dry-run.
"""


def test_parse_canonical_brief_extracts_every_field() -> None:
    sections = {"Escalation Brief": _CANONICAL_BRIEF}
    parsed = parser.parse_escalation_brief(sections)
    assert parsed is not None
    assert isinstance(parsed, ParsedBrief)

    assert parsed.tldr is not None
    assert "session mishandled" in parsed.tldr
    assert parsed.why_blocked is not None
    assert "deny_list path auth/**" in parsed.why_blocked
    assert parsed.the_bug is not None
    assert "10-minute idle" in parsed.the_bug
    assert parsed.why_change_necessary is not None
    assert "legacy tokens" in parsed.why_change_necessary
    assert parsed.proposed_approach is not None
    assert "additive migration".lower() in parsed.proposed_approach.lower()
    assert parsed.test_plan is not None
    assert "session rehydrate" in parsed.test_plan.lower()

    # Alternatives are split in ascending N.
    assert len(parsed.ranked_alternatives) == 2
    assert "session rehydrate via headers only" in parsed.ranked_alternatives[0].lower()
    assert "deprecate legacy tokens" in parsed.ranked_alternatives[1].lower()

    # Files list has leading "- " stripped.
    assert parsed.files_that_would_be_touched == [
        "backend/auth/token.py",
        "backend/migrations/008_add_legacy_token.sql",
    ]

    # Options are synthesized in canonical order.
    assert parsed.options == [
        ParsedBriefOption(
            id="approve_recommended",
            label="Approve Recommended",
            kind="approve",
        ),
        ParsedBriefOption(
            id="alternative_1",
            label="Pick Alternative 1",
            kind="alternative",
        ),
        ParsedBriefOption(
            id="alternative_2",
            label="Pick Alternative 2",
            kind="alternative",
        ),
        ParsedBriefOption(
            id="take_over",
            label="Take Over Manually",
            kind="take_over",
        ),
    ]

    assert parsed.raw == _CANONICAL_BRIEF


# --- sparse brief (TL;DR + one alternative only) ---------------------------


def test_parse_sparse_brief_missing_fields_are_none() -> None:
    sparse = (
        "- **TL;DR:** Quick note about the deny-list hit.\n"
        "- **Ranked alternatives:**\n"
        "  - **Alternative 1:** Just revert the upstream change.\n"
    )
    parsed = parser.parse_escalation_brief({"Escalation Brief": sparse})
    assert parsed is not None
    assert parsed.tldr == "Quick note about the deny-list hit."
    assert parsed.why_blocked is None
    assert parsed.the_bug is None
    assert parsed.why_change_necessary is None
    assert parsed.proposed_approach is None
    assert parsed.test_plan is None
    assert parsed.files_that_would_be_touched == []

    assert len(parsed.ranked_alternatives) == 1
    assert "revert the upstream change" in parsed.ranked_alternatives[0].lower()

    # Options: approve + one alternative + take_over.
    option_ids = [opt.id for opt in parsed.options]
    assert option_ids == ["approve_recommended", "alternative_1", "take_over"]


# --- brief without ranked alternatives section -----------------------------


def test_parse_brief_without_alternatives_offers_only_approve_and_take_over() -> None:
    body = (
        "- **TL;DR:** Investigator found a deny-list hit but couldn't enumerate alternatives.\n"
        "- **Why blocked:** deny_list path migrations/** matched.\n"
        "- **Proposed approach:** Run the migration under a feature flag.\n"
    )
    parsed = parser.parse_escalation_brief({"Escalation Brief": body})
    assert parsed is not None
    assert parsed.ranked_alternatives == []
    assert [o.id for o in parsed.options] == ["approve_recommended", "take_over"]
    # Non-alternative fields still populate correctly.
    assert parsed.proposed_approach is not None
    assert "feature flag" in parsed.proposed_approach.lower()


# --- missing section → None ------------------------------------------------


def test_parse_missing_brief_returns_none() -> None:
    assert parser.parse_escalation_brief({}) is None


def test_parse_empty_brief_returns_none() -> None:
    assert parser.parse_escalation_brief({"Escalation Brief": "   \n\n"}) is None


# --- edge: field alias accepted (old wording) ------------------------------


def test_parse_accepts_alias_for_why_blocked() -> None:
    # Product plan uses "Why this is blocked" — tech plan prompt uses
    # "Why blocked". Parser should accept either.
    body = (
        "- **TL;DR:** sample\n"
        "- **Why this is blocked:** because schemas are deny-listed.\n"
    )
    parsed = parser.parse_escalation_brief({"Escalation Brief": body})
    assert parsed is not None
    assert parsed.why_blocked is not None
    assert "schemas" in parsed.why_blocked


# --- edge: alternatives with non-sequential N sorts ascending --------------


def test_parse_sorts_alternatives_by_n() -> None:
    body = (
        "- **TL;DR:** sample\n"
        "- **Ranked alternatives:**\n"
        "  - **Alternative 2:** second option\n"
        "  - **Alternative 1:** first option\n"
    )
    parsed = parser.parse_escalation_brief({"Escalation Brief": body})
    assert parsed is not None
    assert len(parsed.ranked_alternatives) == 2
    assert parsed.ranked_alternatives[0].startswith("first option")
    assert parsed.ranked_alternatives[1].startswith("second option")


# --- edge: files list handles indented bullets -----------------------------


def test_parse_files_list_picks_up_indented_bullets() -> None:
    body = (
        "- **TL;DR:** sample\n"
        "- **Files that would be touched:**\n"
        "  - src/a.py\n"
        "  - src/b.py\n"
    )
    parsed = parser.parse_escalation_brief({"Escalation Brief": body})
    assert parsed is not None
    assert parsed.files_that_would_be_touched == ["src/a.py", "src/b.py"]
