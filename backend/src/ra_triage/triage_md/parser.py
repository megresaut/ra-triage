"""Parser + writer for TRIAGE.md.

TRIAGE.md is the shared scratchpad that every agent reads and appends to. The
parser below walks the file line-by-line, splitting on ``## <name>`` ATX
headings and returning an insertion-ordered mapping of section name → body
text.

Design choices:
  - ``##`` is the ONLY heading level that delimits sections. ``#`` is a
    page title (we ignore it when it appears — it joins the preamble text
    before the first section, if any). ``###`` and deeper are treated as
    part of the current section body.
  - Unknown section names (not in ``CANONICAL_SECTIONS``) are still parsed
    and returned; the orchestrator is tolerant of extra drafting sections.
  - Body text has leading + trailing whitespace stripped. Interior blank
    lines are preserved.
  - ``append(path, section, body)`` creates the file if missing. When the
    existing file doesn't end with a blank line, we insert one before the
    new ``## <section>`` heading so successive appends render cleanly.

The Scope section uses a specific ``key: value`` shape the Investigator
writes per the prompt template (tech plan §9.1). ``get_scope`` extracts it
into a typed dict that the pipeline uses for state routing.

The Escalation Brief uses a structured bulleted shape (tech plan §9.1 /
product plan "Escalation Brief"). ``parse_escalation_brief`` walks those
bullets into a :class:`ParsedBrief` that the API + frontend action flow
consumes directly (Agent 9). The parser is deliberately tolerant: missing
sub-fields collapse to ``None``, and the ``options`` list is ALWAYS
normalized to (approve_recommended, alternative_1..N, take_over) in that
order regardless of the agent's prose.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

# Matches an ATX ``## Heading`` line. Must be at line start; trailing
# whitespace is tolerated. ``###`` and deeper headings are deliberately
# excluded by the ``(?!#)`` lookahead (they are body content).
_SECTION_HEADING = re.compile(r"^##\s+(?P<name>[^\n]+?)\s*#*\s*$")


def parse(path: Path) -> dict[str, str]:
    """Parse a TRIAGE.md file into ``{section_name: body}``.

    Returns an empty dict if the file does not exist or has no ``##``
    headings. Section order in the returned dict matches file order
    (Python 3.7+ dict preserves insertion order). Body text is stripped of
    leading/trailing whitespace; interior whitespace is preserved as-is.

    Section names in the input file are trimmed of trailing ``#`` (so
    ``## Root Cause ##`` and ``## Root Cause`` both resolve to ``Root
    Cause``). Duplicate headings keep the LAST body — agents shouldn't emit
    duplicates, but we choose a stable behavior anyway.
    """
    if not path.exists():
        return {}
    content = path.read_text(encoding="utf-8")
    return _parse_text(content)


def _parse_text(content: str) -> dict[str, str]:
    """Internal pure-function parser. Public entry is ``parse(path)``."""
    lines = content.splitlines()
    sections: dict[str, list[str]] = {}
    current: str | None = None
    buffer: list[str] = []

    def _flush() -> None:
        if current is None:
            return
        body = "\n".join(buffer).strip("\n")
        body = body.strip()
        sections[current] = [body]

    for line in lines:
        match = _SECTION_HEADING.match(line)
        if match:
            _flush()
            current = match.group("name").strip()
            buffer = []
            continue
        if current is not None:
            buffer.append(line)
    _flush()

    return {name: parts[0] for name, parts in sections.items()}


def append(path: Path, section: str, body: str) -> None:
    """Append a ``## {section}\\n\\n{body}\\n`` block to ``path``.

    Creates the file + parent directories if missing. Ensures exactly one
    blank line between the existing content and the new heading (so
    successive appends render cleanly even if the previous writer forgot
    the trailing newline).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    stripped_body = body.rstrip("\n")
    new_block = f"## {section}\n\n{stripped_body}\n"

    if not path.exists() or path.stat().st_size == 0:
        path.write_text(new_block, encoding="utf-8")
        return

    existing = path.read_text(encoding="utf-8")
    # Ensure the existing content ends with exactly one blank line before
    # the new heading — i.e. the last character is "\n\n". Normalize by
    # stripping trailing newlines and adding exactly two.
    prefix = existing.rstrip("\n") + "\n\n"
    path.write_text(prefix + new_block, encoding="utf-8")


