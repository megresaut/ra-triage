"""Repository-style CRUD helpers for bugs, agent runs, etc.

Ownership: **Agent 2** implements `create_bug`, `get_bug`, `list_bugs`,
`update_bug_state`, `record_agent_run`, `record_rejection`, `record_pr`, etc.
Agent 3 wires Notion status write-back through `update_bug_state`'s
post-transition side-effect hook. This file is intentionally a stub.
"""

from __future__ import annotations
