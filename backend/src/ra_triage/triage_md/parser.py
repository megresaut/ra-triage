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
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

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


__all__ = [
    "append",
    "get_scope",
    "has_escalation",
    "parse",
]