def get_scope(sections: dict[str, str]) -> dict[str, Any]:
    """Parse the ``Scope`` section body into a typed dict.

    Expected body shape (per the Investigator prompt, tech plan §9.1)::

        scope: single-repo | cross-repo
        affected_repos: [repo_a, repo_b]
        deny_list_hit: true | false

    Returned dict always has the three keys. On a missing or malformed
    ``Scope`` section:
      - ``scope`` is ``None`` (caller treats as malformed).
      - ``affected_repos`` is ``[]``.
      - ``deny_list_hit`` is ``False``.

    ``affected_repos`` is tolerant: we accept a JSON array or a
    comma-separated list. Bare bracketed values like ``[a, b]`` are also
    accepted via a simple strip-and-split.
    """
    result: dict[str, Any] = {
        "scope": None,
        "affected_repos": [],
        "deny_list_hit": False,
    }

    body = sections.get("Scope")
    if not body:
        return result

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()

        if key == "scope":
            normalized = value.lower()
            if normalized in {"single-repo", "cross-repo"}:
                result["scope"] = normalized
            else:
                # Keep None so caller sees malformed scope.
                result["scope"] = None
        elif key == "affected_repos":
            result["affected_repos"] = _parse_affected_repos(value)
        elif key == "deny_list_hit":
            result["deny_list_hit"] = value.lower() in {"true", "yes", "1"}

    return result


def _parse_affected_repos(value: str) -> list[str]:
    """Parse ``affected_repos: ...`` value into a list of strings.

    Accepts: a JSON array, a comma-separated list, or a bracketed informal
    list. Returns ``[]`` for empty / None.
    """
    if not value:
        return []

    # Try JSON first — ``["a", "b"]`` is the most precise form and the
    # prompt template shows it that way.
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [str(v).strip() for v in parsed if str(v).strip()]

    # Strip brackets for the ``[a, b]`` informal case.
    stripped = value.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        stripped = stripped[1:-1]

    parts = [p.strip().strip('"').strip("'") for p in stripped.split(",")]
    return [p for p in parts if p]


def has_escalation(sections: dict[str, str]) -> bool:
    """Return True if the parsed sections contain a non-empty Escalation Brief."""
    body = sections.get("Escalation Brief")
    if body is None:
        return False
    return bool(body.strip())


# --- Escalation Brief parsing ------------------------------------------------

#: Regex that matches the ``- **<Field>:**`` anchor the Investigator emits.
#: The field name is captured lowercased for case-insensitive dispatch. We
#: allow optional trailing text on the same line (the start of the body)
#: because the Investigator's prompt template shows both shapes.
_ESCALATION_FIELD_LINE = re.compile(
    r"^\s*-\s*\*\*(?P<field>[^*:]+?)\s*:\*\*\s*(?P<rest>.*)$"
)

#: Regex that detects an Alternative N anchor inside the Ranked alternatives
#: body. Used to split the blob into per-alternative bodies.
_ALTERNATIVE_ANCHOR = re.compile(
    r"^\s*-\s*\*\*Alternative\s+(?P<n>\d+)\s*:\*\*\s*(?P<rest>.*)$",
    re.IGNORECASE,
)

#: Regex that detects a bullet line inside a field body (used to extract
#: file-path lists). Matches ``- <content>`` with optional leading spaces.
_BULLET_LINE = re.compile(r"^\s*-\s+(?P<content>.+?)\s*$")

#: Aliases that map the Investigator's prose labels to the ``ParsedBrief``
#: attribute name. Lowercased on comparison. Kept explicit so a reshuffled
#: prompt wording doesn't silently drop a field.
_FIELD_ALIASES: dict[str, str] = {
    "tl;dr": "tldr",
    "tldr": "tldr",
    "why blocked": "why_blocked",
    "why this is blocked": "why_blocked",
    "the bug": "the_bug",
    "why the change is necessary": "why_change_necessary",
    "why the escalation-triggering change is necessary": "why_change_necessary",
    "why change necessary": "why_change_necessary",
    "proposed approach": "proposed_approach",
    "ranked alternatives": "ranked_alternatives",
    "what i need from you": "_what_i_need",  # not surfaced as an attr
    "files that would be touched": "files_that_would_be_touched",
    "test plan": "test_plan",
}


