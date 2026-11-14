"""读工具失败给出可操作的下一步：存档误用、容器路径、不存在路径。不回显参数值。"""

from __future__ import annotations

import pytest

from harnessix.agent.errors import KernelError
from harnessix.tools.runtime import CodingToolRuntime
from tests.tools.test_files import execute

ARTIFACT_ID = "26172b32-7cd8-5bcb-bb0c-f46352ce9687"


async def test_artifact_style_read_file_points_to_read_artifact(tmp_path):
    async with CodingToolRuntime(tmp_path) as tools:
        # 带read_artifact分页参数：参数校验失败，提示改用read_artifact，其后仍是字段说明。
        paged = await execute(tools, path=ARTIFACT_ID, offset=1, limit=2)
        assert paged.error.code == "tool_invalid_arguments"
        assert paged.error.message.startswith("read_file reads workspace files only")
        assert "read_artifact(artifact_id, offset, limit)" in paged.error.message
        assert "allowed fields: " in paged.error.message
        # 只把存档编号当路径：执行时不存在，同样提示。
        missing = await execute(tools, path=ARTIFACT_ID)
        assert missing.error.code == "tool_not_found"
        assert "read_artifact" in missing.error.message
        assert ARTIFACT_ID not in paged.error.message + missing.error.message


async def test_container_and_missing_paths_explain_the_next_step(tmp_path):
    (tmp_path / "src").mkdir()
    async with CodingToolRuntime(tmp_path) as tools:
        container = await execute(tools, path="/testbed/src/app.py")
        assert container.error.code == "tool_path_denied"
        assert "relative to the workspace root" in container.error.message
        assert "command tool" in container.error.message
        assert "read_artifact" not in container.error.message
        missing = await execute(tools, "grep", path="src/absent", query="x")
        assert missing.error.code == "tool_not_found"
        assert "glob or list_files" in missing.error.message


async def test_container_workspace_prefix_maps_to_workspace_root(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print(1)\n")
    async with CodingToolRuntime(tmp_path, container_workspace_paths=("/testbed",)) as tools:
        mapped = await execute(tools, path="/testbed/src/app.py")
        assert mapped.outcome == "succeeded" and mapped.output["path"] == "src/app.py"
        root = await execute(tools, "list_files", path="/testbed")
        assert root.outcome == "succeeded"
        # 只换算声明的挂载点；其他绝对路径仍被拒绝，不扩大可读范围。
        other = await execute(tools, path="/etc/hosts")
        assert other.error.code == "tool_path_denied"
        escape = await execute(tools, path="/testbed/../etc/hosts")
        assert escape.outcome == "failed"


async def test_absolute_path_inside_the_workspace_root_is_read_as_relative(tmp_path):
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print(1)\n")
    (tmp_path / "outside.py").write_text("secret\n")
    async with CodingToolRuntime(root) as tools:
        absolute = str(tools.workspace_root / "src" / "app.py")
        mapped = await execute(tools, path=absolute)
        assert mapped.outcome == "succeeded" and mapped.output["path"] == "src/app.py"
        found = await execute(tools, "grep", path=str(tools.workspace_root), query="print")
        assert found.outcome == "succeeded"
        # 工作区以外的绝对路径与借..逃逸仍被拒绝。
        outside = await execute(tools, path=str(tmp_path / "outside.py"))
        assert outside.error.code == "tool_path_denied"
        escape = await execute(tools, path=str(tools.workspace_root) + "/../outside.py")
        assert escape.outcome == "failed"


async def test_read_timeout_is_configurable_and_default_descriptors_are_unchanged(tmp_path):
    async with CodingToolRuntime(tmp_path) as default:
        baseline = default.definitions()
    async with CodingToolRuntime(tmp_path, read_timeout_seconds=5) as explicit:
        assert explicit.definitions() == baseline
    async with CodingToolRuntime(tmp_path, read_timeout_seconds=20) as relaxed:
        relaxed_definitions = relaxed.definitions()
    assert [d.version for d in relaxed_definitions] != [d.version for d in baseline]
    for bad in (0, 61, 5.0, True):
        with pytest.raises(KernelError):
            CodingToolRuntime(tmp_path, read_timeout_seconds=bad)
    with pytest.raises(KernelError):
        CodingToolRuntime(tmp_path, container_workspace_paths=("testbed",))


def test_every_read_error_code_has_an_english_next_step():
    """模型读到的读工具失败都是英文并说明下一步；兜底说明只留给表外的错误码。"""
    from typing import get_args

    from harnessix.tools import runtime
    from harnessix.tools.contracts import ReadErrorCode

    assert set(get_args(ReadErrorCode)) == runtime._READ_ERROR_MESSAGES.keys()
    assert all(message.isascii() for message in runtime._READ_ERROR_MESSAGES.values())


async def test_reads_past_the_end_or_of_a_changed_page_explain_how_to_continue(tmp_path):
    # 回归（fluentd-3466 fnnPB3X、fmt-3729 wgLG5JX）：两种失败原来都只有“工作区读取未完成”。
    (tmp_path / "short.py").write_text("one\ntwo\n")
    async with CodingToolRuntime(tmp_path) as tools:
        past = await execute(tools, path="short.py", start_line=3230)
        assert past.error.code == "tool_offset_out_of_range"
        assert past.error.message.startswith("The start position is past the end")
        page = await execute(tools, path="short.py", max_lines=1)
        (tmp_path / "short.py").write_text("one\ntwo\nthree\n")
        stale = await execute(
            tools, path="short.py", start_line=2, expected_revision=page.output["revision"]
        )
        assert stale.error.code == "tool_page_changed"
        assert "Read again without expected_revision" in stale.error.message
