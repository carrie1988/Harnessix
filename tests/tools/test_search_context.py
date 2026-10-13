"""有界搜索前后文：真实读取、历史兼容、取消、归档及平台端口一致性。"""

import hashlib
import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.reducer import replay
from harnessix.tools import search
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.search import SearchCapture
from harnessix.tools.search_contracts import ArchivedGrepOutput, GrepInput, GrepMatch, GrepOutput
from harnessix.tools.windows_read import WindowsReadRuntime
from harnessix.tools.workspace import ReadOperation
from tests.artifacts.helpers import exercise, results
from tests.tools.test_files import call, execute
from tests.tools.test_windows_read_adapter import _FakeWindowsRoot


@pytest.mark.parametrize("radius", [0, 1, 2, 3])
async def test_context_edges_crlf_unicode_and_unterminated_carriage_return(tmp_path, radius):
    lines = ["needle头", "  第一行", "", "needle中", "  最后", "needle尾\r"]
    (tmp_path / "source.py").write_bytes(("\r\n".join(lines)).encode())
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, "grep", query="needle", context_lines=radius)
    output = GrepOutput.model_validate_json(json.dumps(result.output))
    assert output.scan_complete and output.stats.files_read == 1
    assert [hit.line for hit in output.matches] == [1, 4, 6]
    for hit in output.matches:
        if radius == 0:
            assert hit.context is None
        else:
            first, last = max(0, hit.line - 1 - radius), min(len(lines), hit.line + radius)
            assert hit.context.start_line == first + 1
            assert [view.text for view in hit.context.lines] == lines[first:last]
            assert not any(view.text_truncated for view in hit.context.lines)
        assert not hit.text_truncated


async def test_default_preserves_match_facts_and_legacy_output_is_readable(tmp_path):
    (tmp_path / "source.py").write_text("before\nneedle\nafter\n")
    async with CodingToolRuntime(tmp_path) as tools:
        old_style = (await execute(tools, "grep", query="needle")).output
        explicit = (await execute(tools, "grep", query="needle", context_lines=0)).output
        contextual = (await execute(tools, "grep", query="needle", context_lines=2)).output
    assert old_style == explicit
    plain, enriched = old_style["matches"][0], contextual["matches"][0]
    assert {k: v for k, v in plain.items() if k != "context"} == {
        k: v for k, v in enriched.items() if k != "context"
    }
    del plain["context"]
    restored = GrepOutput.model_validate_json(json.dumps(old_style))
    assert restored.matches[0].context is None
    assert "content_sha256" not in enriched and "digest_status" not in enriched


@pytest.mark.parametrize("radius", [-1, 4, True, 1.0, "1", None])
async def test_invalid_context_limits_are_rejected_before_read(tmp_path, monkeypatch, radius):
    def forbidden(*args, **kwargs):
        pytest.fail("非法上下文参数不得进入读取端")

    monkeypatch.setattr(search, "grep", forbidden)
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, "grep", query="needle", context_lines=radius)
    assert result.error.code == "tool_invalid_arguments"


async def test_each_line_is_bounded_and_long_neighbor_is_not_a_complete_search(tmp_path):
    body = "前" * 1600 + "\n" + "前" * 800 + "needle" + "后" * 100 + "\n"
    (tmp_path / "source.py").write_text(body, encoding="utf-8")
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, "grep", query="needle", context_lines=3)
    output = GrepOutput.model_validate_json(json.dumps(result.output))
    assert output.stats.long_lines == 1 and not output.scan_complete
    hit = output.matches[0]
    assert hit.line == 2 and "needle" in hit.text and hit.text_truncated
    assert hit.context.start_line == 1
    assert all(view.text_truncated for view in hit.context.lines)
    assert all(len(view.text.encode()) <= search.MAX_MATCH_BYTES for view in hit.context.lines)


@pytest.mark.parametrize("corruption", ["start", "center", "flag", "newline", "bytes", "side"])
async def test_output_contract_rejects_false_context(tmp_path, corruption):
    (tmp_path / "source.py").write_text("before\nneedle\nafter\n")
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, "grep", query="needle", context_lines=1)
    hit = result.output["matches"][0]
    window = hit["context"]
    if corruption == "start":
        window["start_line"] = 3
    elif corruption == "center":
        window["lines"][1]["text"] = "not the match"
    elif corruption == "flag":
        window["lines"][1]["text_truncated"] = True
    elif corruption == "newline":
        window["lines"][0]["text"] = "two\nlines"
    elif corruption == "bytes":
        window["lines"][0]["text"] = "中" * 129
    else:
        window["lines"] = [window["lines"][0], *window["lines"]]
        hit["line"] = 5
        window["lines"] = [window["lines"][0], *window["lines"]]
    with pytest.raises(ValidationError):
        GrepOutput.model_validate_json(json.dumps(result.output))


@pytest.mark.parametrize("maximum,reason", [(1, "result_limit"), (200, "output_limit")])
async def test_context_records_remain_inside_original_output_limit(tmp_path, maximum, reason):
    (tmp_path / "source.py").write_text(("needle" + "\t" * 378 + "\n") * 40)
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, "grep", query="needle", context_lines=3, max_results=maximum)
    output = GrepOutput.model_validate_json(json.dumps(result.output))
    assert output.truncated and not output.scan_complete and output.truncation_reason == reason
    assert output.matches
    encoded_bytes = sum(len(hit.model_dump_json().encode()) + 1 for hit in output.matches)
    assert encoded_bytes <= search.MAX_SEARCH_RECORD_BYTES


