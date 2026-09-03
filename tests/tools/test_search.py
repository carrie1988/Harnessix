from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from harnessix.tools.patterns import PathPattern
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.search_contracts import GlobOutput, GrepOutput
from tests.tools.test_files import execute


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("*.py", "main.py", True),
        ("*.py", "src/main.py", False),
        ("**/*.py", "main.py", True),
        ("**/*.py", "src/a/main.py", True),
        ("src/**/a?.[pt]y", "src/a1.py", True),
        ("src/**/a?.[pt]y", "src/lib/a1.ty", True),
        ("src/**", "src/a/b", True),
        ("**/**/x", "a/x", True),
        ("[!a]*", "b", True),
        ("[!a]*", "a", False),
        ("[?].py", "?.py", True),
        ("*.py", ".hidden.py", True),
        ("*.py", "UPPER.PY", False),
        ("a[", "a[", True),
    ],
)
def test_bounded_glob_semantics(pattern, path, expected):
    assert PathPattern(pattern).matches(path) is expected


async def test_glob_and_grep_then_read_with_revision(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "main.py").write_text("无需匹配\n")
    (tmp_path / "src/a.py").write_text("第一行\n目标函数 = 1\n目标函数 = 2\n", encoding="utf-8")
    (tmp_path / "src/b.txt").write_text("目标函数\n")
    async with CodingToolRuntime(tmp_path) as tools:
        found = await execute(tools, "glob", pattern="**/*.py")
        assert found.outcome == "succeeded"
        assert found.output["paths"] == ["main.py", "src/a.py"]
        assert found.output["scan_complete"] and not found.output["truncated"]
        result = await execute(tools, "grep", query="目标函数", include="**/*.py")
        assert [(m["path"], m["line"]) for m in result.output["matches"]] == [
            ("src/a.py", 2),
            ("src/a.py", 3),
        ]
        assert result.output["scan_complete"]
        hit = result.output["matches"][0]
        read = await execute(
            tools,
            path=hit["path"],
            start_line=hit["line"],
            expected_revision=hit["revision"],
            max_lines=1,
        )
        assert read.output["text"] == "目标函数 = 1\n"


async def test_result_limit_is_not_reported_as_complete(tmp_path):
    for name in ("c.py", "a.py", "b.py"):
        (tmp_path / name).write_text("needle needle\n")
    async with CodingToolRuntime(tmp_path) as tools:
        for tool, args, field in (("glob", {}, "paths"), ("grep", {"query": "needle"}, "matches")):
            result = await execute(tools, tool, max_results=2, **args)
            assert len(result.output[field]) == 2
            assert result.output["truncated"] and not result.output["scan_complete"]
            assert result.output["truncation_reason"] == "result_limit"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("glob", {"pattern": "../*.py"}),
        ("glob", {"pattern": "/tmp/*"}),
        ("glob", {"pattern": "**/{a,b}"}),
        ("glob", {"pattern": "a\\b"}),
        ("glob", {"pattern": ""}),
        ("glob", {"pattern": "中" * 100}),
        ("glob", {"pattern": "x/" * 33 + "y"}),
        ("glob", {"include_ignored": "true"}),
        ("glob", {"max_results": True}),
        ("grep", {"query": ""}),
        ("grep", {"query": "x\ny"}),
        ("grep", {"query": "中" * 100}),
        ("grep", {"query": "x", "regex": True}),
    ],
)
async def test_invalid_search_arguments_fail_before_io(tmp_path, tool, args):
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, tool, **args)
        assert result.error.code == "tool_invalid_arguments"


@pytest.mark.parametrize(
    "tool,args,field,model",
    [("glob", {}, "paths", GlobOutput), ("grep", {"query": "q"}, "matches", GrepOutput)],
)
@pytest.mark.parametrize("corrupt", ["completeness", "reason", "order", "duplicate", "empty"])
async def test_output_contract_rejects_inconsistent_search_results(
    tmp_path, tool, args, field, model, corrupt
):
    for name in ("a", "b"):
        (tmp_path / name).write_text("q\n")
    async with CodingToolRuntime(tmp_path) as tools:
        output = (await execute(tools, tool, **args)).output
    if corrupt == "completeness":
        output["scan_complete"] = False
    elif corrupt == "reason":
        output["truncation_reason"] = "result_limit"
    elif corrupt == "order":
        output[field].reverse()
    elif corrupt == "duplicate":
        output[field].append(output[field][-1])
    else:
        output.update(truncated=True, scan_complete=False, truncation_reason="result_limit")
        output[field] = []
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(output))