@dataclass(frozen=True)
class ParsedBriefOption:
    """One option the reviewer can pick on an escalated bug."""

    id: str  # "approve_recommended" | f"alternative_{N}" | "take_over"
    label: str
    kind: Literal["approve", "alternative", "take_over"]


@dataclass(frozen=True)
class ParsedBrief:
    """Structured view of the Investigator's ``## Escalation Brief`` body."""

    raw: str
    tldr: str | None = None
    why_blocked: str | None = None
    the_bug: str | None = None
    why_change_necessary: str | None = None
    proposed_approach: str | None = None
    ranked_alternatives: list[str] = field(default_factory=list)
    files_that_would_be_touched: list[str] = field(default_factory=list)
    test_plan: str | None = None
    options: list[ParsedBriefOption] = field(default_factory=list)


def parse_escalation_brief(sections: dict[str, str]) -> ParsedBrief | None:
    """Parse the ``Escalation Brief`` section into a :class:`ParsedBrief`.

    Returns ``None`` if the section is absent or empty. Missing sub-fields
    return as ``None`` / empty lists on the result; the ``raw`` attribute
    always holds the original body verbatim so the UI can fall back to a
    full markdown render.

    Parsing strategy: we scan the body line-by-line collecting ``- **<Field>:**``
    anchors. Each anchor opens a new field; subsequent lines that are not
    anchors (and not blank-before-anchor) are appended to the current
    field's body. ``Ranked alternatives`` is a special case — its body is
    further split on ``- **Alternative N:**`` markers into a list of
    per-alternative bodies. ``Files that would be touched`` has its bullets
    extracted into a flat string list.

    The ``options`` list is always synthesized in canonical order (approve
    first, alternatives next in ascending N, take-over last) regardless of
    what the Investigator wrote under "What I need from you" — the
    Investigator's prose drives the alternative labels, but the order and
    identifiers are orchestrator-controlled so the API + frontend have a
    stable contract.
    """
    body = sections.get("Escalation Brief")
    if body is None:
        return None
    if not body.strip():
        return None

    fields = _collect_field_bodies(body)
    alternatives = _split_alternatives(fields.get("ranked_alternatives", ""))
    files_list = _extract_bulleted_lines(fields.get("files_that_would_be_touched", ""))
    options = _build_options(alternatives)

    return ParsedBrief(
        raw=body,
        tldr=_nonempty(fields.get("tldr")),
        why_blocked=_nonempty(fields.get("why_blocked")),
        the_bug=_nonempty(fields.get("the_bug")),
        why_change_necessary=_nonempty(fields.get("why_change_necessary")),
        proposed_approach=_nonempty(fields.get("proposed_approach")),
        ranked_alternatives=alternatives,
        files_that_would_be_touched=files_list,
        test_plan=_nonempty(fields.get("test_plan")),
        options=options,
    )


