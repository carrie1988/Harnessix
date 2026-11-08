"""补丁导出命令在真实Git仓库上执行：只含智能体开始之后的改动，不改动工作区和它的Git索引。"""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

from integrations.harbor.patch_export import (
    parse_object_id,
    patch_command,
    workspace_tree_command,
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=e@x", "-c", "user.name=e", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def bash(command: str) -> str:
    result = subprocess.run(["bash", "-c", command], check=True, capture_output=True, text=True)
    return result.stdout


def committed_repo(tmp_path: Path, files: dict[str, str], gitignore: str = "") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    for name, body in files.items():
        (repo / name).write_text(body)
    if gitignore:
        (repo / ".gitignore").write_text(gitignore)
    git(repo, "add", "-A", "-f")
    git(repo, "commit", "-qm", "base")
    return repo


def snapshot(repo: Path) -> str:
    base = parse_object_id(bash(workspace_tree_command(str(repo))))
    assert base is not None
    return base


def export(repo: Path, base: str, logs: Path) -> str:
    logs.mkdir()
    bash(patch_command(str(repo), base, PurePosixPath(str(logs))))
    return (logs / "patch.diff").read_text()


def test_patch_includes_new_files_and_leaves_the_index_alone(tmp_path: Path) -> None:
    repo = committed_repo(tmp_path, {"a.py": "a = 1\n"})
    base = snapshot(repo)
    (repo / "a.py").write_text("a = 2\n")
    (repo / "new.py").write_text("b = 1\n")
    index_before = (repo / ".git/index").read_bytes()
    patch = export(repo, base, tmp_path / "logs")
    assert "+a = 2" in patch and "new file mode" in patch and "+b = 1" in patch
    assert (repo / ".git/index").read_bytes() == index_before
    assert git(repo, "status", "--short").splitlines() == [" M a.py", "?? new.py"]


def test_changes_present_before_the_agent_started_are_not_in_the_patch(tmp_path: Path) -> None:
    """镜像自带的未跟踪构建产物与已修改的依赖清单不是智能体的改动（gin-4003、preact-2927）。"""
    repo = committed_repo(tmp_path, {"lock.json": "v1\n", "src.go": "x\n"})
    (repo / "pkg.test").write_bytes(b"\x7fELF" + bytes(4096))
    (repo / "lock.json").write_text("v1-local\n")
    base = snapshot(repo)
    (repo / "src.go").write_text("y\n")
    patch = export(repo, base, tmp_path / "logs")
    assert "src.go" in patch and "+y" in patch
    assert "pkg.test" not in patch and "lock.json" not in patch


def test_agent_edits_to_preexisting_changes_are_relative_to_the_start(tmp_path: Path) -> None:
    repo = committed_repo(tmp_path, {"lock.json": "v1\n"})
    (repo / "lock.json").write_text("v1-local\n")
    base = snapshot(repo)
    (repo / "lock.json").write_text("v1-local\nagent\n")
    patch = export(repo, base, tmp_path / "logs")
    assert "-v1\n" not in patch and " v1-local" in patch and "+agent" in patch


def test_tracked_files_matching_gitignore_are_not_reported_deleted(tmp_path: Path) -> None:
    """astropy-7166：已跟踪却匹配.gitignore的C源文件，空白临时索引会把它们导出成删除。"""
    repo = committed_repo(tmp_path, {"gen.c": "int a;\n", "mod.py": "a = 1\n"}, gitignore="*.c\n")
    base = snapshot(repo)
    (repo / "mod.py").write_text("a = 2\n")
    (repo / "build.c").write_text("ignored\n")
    patch = export(repo, base, tmp_path / "logs")
    assert "deleted file" not in patch and "gen.c" not in patch
    assert "+a = 2" in patch and "build.c" not in patch
    (repo / "gen.c").write_text("int b;\n")
    assert "+int b;" in export(repo, base, tmp_path / "logs2")


def test_patch_command_requires_a_tree_id() -> None:
    with pytest.raises(ValueError):
        patch_command("/testbed", "HEAD; rm -rf /", PurePosixPath("/logs/agent"))
