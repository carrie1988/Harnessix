"""补丁导出命令在真实Git仓库上执行：包含修改与新文件，不改动工作区的Git索引。"""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

from integrations.harbor.patch_export import head_command, parse_head, patch_command


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=e@x", "-c", "user.name=e", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_patch_includes_new_files_and_leaves_the_index_alone(tmp_path: Path) -> None:
    repo, logs = tmp_path / "repo", tmp_path / "logs"
    repo.mkdir()
    logs.mkdir()
    git(repo, "init", "-q")
    (repo / "a.py").write_text("a = 1\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-qm", "base")
    base = parse_head(
        subprocess.run(head_command(str(repo)), shell=True, capture_output=True, text=True).stdout
    )
    assert base is not None
    (repo / "a.py").write_text("a = 2\n")
    (repo / "new.py").write_text("b = 1\n")
    index_before = (repo / ".git/index").read_bytes()
    command = patch_command(str(repo), base, PurePosixPath(str(logs)))
    subprocess.run(["bash", "-c", command], check=True)
    patch = (logs / "patch.diff").read_text()
    assert "+a = 2" in patch and "new file mode" in patch and "+b = 1" in patch
    assert (repo / ".git/index").read_bytes() == index_before
    assert git(repo, "status", "--short").splitlines() == [" M a.py", "?? new.py"]
