"""Unit tests for ``integrations.github``.

Strategy: install a stub subprocess runner via ``github.set_runner`` that
records every argv and returns canned stdout/stderr/returncode. We assert
the exact argv assembled for each gh subcommand plus the push-before-create
flow, then exercise failure paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from ra_triage.config.registry import RepoConfig
from ra_triage.integrations import github as github_module
from ra_triage.integrations.github import GitHubError


@dataclass
class _FakeResult:
    returncode: int
    stdout: str
    stderr: str


class _Recorder:
    """Records argv lists and returns scripted responses in order.

    Return type is annotated ``Any`` so duck-typing works: the real runner's
    return type is the module-private ``_SubprocessResult`` dataclass, but
    :class:`_FakeResult` has the same field shape and that's all the
    callers inspect.
    """

    def __init__(self, responses: list[_FakeResult]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[str], Path, float]] = []

    async def __call__(
        self, argv: list[str], *, cwd: Path, timeout: float
    ) -> Any:
        self.calls.append((list(argv), cwd, timeout))
        if not self.responses:
            raise AssertionError(
                f"No scripted response for runner call: argv={argv!r}"
            )
        return self.responses.pop(0)


def _install_runner(recorder: _Recorder) -> None:
    """Install a recorder via the module's ``set_runner`` with a cast.

    The module's ``_SubprocessRunner`` Protocol is parameterized over a
    private dataclass, so the type-checker treats our duck-compatible
    ``_Recorder`` as incompatible. ``cast`` here is safe — the protocol is
    structural and our recorder satisfies the callable shape.
    """
    github_module.set_runner(cast(Any, recorder))


@pytest.fixture()
def reset_runner() -> Any:
    """Ensure the module-level runner is restored after each test."""
    github_module.set_runner(None)
    yield
    github_module.set_runner(None)


def _avm_backend_repo() -> RepoConfig:
    return RepoConfig(
        name="avm-backend",
        path=Path("/tmp/does-not-exist"),
        main_branch="main",
        branch_prefix="triage/",
        dev_command="x",
        build_command="x",
        test_command="x",
        dev_port=8080,
        test_launch_port=18080,
        integration_port=28080,
        env_file=".env",
        role="backend",
        github_origin="github.com/megresaut/avm-backend",
    )


# --- gh_pr_create -----------------------------------------------------------


async def test_gh_pr_create_pushes_then_creates_and_parses_url(
    tmp_path: Path, reset_runner: None
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    body = worktree / "TRIAGE.md"
    body.write_text("# Test body\n", encoding="utf-8")

    recorder = _Recorder(
        [
            # git push response
            _FakeResult(returncode=0, stdout="", stderr="Branch 'triage/42' set up"),
            # gh pr create response
            _FakeResult(
                returncode=0,
                stdout="https://github.com/megresaut/avm-backend/pull/123\n",
                stderr="",
            ),
        ]
    )
    _install_runner(recorder)

    info = await github_module.gh_pr_create(
        worktree=worktree,
        repo=_avm_backend_repo(),
        bug_id=42,
        title="fix(bug-42): broken thing",
        body_path=body,
    )

    # Two calls: push, then create.
    assert len(recorder.calls) == 2
    push_argv, push_cwd, _ = recorder.calls[0]
    assert push_argv == ["git", "push", "-u", "origin", "triage/42"]
    assert push_cwd == worktree

    create_argv, create_cwd, _ = recorder.calls[1]
    assert create_argv[:3] == ["gh", "pr", "create"]
    assert "--title" in create_argv
    assert create_argv[create_argv.index("--title") + 1] == (
        "fix(bug-42): broken thing"
    )
    assert "--body-file" in create_argv
    assert create_argv[create_argv.index("--body-file") + 1] == str(body)
    assert "--base" in create_argv
    assert create_argv[create_argv.index("--base") + 1] == "main"
    assert "--head" in create_argv
    assert create_argv[create_argv.index("--head") + 1] == "triage/42"
    assert create_cwd == worktree

    assert info.repo_name == "avm-backend"
    assert info.branch == "triage/42"
    assert info.url == "https://github.com/megresaut/avm-backend/pull/123"
    assert info.number == 123
    assert info.status == "open"


async def test_gh_pr_create_raises_when_push_fails(
    tmp_path: Path, reset_runner: None
) -> None:
    body = tmp_path / "TRIAGE.md"
    body.write_text("x", encoding="utf-8")
    recorder = _Recorder(
        [_FakeResult(returncode=128, stdout="", stderr="fatal: no upstream")]
    )
    _install_runner(recorder)

    with pytest.raises(GitHubError) as exc_info:
        await github_module.gh_pr_create(
            worktree=tmp_path,
            repo=_avm_backend_repo(),
            bug_id=42,
            title="fix(bug-42): x",
            body_path=body,
        )
    assert "git push" in str(exc_info.value)
    assert "no upstream" in str(exc_info.value)


async def test_gh_pr_create_raises_when_body_missing(
    tmp_path: Path, reset_runner: None
) -> None:
    """If TRIAGE.md doesn't exist yet, `gh_pr_create` refuses early."""
    with pytest.raises(GitHubError) as exc_info:
        await github_module.gh_pr_create(
            worktree=tmp_path,
            repo=_avm_backend_repo(),
            bug_id=42,
            title="fix(bug-42): x",
            body_path=tmp_path / "missing.md",
        )
    assert "does not exist" in str(exc_info.value)


