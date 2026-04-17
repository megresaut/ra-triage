"""Tests for ``ra_triage.orchestrator.tester_safety``.

Two layers:

  1. Pure-function ``is_test_path`` classifier — AVM-like paths are
     enumerated with the expected verdict; the glob engine is exercised
     across Go, JS/TS, and Python conventions.
  2. End-to-end ``check_and_revert_prod_changes`` against a real git
     repo. We build a tiny fixture repo in ``tmp_path``, commit a baseline,
     mutate both a test file and a prod file, and assert the prod
     modification is reverted while the test-file change is kept.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from ra_triage.orchestrator.tester_safety import (
    check_and_revert_prod_changes,
    is_test_path,
)

# --- pure classifier ----------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        # Test-file paths (Go)
        ("api/handler_test.go", True),
        ("api/internal/cart_test.go", True),
        # Prod-file paths (Go)
        ("api/handler.go", False),
        ("api/cmd/server/main.go", False),
        ("api/internal/cart.go", False),
        # Test-file paths (JS/TS)
        ("src/components/Button.test.tsx", True),
        ("src/lib/api.test.ts", True),
        ("src/__tests__/App.test.jsx", True),
        ("src/components/Form.spec.tsx", True),
        ("src/lib/utils.spec.js", True),
        # Prod-file paths (TS/TSX)
        ("src/components/Button.tsx", False),
        ("src/lib/api.ts", False),
        ("src/App.tsx", False),
        # Directory-based test locations
        ("api/tests/integration/user_test.go", True),
        ("client/test/setup.ts", True),
        ("pkg/foo/tests/fixture.go", True),
        # Python
        ("tests/unit/test_thing.py", True),
        ("tests/test_thing.py", True),
        ("src/test_thing.py", True),
        ("src/thing_test.py", True),
        ("src/thing.py", False),
        # TRIAGE.md always allowed
        ("TRIAGE.md", True),
        # README-like prod docs — NOT in allow-list
        ("README.md", False),
        ("docs/architecture.md", False),
        # Edge: file that has "tests" in its NAME but not directory
        ("src/testable_api.go", False),
    ],
)
def test_is_test_path(path: str, expected: bool) -> None:
    assert is_test_path(path) is expected, (
        f"{path!r} expected {expected}, got {is_test_path(path)}"
    )


# --- git fixture + end-to-end revert ------------------------------------


def _run(args: list[str], cwd: Path) -> None:
    """Run a blocking subprocess in ``cwd``, raising on non-zero exit."""
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True)


def _init_repo(path: Path) -> None:
    """Initialize a git repo with one commit at HEAD containing baseline files."""
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], cwd=path)
    # Minimal author config; required for `git commit`.
    _run(["git", "config", "user.email", "triage-test@example.com"], cwd=path)
    _run(["git", "config", "user.name", "Triage Test"], cwd=path)
    # Ensure default branch name is ``main`` so tests match the registry.
    _run(["git", "checkout", "-q", "-b", "main"], cwd=path)

    (path / "src").mkdir()
    (path / "src" / "handler.go").write_text(
        "package main\nfunc Handler() {}\n", encoding="utf-8"
    )
    (path / "src" / "handler_test.go").write_text(
        "package main\nimport \"testing\"\nfunc TestHandler(t *testing.T) {}\n",
        encoding="utf-8",
    )
    (path / "TRIAGE.md").write_text("# initial\n", encoding="utf-8")

    _run(["git", "add", "."], cwd=path)
    _run(["git", "commit", "-q", "-m", "baseline"], cwd=path)


async def _check(
    worktree: Path, *, baseline: dict[str, str] | None = None
):
    return await check_and_revert_prod_changes(worktree, baseline=baseline)


def test_safety_check_reverts_prod_modification_keeps_test_additions(
    tmp_path: Path,
) -> None:
    """Tester mutated both a test file and a prod file → prod reverted, test kept."""
    repo = tmp_path / "wt"
    _init_repo(repo)

    # Simulate Tester edits.
    (repo / "src" / "handler.go").write_text(
        "package main\nfunc Handler() { /* evil prod edit */ }\n",
        encoding="utf-8",
    )
    # Append to existing test file (allowed).
    (repo / "src" / "handler_test.go").write_text(
        "package main\nimport \"testing\"\nfunc TestHandler(t *testing.T) {}\n"
        "func TestRegression(t *testing.T) { /* new */ }\n",
        encoding="utf-8",
    )

    result = asyncio.run(_check(repo))

    assert result.had_violation is True
    assert "src/handler.go" in result.reverted_paths
    # Test file is preserved — not in reverted_paths.
    assert "src/handler_test.go" in result.test_paths
    assert "src/handler_test.go" not in result.reverted_paths

    # Verify on disk.
    assert (
        "evil prod edit"
        not in (repo / "src" / "handler.go").read_text(encoding="utf-8")
    )
    assert (
        "TestRegression"
        in (repo / "src" / "handler_test.go").read_text(encoding="utf-8")
    )


def test_safety_check_revert_deletes_untracked_prod_files(tmp_path: Path) -> None:
    """A new (untracked) prod file is removed outright — can't ``git checkout`` it."""
    repo = tmp_path / "wt"
    _init_repo(repo)

    (repo / "src" / "new_prod.go").write_text(
        "package main\nfunc New() {}\n", encoding="utf-8"
    )

    result = asyncio.run(_check(repo))

    assert result.had_violation is True
    assert "src/new_prod.go" in result.reverted_paths
    assert not (repo / "src" / "new_prod.go").exists()


def test_safety_check_no_violation_for_test_files_and_triage_md(tmp_path: Path) -> None:
    """Only TRIAGE.md + test files touched → no violation, nothing reverted."""
    repo = tmp_path / "wt"
    _init_repo(repo)

    # Modify only allow-listed files.
    (repo / "TRIAGE.md").write_text("# Updated by Tester\n", encoding="utf-8")
    new_test = repo / "tests"
    new_test.mkdir()
    (new_test / "added_test.go").write_text(
        "package tests\nimport \"testing\"\n"
        "func TestAdded(t *testing.T) {}\n",
        encoding="utf-8",
    )

    result = asyncio.run(_check(repo))

    assert result.had_violation is False
    assert result.reverted_paths == []
    assert "TRIAGE.md" in result.test_paths
    assert "tests/added_test.go" in result.test_paths


def test_safety_check_no_changes_at_all(tmp_path: Path) -> None:
    """No edits made → had_violation False, empty lists."""
    repo = tmp_path / "wt"
    _init_repo(repo)

    result = asyncio.run(_check(repo))

    assert result.had_violation is False
    assert result.changed_paths == []
    assert result.reverted_paths == []
    assert result.test_paths == []
