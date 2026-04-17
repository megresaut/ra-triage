"""Claude Code subprocess wrapper for RA Triage agents.

This is the shared spine for every agent role (Investigator, Fixer,
Tester, etc.). Each concrete agent subclasses :class:`ClaudeCodeAgent`
and declares its role, completion tokens, tool allowlist, and prompt
template. The base class handles the things every agent needs:

- Render the Jinja prompt template with the per-run context.
- Persist the rendered prompt under ``$TRIAGE_HOME/prompts/<bug_id>/``.
- Spawn ``claude`` with the exact argv tech plan §5.1 / §9 specifies.
- Stream the subprocess's stdout line-by-line — every line is (a) written
  to a per-run log file under ``$TRIAGE_HOME/logs/<bug_id>/``, (b)
  published on the event bus for WebSocket fan-out, and (c) scanned for
  the role's completion tokens so the orchestrator can tell SUCCESS apart
  from FAILED apart from a crash.
- Enforce the 30-minute wall-clock timeout (tech plan §16).
- Snapshot the post-run TRIAGE.md content into the returned result so the
  orchestrator can decide the next transition without re-reading the file.

Stream-json parsing
-------------------

Claude Code's ``--output-format stream-json`` prints one JSON object per
line. Each line has a ``type`` field (``system``, ``assistant``,
``user``, ``result``) and an embedded ``message`` or ``result`` payload.
We unwrap the interesting text content into a compact single-line
human-readable form for the log file and the WS stream; if a line is not
valid JSON we log the raw line so operators can still debug.

The streamer also tees the raw JSON to the log file (under a ``raw=``
prefix) so replay with ``jq`` remains possible if we need it.

Tests override ``claude_cli_path`` via the module-level
:data:`_claude_cli_override` seam so we can exercise the streaming +
completion-token logic without invoking the real Claude CLI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ra_triage.settings import get_settings
from ra_triage.utils.events import publish_agent_stdout

logger = logging.getLogger(__name__)


# --- Jinja loader --------------------------------------------------------

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# StrictUndefined turns a missing context variable into a loud error at
# render time rather than silently rendering an empty string — prompts
# drive agent behavior and a typo in a context key should surface loudly.
_jinja_env = Environment(
    loader=FileSystemLoader(str(_PROMPTS_DIR)),
    undefined=StrictUndefined,
    trim_blocks=False,
    lstrip_blocks=False,
    keep_trailing_newline=True,
    autoescape=False,  # these are prompts, not HTML
)


# --- Test seam -----------------------------------------------------------

#: Tests set this to a path to a fake CLI to exercise the subprocess
#: streaming + completion-token detection without needing real Claude.
#: Production leaves it as ``None`` and ``shutil.which("claude")`` is used
#: instead.
_claude_cli_override: list[str] | None = None


def set_claude_cli_override(argv: list[str] | None) -> None:
    """Override the ``claude`` invocation argv for tests.

    Pass a list like ``["/usr/bin/python3", "/path/to/fake_claude.py"]`` to
    run a stand-in; pass ``None`` to restore default behavior
    (``shutil.which("claude")`` with no pre-args).
    """
    global _claude_cli_override
    _claude_cli_override = argv


def _resolve_claude_argv() -> list[str]:
    """Return the argv prefix for invoking Claude Code.

    Honors :data:`_claude_cli_override` first; otherwise resolves the real
    ``claude`` binary via ``shutil.which``. Raises :class:`RuntimeError`
    with a clear message if neither is available.
    """
    if _claude_cli_override is not None:
        return list(_claude_cli_override)
    resolved = shutil.which("claude")
    if not resolved:
        raise RuntimeError(
            "Claude Code CLI (`claude`) not found on PATH. Install it via "
            "`npm i -g @anthropic-ai/claude-code` or set the override via "
            "`ra_triage.agents.base.set_claude_cli_override(...)` in tests."
        )
    return [resolved]


# --- Result dataclass ----------------------------------------------------


@dataclass
class AgentResult:
    """Outcome of one agent subprocess run.

    ``completion_token`` is the FIRST token from the role's
    ``completion_tokens`` tuple observed in the human-readable stream (or
    ``None`` if none was seen — treated as a crash by the orchestrator).

    ``triage_md`` is the full TRIAGE.md contents after the run; empty
    string if the file doesn't exist (the agent never wrote one).
    """

    exit_code: int
    completion_token: str | None
    log_path: Path
    prompt_path: Path
    triage_md: str
    started_at: datetime
    ended_at: datetime


# --- Base class ----------------------------------------------------------


class ClaudeCodeAgent:
    """Base class for every RA Triage agent.

    Subclasses declare the role-specific configuration as class attrs; the
    :meth:`run` method is the only public entry point.

    Implementation notes:
      - ``cwd`` of the subprocess is the worktree path — this scopes
        ``Read``, ``Edit``, ``Write``, ``Bash(...)`` calls to that tree.
      - ``env`` inherits the parent's environment with ``CLAUDE_CODE_NONINTERACTIVE``
        forced on so the CLI doesn't block on prompts.
      - stderr is merged into stdout so we can stream both uniformly.
    """

    # Must be overridden by subclasses.
    role: ClassVar[str] = ""
    completion_tokens: ClassVar[tuple[str, ...]] = ()
    allowed_tools: ClassVar[list[str]] = []
    prompt_template: ClassVar[str] = ""

    # Sensible defaults; subclasses may override.
    max_turns: ClassVar[int] = 40
    timeout_seconds: ClassVar[int] = 1800  # 30 minutes (tech plan §16)

    def __init__(self) -> None:
        """Validate class-level config once at construction time."""
        if not self.role:
            raise ValueError(f"{type(self).__name__}.role must be non-empty")
        if not self.completion_tokens:
            raise ValueError(
                f"{type(self).__name__}.completion_tokens must be non-empty"
            )
        if not self.prompt_template:
            raise ValueError(
                f"{type(self).__name__}.prompt_template must be non-empty"
            )

    # -- public API -------------------------------------------------------

    async def run(
        self, bug: Any, worktree: Path, context: dict[str, Any]
    ) -> AgentResult:
        """Render the prompt, spawn Claude, stream its output, return the result.

        ``bug`` is duck-typed — only ``bug.id`` is used directly here. The
        caller is responsible for stamping ``agent_runs`` rows before and
        after this call.

        ``context`` must contain an ``attempt`` int; the base class uses
        it for the log/prompt file names. Subclasses pass domain-specific
        keys through to the Jinja template.
        """
        attempt = context.get("attempt")
        if not isinstance(attempt, int) or attempt < 1:
            raise ValueError(
                f"context['attempt'] must be a positive int; got {attempt!r}"
            )

        bug_id = int(bug.id)

        rendered_prompt = self._render_prompt(context)
        prompt_path = self._persist_prompt(bug_id, attempt, rendered_prompt)
        log_path = self._log_path(bug_id, attempt)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate any prior log for this (bug, role, attempt) tuple — the
        # orchestrator bumps ``attempt`` on retry so pre-existing content
        # would only be from an aborted sibling process.
        log_path.write_text("", encoding="utf-8")

        argv = self._build_argv(rendered_prompt)
        env = {**os.environ, "CLAUDE_CODE_NONINTERACTIVE": "1"}

        started_at = _utcnow()
        completion_token: str | None = None
        exit_code: int

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(worktree),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Failed to spawn agent subprocess: {exc}. argv[0]={argv[0]!r}"
            ) from exc

        try:
            completion_token = await asyncio.wait_for(
                self._stream_and_detect(proc, bug_id, attempt, log_path),
                timeout=self.timeout_seconds,
            )
            exit_code = await proc.wait()
        except TimeoutError:
            logger.warning(
                "Agent role=%s bug_id=%s attempt=%d exceeded %ds timeout; terminating",
                self.role,
                bug_id,
                attempt,
                self.timeout_seconds,
            )
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            exit_code = -1
            completion_token = None
            # Append a timeout marker to the log so the artifact explains
            # why there's no completion token.
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"\n[triage] TIMEOUT after {self.timeout_seconds}s; process terminated\n"
                )
        ended_at = _utcnow()

        triage_md = self._read_triage_md(worktree)

        return AgentResult(
            exit_code=exit_code,
            completion_token=completion_token,
            log_path=log_path,
            prompt_path=prompt_path,
            triage_md=triage_md,
            started_at=started_at,
            ended_at=ended_at,
        )

    # -- prompt / paths ---------------------------------------------------

    def _render_prompt(self, context: dict[str, Any]) -> str:
        """Render the Jinja template with ``context``."""
        template = _jinja_env.get_template(self.prompt_template)
        return template.render(**context)

    def _persist_prompt(self, bug_id: int, attempt: int, prompt: str) -> Path:
        """Write the rendered prompt to disk and return its path."""
        path = get_settings().prompts_dir / str(bug_id) / f"{self.role}.{attempt}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prompt, encoding="utf-8")
        return path

    def _log_path(self, bug_id: int, attempt: int) -> Path:
        return get_settings().logs_dir / str(bug_id) / f"{self.role}.{attempt}.log"

    # -- subprocess argv --------------------------------------------------

    def _build_argv(self, rendered_prompt: str) -> list[str]:
        """Build the exact argv the base class uses to spawn Claude."""
        base = _resolve_claude_argv()
        return [
            *base,
            "-p",
            rendered_prompt,
            "--output-format",
            "stream-json",
            "--allowed-tools",
            ",".join(self.allowed_tools),
            "--max-turns",
            str(self.max_turns),
            "--verbose",
            "--dangerously-skip-permissions",
        ]

    # -- streaming --------------------------------------------------------

    async def _stream_and_detect(
        self,
        proc: asyncio.subprocess.Process,
        bug_id: int,
        attempt: int,
        log_path: Path,
    ) -> str | None:
        """Drain the subprocess's stdout, writing to log + event bus.

        Returns the FIRST completion token observed, or ``None`` if the
        process exits without printing one (orchestrator treats as crash).
        """
        assert proc.stdout is not None

        detected: str | None = None
        # Open the log file for append; flush after each line so operators
        # tailing it see updates in real time.
        with log_path.open("a", encoding="utf-8", buffering=1) as log_fh:
            while True:
                try:
                    raw_line = await proc.stdout.readline()
                except asyncio.CancelledError:
                    raise
                if not raw_line:
                    # EOF — subprocess has finished writing.
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                if not line:
                    continue

                human = _to_human_line(line)

                # Persist raw + human-readable separately so replay works.
                log_fh.write(f"raw={line}\n")
                log_fh.write(f"{human}\n")
                log_fh.flush()

                # Fan out to WS subscribers.
                try:
                    publish_agent_stdout(
                        bug_id=bug_id,
                        role=self.role,
                        attempt=attempt,
                        text=human,
                    )
                except Exception:  # pragma: no cover - defensive
                    logger.exception(
                        "publish_agent_stdout failed for bug_id=%s role=%s",
                        bug_id,
                        self.role,
                    )

                # Completion-token scan — FIRST wins; don't short-circuit.
                if detected is None:
                    for tok in self.completion_tokens:
                        if tok in human:
                            detected = tok
                            break

        return detected

    # -- post-run ---------------------------------------------------------

    def _read_triage_md(self, worktree: Path) -> str:
        path = worktree / "TRIAGE.md"
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            logger.exception("Failed to read TRIAGE.md at %s", path)
            return ""


# --- Stream-JSON unwrap --------------------------------------------------


def _to_human_line(raw: str) -> str:
    """Convert one stream-json line to a compact human-readable summary.

    Falls back to the raw line if parsing fails. The shape of Claude
    Code's stream-json output is documented as each line being one JSON
    object — we extract the interesting fields without assuming a fully
    stable schema.
    """
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw

    if not isinstance(obj, dict):
        return raw

    kind = obj.get("type")
    if kind == "assistant":
        return _summarize_assistant(obj)
    if kind == "user":
        return _summarize_user(obj)
    if kind == "system":
        subtype = obj.get("subtype", "")
        return f"[system{':' + subtype if subtype else ''}]"
    if kind == "result":
        subtype = obj.get("subtype", "")
        text = obj.get("result") or ""
        return f"[result{':' + subtype if subtype else ''}] {text}".strip()

    # Unknown shape — keep the raw line but prefix with type hint.
    return f"[{kind}] {raw}" if kind else raw


def _summarize_assistant(obj: dict[str, Any]) -> str:
    """Extract a one-line summary from an assistant event."""
    message = obj.get("message") or {}
    if not isinstance(message, dict):
        return "[assistant]"
    content = message.get("content")
    if not isinstance(content, list):
        return "[assistant]"
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = str(block.get("text", "")).strip()
            if text:
                parts.append(text)
        elif btype == "tool_use":
            name = block.get("name", "tool")
            tool_input = block.get("input") or {}
            summary = _tool_input_summary(str(name), tool_input)
            parts.append(f"[tool_use:{name}] {summary}".rstrip())
    joined = " \n".join(p for p in parts if p)
    return joined or "[assistant]"


def _summarize_user(obj: dict[str, Any]) -> str:
    """Extract a one-line summary from a user/tool-result event."""
    message = obj.get("message") or {}
    if not isinstance(message, dict):
        return "[user]"
    content = message.get("content")
    if not isinstance(content, list):
        return "[user]"
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            tool_use_id = block.get("tool_use_id", "")
            is_error = bool(block.get("is_error"))
            tag = "tool_result_error" if is_error else "tool_result"
            inner = block.get("content")
            if isinstance(inner, list) and inner:
                # Join textual pieces only; avoid dumping giant blobs.
                snippets: list[str] = []
                for b in inner:
                    if isinstance(b, dict) and b.get("type") == "text":
                        snippets.append(str(b.get("text", "")))
                joined = " ".join(snippets).strip()
                return f"[{tag}:{tool_use_id}] {joined[:500]}".rstrip()
            return f"[{tag}:{tool_use_id}]"
    return "[user]"


def _tool_input_summary(name: str, tool_input: dict[str, Any]) -> str:
    """Collapse a tool_use input dict into a short suffix for the log."""
    if not isinstance(tool_input, dict):
        return ""
    if name in {"Read", "Glob"}:
        path = tool_input.get("file_path") or tool_input.get("path") or ""
        return str(path)
    if name == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"{pattern!r} in {path}".strip()
    if name == "Bash":
        cmd = tool_input.get("command", "")
        return str(cmd)[:200]
    if name in {"Edit", "Write"}:
        path = tool_input.get("file_path", "")
        return str(path)
    # Fallback: compact JSON with length cap
    try:
        raw = json.dumps(tool_input, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
    return raw[:200]


# --- Utility -------------------------------------------------------------


def _utcnow() -> datetime:
    """Naive UTC now — matches ``db.repo._utcnow`` for storage consistency."""
    return datetime.now(UTC).replace(tzinfo=None)


__all__ = [
    "AgentResult",
    "ClaudeCodeAgent",
    "set_claude_cli_override",
]