def test_largest_escaped_context_record_can_fit_the_existing_first_record_limit():
    lines = ["\t" * 384] * 7
    lines[3] = "needle" + "\t" * 378
    text = "\n".join(lines)
    args = GrepInput(query="needle", context_lines=3)
    hits = list(
        search.grep_line_matches("x" * 1024, "f" * 64, text, args, ReadOperation(), search._Scan())
    )
    assert len(hits) == 1
    assert len(hits[0].context.lines) == 7
    assert len(hits[0].model_dump_json().encode()) + 1 < search.MAX_SEARCH_RECORD_BYTES


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_window_iteration_observes_cancellation_and_deadline(stop):
    operation = ReadOperation()
    scan = search._Scan()
    hits = search.grep_line_matches(
        "source.py",
        "f" * 64,
        "needle\n" * 1000,
        GrepInput(query="needle", context_lines=3),
        operation,
        scan,
    )
    assert next(hits).line == 1
    if stop == "cancel":
        operation.stopped.set()
        expected = TurnCancelled
    else:
        operation.deadline = 0
        expected = ReadToolError
    with pytest.raises(expected):
        next(hits)


@pytest.mark.parametrize("radius", [0, 3])
async def test_native_port_uses_same_windows_capture_and_gaps(tmp_path, radius):
    (tmp_path / "source.py").write_text("前\nneedle\n" + "后" * 1600 + "\nneedle尾\r")
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, "grep", query="needle", context_lines=radius)
    native = WindowsReadRuntime(tmp_path, root_factory=cast(Any, _FakeWindowsRoot))
    capture = SearchCapture()
    try:
        output = native.grep(
            GrepInput(query="needle", context_lines=radius), ReadOperation(), capture=capture
        )
    finally:
        native.close()
    common = output.model_dump(mode="json")
    for view in (common, result.output):
        for hit in view["matches"]:
            hit.pop("revision")
    assert common == result.output
    captured = [GrepMatch.model_validate_json(line) for line in capture.body.splitlines()]
    assert [(hit.line, hit.context) for hit in captured] == [
        (hit.line, hit.context) for hit in output.matches
    ]
    assert not capture.complete and output.stats.long_lines == 1


async def test_search_context_revision_does_not_allow_changed_file_read(tmp_path):
    path = tmp_path / "source.py"
    path.write_text("before\nneedle\nafter\n")
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, "grep", query="needle", context_lines=3)
        hit = result.output["matches"][0]
        path.write_text("needle CHANGED\n")
        read = await execute(tools, path=hit["path"], expected_revision=hit["revision"])
    assert read.error.code == "tool_page_changed"


async def test_context_is_persisted_in_full_archive_and_survives_reopen(tmp_path):
    store, artifacts, scope, thread, turn = await exercise(
        tmp_path, args={"query": "needle", "context_lines": 3, "max_results": 1}, count=12
    )
    output = results(turn)[0].output
    assert output["preview"]["truncated"] and len(output["preview"]["matches"]) == 1
    assert output["artifact"]["complete"] and output["artifact"]["records"] == 12
    page = await artifacts.read(thread.thread_id, scope, UUID(output["artifact"]["artifact_id"]))
    hits = [GrepMatch.model_validate_json(line) for line in page.text.splitlines()]
    assert [hit.line for hit in hits] == list(range(1, 13))
    assert all(hit.context is not None for hit in hits)
    assert hits[5].context.start_line == 3 and len(hits[5].context.lines) == 7
    reopened = await store.get_thread(thread.thread_id)
    assert replay(await store.events(thread.thread_id)) == reopened
    assert reopened.turns[-1] == turn


async def test_previous_major_grep_contract_cannot_execute_new_context(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("旧工具合同不得进入新版执行")

    monkeypatch.setattr(search, "grep", forbidden)
    async with CodingToolRuntime(tmp_path) as tools:
        current = call(tools, "grep", query="needle", context_lines=3)
        assert current.tool_version.startswith("2.")
        stale = current.model_copy(update={"tool_version": "1." + current.tool_version[2:]})
        with pytest.raises(KernelError) as failure:
            await tools.execute(stale, CancelToken())
    assert failure.value.code == "tool_contract_changed"


@pytest.mark.parametrize(
    "name,model,digest",
    [
        (
            "grep-input",
            GrepInput,
            "2cfe7bcf76ac3697b1fb127aaaf9245720b1c12d511ac061bd68b7656b606c87",
        ),
        (
            "grep-output",
            GrepOutput,
            "b28e788524d581b764c8e7d20f5af238628295fafed7e61b341984a9af05640a",
        ),
        (
            "archived-grep-output",
            ArchivedGrepOutput,
            "b965a210e3a3340c456fb5a86c2517d5800108b01d8748c0f5de840e41bc8e11",
        ),
    ],
)
def test_context_v2_schemas_are_frozen_without_rewriting_v1(name, model, digest):
    path = Path(__file__).parents[2] / "spec" / f"{name}-v2.schema.json"
    assert json.loads(path.read_bytes()) == model.model_json_schema()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
