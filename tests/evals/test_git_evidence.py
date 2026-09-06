from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from harnessix.evals.git_evidence import collect_git_evidence


def git_executable() -> Path:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git 不可用")
    return Path(executable).resolve()


def command(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        [str(git_executable()), *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    return result.stdout.strip()


async def test_collects_real_git_paths_index_types_and_full_diff_digest(tmp_path: Path) -> None:
    command(tmp_path, "init", "-q")
    command(tmp_path, "config", "user.name", "Harnessix Eval")
    command(tmp_path, "config", "user.email", "eval@harnessix.invalid")
    (tmp_path / "a.py").write_text("before\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("before\n", encoding="utf-8")
    command(tmp_path, "add", "a.py", "b.py")
    command(tmp_path, "commit", "-qm", "baseline")
    baseline = command(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "a.py").write_text("after\n", encoding="utf-8")
    command(tmp_path, "mv", "b.py", "renamed.py")
    command(tmp_path, "add", "renamed.py")
    (tmp_path / "new.py").write_text("new\n", encoding="utf-8")

    evidence = await collect_git_evidence(
        tmp_path,
        git_executable(),
        baseline_revision=baseline,
        baseline_tree_sha256="d" * 64,
    )

    assert evidence.head_revision == evidence.baseline_revision == baseline
    assert evidence.changed_paths == ("a.py", "b.py", "new.py", "renamed.py")
    assert evidence.staged_paths == ("b.py", "renamed.py")
    assert evidence.untracked_paths == ("new.py",)
    assert evidence.unsupported_change_paths == ("b.py", "new.py", "renamed.py")
    assert len(evidence.status_sha256) == len(evidence.diff_sha256) == 64
    assert evidence.diff_observed_bytes > 0
