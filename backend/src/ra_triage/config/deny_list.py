"""Deny-list path matching.

The deny-list itself is per-project data stored inside `ProjectConfig`; this
module just exposes the matching helpers. The Investigator uses these to
decide whether to escalate; the Fixer uses them as a second line of defense
before writing.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

from ra_triage.config.registry import DenyList


def path_hits_deny_list(path: str, deny_list: DenyList) -> bool:
    """Return True if `path` matches any glob pattern on the deny-list.

    Uses `fnmatch` (case-sensitive) against every configured pattern. Handles
    the `**/` prefix convention by stripping it and also matching the pattern
    at path root — `fnmatch` doesn't treat `**` as "zero-or-more path
    segments", so `**/schema.sql` would otherwise miss a top-level `schema.sql`.
    """
    normalized = path.replace("\\", "/")
    for pattern in deny_list.paths:
        if fnmatchcase(normalized, pattern):
            return True
        # Support zero-depth matches when the pattern starts with "**/".
        if pattern.startswith("**/") and fnmatchcase(normalized, pattern[3:]):
            return True
    return False


def category_hits_deny_list(category: str, deny_list: DenyList) -> bool:
    """Return True if `category` is a deny-listed change category."""
    return category in deny_list.categories
