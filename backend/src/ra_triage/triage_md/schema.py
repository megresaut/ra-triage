"""Canonical TRIAGE.md section names.

Tech plan §5.4 defines the contract between the agents and the orchestrator:
every agent appends well-named `## ...` sections; the orchestrator parses the
file after each run and decides the next transition based on section
presence + content.

The order below is the expected ordering in a fully-populated TRIAGE.md.
Agents write in phases (Investigator writes Root Cause → Escalation Brief;
Fixer/Tester append Fixer Notes / Tester Notes later). Unknown sections are
still captured by the parser (tolerance for drafting agents), but are not
canonical.
"""

from __future__ import annotations

CANONICAL_SECTIONS: list[str] = [
    "Root Cause",
    "Scope",
    "Analysis",
    "Recommended Fix Approach",
    "Escalation Brief",
    "Cross-Repo Contract",
    "Fixer Notes",
    "Fixer-Frontend Notes",
    "Fixer-Backend Notes",
    "Fixer-Frontend Contract Concern",
    "Fixer-Backend Contract Concern",
    "Tester Notes",
    "Tester Failure",
    "Integration Tester Notes",
    "Integration Tester Failure",
    "Investigator Failure",
    "Fixer Failure",
    "Fixer Escalation",
]

#: Canonical names as a set, for membership checks.
CANONICAL_SECTION_SET: frozenset[str] = frozenset(CANONICAL_SECTIONS)


__all__ = ["CANONICAL_SECTIONS", "CANONICAL_SECTION_SET"]
