"""Git只读查询按用户实际生效的换行设置比较工作区（Git for Windows默认core.autocrlf=true）。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.tools import git as git_tools
from harnessix.tools import git_line_endings as line_endings_module
from harnessix.tools.git import GitReadRuntime
from harnessix.tools.git_contracts import GitDiffInput, GitDiffOutput, GitStatusInput
from harnessix.tools.git_line_endings import (
    global_config_path,
    line_ending_arguments,
    probe_environment,
)
from tests.tools.test_git import _git

LF = b"first\nsecond\nthird\n"
CRLF = LF.replace(b"\n", b"\r\n")


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (b"", ()),
        (b"core.autocrlf\ntrue\0", ("-c", "core.autocrlf=true")),
        # 系统级在前、用户级在后：后出现的同名键生效。
        (b"core.autocrlf\ntrue\0core.autocrlf\ninput\0", ("-c", "core.autocrlf=input")),
        (
            b"core.autocrlf\nYes\0core.eol\nCRLF\0",
            ("-c", "core.autocrlf=true", "-c", "core.eol=crlf"),
        ),
        (b"core.autocrlf\noff\0", ("-c", "core.autocrlf=false")),
        # 只写键名的布尔项按真值处理。
        (b"core.autocrlf\0", ("-c", "core.autocrlf=true")),
        # Git不认可的取值按未设置处理。
        (b"core.autocrlf\nmaybe\0core.eol\nmac\0", ()),
        (b"core.eol\nnative\0", ("-c", "core.eol=native")),
        # 探测为保证退出码为0另匹配的键，不产生参数。
        (b"core.repositoryformatversion\n0\0", ()),
    ],
)
def test_the_effective_values_become_command_line_settings(
    output: bytes, expected: tuple[str, ...]
) -> None:
    assert line_ending_arguments(output) == expected


def test_the_global_config_is_found_like_git_finds_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    assert global_config_path(windows=False, source={"HOME": str(home)}) is None
    xdg = home / ".config" / "git" / "config"
    xdg.write_text("[core]\n\tautocrlf = input\n", encoding="utf-8")
    assert global_config_path(windows=False, source={"HOME": str(home)}) == xdg
    dotfile = home / ".gitconfig"
    dotfile.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")
    assert global_config_path(windows=False, source={"HOME": str(home)}) == dotfile
    # Git for Windows没有HOME时依次看HOMEDRIVE+HOMEPATH与USERPROFILE。
    drive, rest = str(home)[:2], str(home)[2:]
    windows = {"HOMEDRIVE": drive, "HOMEPATH": rest, "USERPROFILE": "/nonexistent"}
    if Path(drive + rest).is_dir():
        assert global_config_path(windows=True, source=windows) == dotfile
    assert global_config_path(windows=True, source={"USERPROFILE": str(home)}) == dotfile
    assert global_config_path(windows=False, source={}) in (None, Path.home() / ".gitconfig")


def test_the_probe_sees_system_and_user_config_but_nothing_else(tmp_path: Path) -> None:
    isolated = dict(git_tools._ENVIRONMENT)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")
    environment = probe_environment(isolated, windows=False, source={"HOME": str(home)})
    assert "GIT_CONFIG_NOSYSTEM" not in environment
    assert environment["GIT_CONFIG_GLOBAL"] == str(home / ".gitconfig")
    assert {
        name: environment[name]
        for name in isolated
        if name not in {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"}
    } == {
        name: value
        for name, value in isolated.items()
        if name not in {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"}
    }
    # 没有用户级配置文件时仍指向空设备，不传HOME。
    bare = probe_environment(isolated, windows=False, source={"HOME": str(tmp_path)})
    assert bare["GIT_CONFIG_GLOBAL"] == isolated["GIT_CONFIG_GLOBAL"]
    assert "HOME" not in bare


def _isolated_git(root: Path, *arguments: str, autocrlf: str = "false") -> None:
    """测试自己的git命令：不读任何系统或用户配置，换行转换由参数明确指定。"""
    subprocess.run(
        [str(_git()), "-c", f"core.autocrlf={autocrlf}", *arguments],
        cwd=root,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
    )


def checked_out_with_crlf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user: str) -> Path:
    """对象是LF、工作区是CRLF：用core.autocrlf=true检出，与Git for Windows用户的仓库相同。

    检出后改动文件时间：索引里的时间与大小记录不再匹配时Git要比较内容，这正是会话中文件被
    编辑器或工具重写后的状态。用户级配置写明core.autocrlf，结果不受测试机系统级配置影响。
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(f"[core]\n\tautocrlf = {user}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    _isolated_git(root, "init", "-q")
    (root / "notes.txt").write_bytes(LF)
    _isolated_git(root, "add", "notes.txt")
    _isolated_git(
        root,
        "-c",
        "user.name=Harnessix Test",
        "-c",
        "user.email=t@h.invalid",
        "commit",
        "-qm",
        "base",
    )
    notes = root / "notes.txt"
    notes.unlink()
    _isolated_git(root, "checkout", "--", "notes.txt", autocrlf="true")
    assert notes.read_bytes() == CRLF
    later = notes.stat().st_mtime + 100
    os.utime(notes, (later, later))
    return root


def reader(root: Path, tmp_path: Path) -> GitReadRuntime:
    return GitReadRuntime(root, _git(), state_directory=tmp_path / "state")


async def test_crlf_checkout_is_clean_when_the_user_converts_line_endings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = checked_out_with_crlf(tmp_path, monkeypatch, "true")
    git = reader(root, tmp_path)
    status = await git.execute(GitStatusInput(), CancelToken())
    assert status.entries == ()
    assert (await git.workspace_changes(CancelToken())).entries == ()
    # 真正改动的那一行才出现在差异里，不是整个文件。
    (root / "notes.txt").write_bytes(CRLF.replace(b"second", b"SECOND"))
    diff = await git.execute(GitDiffInput(target="worktree", context_lines=0), CancelToken())
    assert isinstance(diff, GitDiffOutput)
    changed = [line for line in diff.text.splitlines() if line[:1] in "+-"]
    assert [line for line in changed if not line.startswith(("+++", "---"))] == [
        "-second",
        "+SECOND",
    ]


async def test_without_conversion_the_whole_file_differs_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = checked_out_with_crlf(tmp_path, monkeypatch, "false")
    git = reader(root, tmp_path)
    status = await git.execute(GitStatusInput(), CancelToken())
    assert [(entry.path, entry.kind) for entry in status.entries] == [("notes.txt", "ordinary")]
    diff = await git.execute(GitDiffInput(target="worktree", context_lines=0), CancelToken())
    removed = [
        line
        for line in diff.text.splitlines()
        if line.startswith("-") and line != "--- a/notes.txt"
    ]
    assert removed == ["-first", "-second", "-third"]


async def test_the_repository_setting_overrides_the_user_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = checked_out_with_crlf(tmp_path, monkeypatch, "true")
    _isolated_git(root, "config", "core.autocrlf", "false")
    status = await reader(root, tmp_path).execute(GitStatusInput(), CancelToken())
    assert [entry.path for entry in status.entries] == ["notes.txt"]


async def test_the_probe_runs_once_and_retries_after_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = checked_out_with_crlf(tmp_path, monkeypatch, "true")
    git = reader(root, tmp_path)
    real = line_endings_module.probe_line_endings
    outcomes: list[tuple[str, ...] | None] = []

    async def failing_once(*args, **kwargs):
        if not outcomes:
            outcomes.append(None)
            return None
        result = await real(*args, **kwargs)
        outcomes.append(result)
        return result

    monkeypatch.setattr(line_endings_module, "probe_line_endings", failing_once)
    # 探测不成立：本次按隔离读取的原行为（整个文件都改了），下次再探测。
    first = await git.execute(GitStatusInput(), CancelToken())
    assert [entry.path for entry in first.entries] == ["notes.txt"]
    for _ in range(3):
        assert (await git.execute(GitStatusInput(), CancelToken())).entries == ()
    assert outcomes == [None, ("-c", "core.autocrlf=true")]