def _collect_field_bodies(body: str) -> dict[str, str]:
    """Walk the brief body, collecting each ``- **<Field>:**`` block.

    Lines after an anchor (up to the next anchor) are treated as the
    field's body. Blank lines before the first anchor are discarded.
    Unknown field labels are dropped silently — they never land in the
    returned dict — but their body text DOES continue to accumulate under
    them internally so subsequent continuation lines don't leak into the
    previous known field.
    """
    collected: dict[str, list[str]] = {}
    current_attr: str | None = None
    current_lines: list[str] = []

    def _flush() -> None:
        if current_attr is None:
            return
        text = "\n".join(current_lines).strip()
        # Preserve the LAST write for duplicate fields — agents shouldn't
        # emit duplicates, but if they do, we behave predictably.
        collected[current_attr] = [text]

    for raw_line in body.splitlines():
        match = _ESCALATION_FIELD_LINE.match(raw_line)
        # Only treat a line as a TOP-LEVEL field anchor if its label is in
        # the known alias table. Unknown labels (e.g. "Alternative 1")
        # are allowed to be continuation text for the current field —
        # this lets "Ranked alternatives" wrap nested alternative bullets
        # without us closing the parent field prematurely.
        if match:
            field_label = match.group("field").strip().lower()
            if field_label in _FIELD_ALIASES:
                _flush()
                current_attr = _FIELD_ALIASES[field_label]
                rest = match.group("rest").strip()
                current_lines = [rest] if rest else []
                continue
        if current_attr is None:
            # Unanchored lines before the first known field — ignore.
            continue
        current_lines.append(raw_line)

    _flush()
    # Drop unknown fields (they're stored under None-mapped aliases we don't
    # care about, e.g. "_what_i_need").
    return {k: v[0] for k, v in collected.items() if not k.startswith("_")}


def _split_alternatives(blob: str) -> list[str]:
    """Split a ranked-alternatives blob on ``- **Alternative N:**`` anchors.

    Returns a list of bodies (one per alternative). Leading preamble
    before the first anchor is discarded — it's typically empty, and the
    Investigator prompt template shows the anchor as the first line.
    Alternatives are ordered by their N (ascending) to match the options
    list the orchestrator synthesizes.
    """
    if not blob.strip():
        return []

    # Pass 1: scan for anchors, recording (N, start_line) pairs.
    lines = blob.splitlines()
    anchors: list[tuple[int, int, str]] = []  # (n, line_idx, rest_of_anchor_line)
    for idx, raw_line in enumerate(lines):
        m = _ALTERNATIVE_ANCHOR.match(raw_line)
        if m:
            try:
                n_value = int(m.group("n"))
            except ValueError:
                continue
            anchors.append((n_value, idx, m.group("rest")))

    if not anchors:
        return []

    # Pass 2: slice each alternative's body from its anchor to the next.
    bodies: list[tuple[int, str]] = []
    for i, (n, start, rest) in enumerate(anchors):
        end = anchors[i + 1][1] if i + 1 < len(anchors) else len(lines)
        continuation = "\n".join(lines[start + 1 : end])
        combined_parts: list[str] = []
        if rest.strip():
            combined_parts.append(rest.strip())
        if continuation.strip():
            combined_parts.append(continuation.rstrip())
        body = "\n".join(combined_parts).strip()
        bodies.append((n, body))

    bodies.sort(key=lambda pair: pair[0])
    return [body for _n, body in bodies]


def _extract_bulleted_lines(blob: str) -> list[str]:
    """Return the trimmed text of every ``- <content>`` line in ``blob``.

    Used for the "Files that would be touched" field so callers get a
    flat list of paths. Non-bullet lines are ignored.
    """
    if not blob.strip():
        return []
    out: list[str] = []
    for raw_line in blob.splitlines():
        m = _BULLET_LINE.match(raw_line)
        if m:
            content = m.group("content").strip()
            if content:
                out.append(content)
    return out


def _build_options(alternatives: list[str]) -> list[ParsedBriefOption]:
    """Synthesize the canonical options list in stable order."""
    options: list[ParsedBriefOption] = [
        ParsedBriefOption(
            id="approve_recommended",
            label="Approve Recommended",
            kind="approve",
        )
    ]
    for idx in range(1, len(alternatives) + 1):
        options.append(
            ParsedBriefOption(
                id=f"alternative_{idx}",
                label=f"Pick Alternative {idx}",
                kind="alternative",
            )
        )
    options.append(
        ParsedBriefOption(
            id="take_over",
            label="Take Over Manually",
            kind="take_over",
        )
    )
    return options


def _nonempty(value: str | None) -> str | None:
    """Return ``value`` stripped, or ``None`` if it's empty / whitespace."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


__all__ = [
    "ParsedBrief",
    "ParsedBriefOption",
    "append",
    "get_scope",
    "has_escalation",
    "parse",
    "parse_escalation_brief",
]