async def test_gh_pr_create_raises_on_nonzero_exit(
    tmp_path: Path, reset_runner: None
) -> None:
    body = tmp_path / "TRIAGE.md"
    body.write_text("x", encoding="utf-8")
    recorder = _Recorder(
        [
            _FakeResult(returncode=0, stdout="", stderr=""),
            _FakeResult(returncode=1, stdout="", stderr="pr with this head already exists"),
        ]
    )
    _install_runner(recorder)

    with pytest.raises(GitHubError) as exc_info:
        await github_module.gh_pr_create(
            worktree=tmp_path,
            repo=_avm_backend_repo(),
            bug_id=42,
            title="fix(bug-42): x",
            body_path=body,
        )
    assert "pr with this head already exists" in str(exc_info.value)


async def test_gh_pr_create_raises_when_no_url_in_output(
    tmp_path: Path, reset_runner: None
) -> None:
    body = tmp_path / "TRIAGE.md"
    body.write_text("x", encoding="utf-8")
    recorder = _Recorder(
        [
            _FakeResult(returncode=0, stdout="", stderr=""),
            _FakeResult(returncode=0, stdout="(no url here)", stderr=""),
        ]
    )
    _install_runner(recorder)

    with pytest.raises(GitHubError) as exc_info:
        await github_module.gh_pr_create(
            worktree=tmp_path,
            repo=_avm_backend_repo(),
            bug_id=42,
            title="fix(bug-42): x",
            body_path=body,
        )
    assert "no PR URL" in str(exc_info.value)


async def test_gh_pr_create_with_cross_ref_note_writes_temp_body(
    tmp_path: Path, reset_runner: None
) -> None:
    """Cross-repo case: body file points at a concatenated temp file."""
    body = tmp_path / "TRIAGE.md"
    body.write_text("## Root Cause\n\nX\n", encoding="utf-8")

    recorder = _Recorder(
        [
            _FakeResult(returncode=0, stdout="", stderr=""),
            _FakeResult(
                returncode=0,
                stdout="https://github.com/megresaut/avm-backend/pull/999",
                stderr="",
            ),
        ]
    )
    _install_runner(recorder)

    await github_module.gh_pr_create(
        worktree=tmp_path,
        repo=_avm_backend_repo(),
        bug_id=42,
        title="fix(bug-42): x",
        body_path=body,
        cross_ref_body_note=(
            "## Companion PR\n\n"
            "Frontend: https://github.com/megresaut/builder_frontend/pull/5"
        ),
    )

    # The body-file argument should NOT equal the original TRIAGE.md path
    # — it should point at a temp file that was concatenated.
    create_argv = recorder.calls[1][0]
    body_file_arg = create_argv[create_argv.index("--body-file") + 1]
    assert body_file_arg != str(body)
    # The concatenation temp file is cleaned up on return; there's
    # nothing stable to assert against its path, but the argv is enough.


