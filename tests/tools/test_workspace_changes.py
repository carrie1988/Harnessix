"""会话基线的观察端口：与git_status同一Git只读端口，可只看指定路径；读取失败沿用读工具错误。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.tools import git as git_tools
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.runtime import CodingToolRuntime
from tests.tools.test_files import execute
from tests.tools.test_git import _command, _git, _repository


def bound_tools(workspace: Path, state: Path) -> CodingToolRuntime:
    """带Git绑定的读工具；Windows的Git读取要求工作区之外的私有状态目录，POSIX忽略它。"""
    return CodingToolRuntime(workspace, git_executable=_git(), git_state_directory=state)


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _repository(root)
    return root


async def test_changes_match_the_git_status_tool(tmp_path: Path) -> None:
    root = repository(tmp_path)
    (root / "tracked.py").write_text("after\n", encoding="utf-8")
    (root / "notes.txt").write_text("scratch\n", encoding="utf-8")
    async with bound_tools(root, tmp_path / "state") as tools:
        observed = await tools.workspace_changes(CancelToken())
        shown = await execute(tools, "git_status", limit=200)
    assert observed is not None
    assert observed.model_dump(mode="json") == shown.output
    assert [(entry.path, entry.kind) for entry in observed.entries] == [
        ("tracked.py", "ordinary"),
        ("notes.txt", "untracked"),
    ]


async def test_paths_limit_the_observation_to_literal_paths(tmp_path: Path) -> None:
    root = repository(tmp_path)
    (root / "tracked.py").write_text("after\n", encoding="utf-8")
    # 通配字符按字面匹配：只报告同名文件，不扩展成模式。
    (root / "data[1].txt").write_text("literal\n", encoding="utf-8")
    (root / "data1.txt").write_text("not matched\n", encoding="utf-8")
    for index in range(250):
        (root / f"build_{index:03}.o").write_bytes(b"\0")
    async with bound_tools(root, tmp_path / "state") as tools:
        everything = await tools.workspace_changes(CancelToken())
        selected = await tools.workspace_changes(
            CancelToken(), paths=("tracked.py", "data[1].txt", "gone.txt")
        )
        _command(root, "checkout", "--", "tracked.py")
        (root / "data[1].txt").unlink()
        reverted = await tools.workspace_changes(CancelToken(), paths=("tracked.py", "data[1].txt"))
    assert everything is not None and everything.truncated and everything.total_entries == 253
    assert selected is not None and not selected.truncated
    assert [entry.path for entry in selected.entries] == ["tracked.py", "data[1].txt"]
    assert selected.head_oid == everything.head_oid
    assert reverted is not None and reverted.entries == ()


async def test_without_a_git_binding_there_is_nothing_to_observe(tmp_path: Path) -> None:
    async with CodingToolRuntime(tmp_path) as tools:
        assert await tools.workspace_changes(CancelToken()) is None
        assert await tools.workspace_changes(CancelToken(), paths=("a.txt",)) is None


async def test_read_failures_keep_the_read_tool_error_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "plain"
    nested = tmp_path / "repo" / "package"
    plain.mkdir()
    nested.mkdir(parents=True)
    _repository(tmp_path / "repo")
    async with bound_tools(plain, tmp_path / "state-plain") as tools:
        with pytest.raises(ReadToolError) as not_a_repository:
            await tools.workspace_changes(CancelToken())
    # 工作区是仓库的子目录时，固定Git端口不读取上层仓库。
    async with bound_tools(nested, tmp_path / "state-nested") as tools:
        with pytest.raises(ReadToolError) as parent_repository:
            await tools.workspace_changes(CancelToken())
    assert not_a_repository.value.code == "not_found"
    assert parent_repository.value.code == "path_denied"

    def too_many(result: object, limit: int) -> None:
        raise ReadToolError("limit_exceeded")

    monkeypatch.setattr(git_tools, "_status", too_many)
    async with bound_tools(tmp_path / "repo", tmp_path / "state-repo") as tools:
        with pytest.raises(ReadToolError) as unlistable:
            await tools.workspace_changes(CancelToken())
    assert unlistable.value.code == "limit_exceeded"
    with pytest.raises(KernelError) as closed:
        await tools.workspace_changes(CancelToken())
    assert closed.value.code == "tool_runtime_closed"
