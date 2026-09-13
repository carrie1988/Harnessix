from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.tools.runtime import CodingToolRuntime
from tests.tools.test_files import call, execute

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows原生Handle运行时语义")


async def test_windows_runtime_executes_all_four_read_tools(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    # 固定LF字节，避免Windows文本写入把测试夹具隐式转换为CRLF。
    (tmp_path / "src/a.py").write_bytes(b"first\nneedle = 1\n")
    (tmp_path / "src/b.txt").write_bytes(b"needle\n")
    (tmp_path / ".env").write_text("secret", encoding="utf-8")

    async with CodingToolRuntime(tmp_path) as tools:
        assert {item.name for item in tools.definitions()} == {
            "list_files",
            "read_file",
            "glob",
            "grep",
        }
        listed = await execute(tools, "list_files")
        assert [item["name"] for item in listed.output["entries"]] == ["src"]
        read = await execute(tools, path="src/a.py", max_lines=1)
        assert read.output["text"] == "first\n"
        found = await execute(tools, "glob", pattern="**/*.py")
        assert found.output["paths"] == ["src/a.py"]
        matched = await execute(tools, "grep", query="needle", include="**/*.py")
        assert [(item["path"], item["line"]) for item in matched.output["matches"]] == [
            ("src/a.py", 2)
        ]


async def test_windows_runtime_enforces_revision_denial_and_long_paths(tmp_path: Path) -> None:
    relative = "/".join(["segment" * 10] * 4) + "/main.py"
    target = tmp_path / Path(*relative.split("/"))
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    assert len(str(target)) > 260

    async with CodingToolRuntime(tmp_path) as tools:
        first = await execute(tools, path=relative, max_lines=1)
        assert first.outcome == "succeeded"
        target.write_text("after\n", encoding="utf-8")
        changed = await execute(
            tools,
            path=relative,
            start_line=2,
            expected_revision=first.output["revision"],
        )
        assert changed.error.code == "tool_page_changed"
        denied = await execute(tools, path=".env")
        assert denied.error.code == "tool_path_denied"


async def test_windows_runtime_rejects_junction_and_explicit_git(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("canary", encoding="utf-8")
    result = await asyncio.to_thread(
        subprocess.run,
        ["cmd", "/D", "/C", "mklink", "/J", str(tmp_path / "junction"), str(outside)],
        capture_output=True,
        check=False,
    )
    try:
        if result.returncode == 0:
            async with CodingToolRuntime(tmp_path) as tools:
                denied = await execute(tools, path="junction/secret.txt")
                assert denied.error.code == "tool_path_denied"
                assert "canary" not in denied.model_dump_json()
        with pytest.raises(KernelError) as git:
            CodingToolRuntime(tmp_path, git_executable=Path("git.exe"))
        assert git.value.code == "product_git_platform_unsupported"
    finally:
        if (tmp_path / "junction").exists():
            await asyncio.to_thread(
                subprocess.run,
                ["cmd", "/D", "/C", "rmdir", str(tmp_path / "junction")],
                capture_output=True,
                check=False,
            )
        (outside / "secret.txt").unlink(missing_ok=True)
        outside.rmdir()


async def test_windows_runtime_rejects_ads_reserved_names_and_hardlinks(tmp_path: Path) -> None:
    original = tmp_path / "original.txt"
    alias = tmp_path / "alias.txt"
    original.write_text("canary", encoding="utf-8")
    os.link(original, alias)

    async with CodingToolRuntime(tmp_path) as tools:
        for path in ("original.txt:secret", "CON", "C:/outside.txt", "alias.txt"):
            denied = await execute(tools, path=path)
            assert denied.error.code == "tool_path_denied"
            assert "canary" not in denied.model_dump_json()


async def test_windows_runtime_close_rejects_followup_calls(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("content", encoding="utf-8")
    tools = CodingToolRuntime(tmp_path)
    request = call(tools, path="a.txt")
    await tools.aclose()
    with pytest.raises(KernelError) as closed:
        await tools.execute(request, CancelToken())
    assert closed.value.code == "tool_runtime_closed"
