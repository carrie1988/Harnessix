from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.evals.swebench.source_revision import require_harnessix_execution_revision

GIT = Path(shutil.which("git") or "")


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        (str(GIT), *arguments),
        cwd=root,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
    )
    return completed.stdout.decode().strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "source"
    (root / "src" / "harnessix").mkdir(parents=True)
    (root / "src" / "harnessix" / "runtime.py").write_text("VERSION = 1\n")
    (root / "pyproject.toml").write_text("[project]\nname = 'test'\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "implementation")
    return root, _git(root, "rev-parse", "HEAD")


def test_revision_accepts_later_document_only_commit(tmp_path: Path) -> None:
    root, revision = _repository(tmp_path)
    (root / "README.md").write_text("plan\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "plan")

    require_harnessix_execution_revision(root, GIT, revision)


def test_revision_rejects_uncommitted_execution_change(tmp_path: Path) -> None:
    root, revision = _repository(tmp_path)
    (root / "src" / "harnessix" / "runtime.py").write_text("VERSION = 2\n")

    with pytest.raises(KernelError, match="未提交变更"):
        require_harnessix_execution_revision(root, GIT, revision)


def test_revision_rejects_committed_execution_drift(tmp_path: Path) -> None:
    root, revision = _repository(tmp_path)
    (root / "src" / "harnessix" / "runtime.py").write_text("VERSION = 2\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "drift")

    with pytest.raises(KernelError, match="偏离"):
        require_harnessix_execution_revision(root, GIT, revision)