# --- gh_pr_merge ------------------------------------------------------------


async def test_gh_pr_merge_default_flags(
    tmp_path: Path, reset_runner: None
) -> None:
    recorder = _Recorder([_FakeResult(returncode=0, stdout="", stderr="")])
    _install_runner(recorder)

    await github_module.gh_pr_merge(worktree=tmp_path, pr_number=7)

    assert len(recorder.calls) == 1
    argv, cwd, _ = recorder.calls[0]
    assert argv == ["gh", "pr", "merge", "7", "--squash", "--delete-branch"]
    assert cwd == tmp_path


async def test_gh_pr_merge_without_squash_or_delete(
    tmp_path: Path, reset_runner: None
) -> None:
    recorder = _Recorder([_FakeResult(returncode=0, stdout="", stderr="")])
    _install_runner(recorder)

    await github_module.gh_pr_merge(
        worktree=tmp_path, pr_number=7, squash=False, delete_branch=False
    )
    assert recorder.calls[0][0] == ["gh", "pr", "merge", "7"]


async def test_gh_pr_merge_raises_on_failure(
    tmp_path: Path, reset_runner: None
) -> None:
    recorder = _Recorder(
        [_FakeResult(returncode=1, stdout="", stderr="required checks failing")]
    )
    _install_runner(recorder)
    with pytest.raises(GitHubError) as exc_info:
        await github_module.gh_pr_merge(worktree=tmp_path, pr_number=7)
    assert "required checks failing" in str(exc_info.value)


# --- gh_pr_status -----------------------------------------------------------


async def test_gh_pr_status_parses_merged(
    tmp_path: Path, reset_runner: None
) -> None:
    recorder = _Recorder(
        [
            _FakeResult(
                returncode=0,
                stdout='{"state":"MERGED","mergedAt":"2026-04-17T00:00:00Z"}',
                stderr="",
            )
        ]
    )
    _install_runner(recorder)

    status = await github_module.gh_pr_status(worktree=tmp_path, pr_number=7)
    assert status == "merged"
    assert recorder.calls[0][0] == [
        "gh",
        "pr",
        "view",
        "7",
        "--json",
        "state,mergedAt",
    ]


async def test_gh_pr_status_parses_open(
    tmp_path: Path, reset_runner: None
) -> None:
    recorder = _Recorder(
        [_FakeResult(returncode=0, stdout='{"state":"OPEN","mergedAt":null}', stderr="")]
    )
    _install_runner(recorder)
    status = await github_module.gh_pr_status(worktree=tmp_path, pr_number=7)
    assert status == "open"


async def test_gh_pr_status_parses_closed(
    tmp_path: Path, reset_runner: None
) -> None:
    recorder = _Recorder(
        [_FakeResult(returncode=0, stdout='{"state":"CLOSED","mergedAt":null}', stderr="")]
    )
    _install_runner(recorder)
    status = await github_module.gh_pr_status(worktree=tmp_path, pr_number=7)
    assert status == "closed"


async def test_gh_pr_status_raises_on_bad_json(
    tmp_path: Path, reset_runner: None
) -> None:
    recorder = _Recorder(
        [_FakeResult(returncode=0, stdout="not json", stderr="")]
    )
    _install_runner(recorder)
    with pytest.raises(GitHubError):
        await github_module.gh_pr_status(worktree=tmp_path, pr_number=7)


async def test_gh_pr_status_raises_on_unknown_state(
    tmp_path: Path, reset_runner: None
) -> None:
    recorder = _Recorder(
        [_FakeResult(returncode=0, stdout='{"state":"DRAFT"}', stderr="")]
    )
    _install_runner(recorder)
    with pytest.raises(GitHubError):
        await github_module.gh_pr_status(worktree=tmp_path, pr_number=7)
