"""模型线协议上的工具结果正文：真实工具输出渲染为紧凑文本，形状不符时保持规范JSON。"""

from __future__ import annotations

import json
from uuid import uuid4

from harnessix.agent.models import (
    Item,
    ItemContent,
    ItemStatus,
    ToolCallContent,
    ToolResultContent,
)
from harnessix.domain.models import EffectClass
from harnessix.models._chat_mapping import build_request
from harnessix.models._history import messages_for
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.tool_result_text import encode_json, tool_result_text
from harnessix.tools import search
from harnessix.tools.contracts import ReadFileInput
from harnessix.tools.files import read_file_snapshot
from harnessix.tools.search_contracts import GrepInput, RegexGrepInput
from harnessix.tools.workspace import ReadOperation, Workspace
from tests.contracts.provider import model_request

ARTIFACT = {
    "artifact_id": "01a1bdff-6c81-7e1f-a10b-5e400a79845c",
    "sha256": "0" * 64,
    "size_bytes": 804,
    "records": 19,
    "format": "jsonl/v1",
    "complete": True,
    "expires_at": "2026-11-10T01:49:55.713122Z",
}


def item(content: ItemContent) -> Item:
    return Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=content)


def succeeded(output: object) -> dict[str, object]:
    return {"outcome": "succeeded", "output": output, "error": None, "diff_artifact": None}


def grep_output(tmp_path, files: dict[str, bytes], args) -> dict[str, object]:
    for name, body in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    with Workspace(tmp_path) as workspace:
        return search.grep(workspace, args, ReadOperation()).model_dump(mode="json")


def test_snapshot_grep_renders_grep_n_lines_with_one_identity_per_file(tmp_path):
    body = b"".join(b"line %d\n" % n for n in range(1, 13))
    preview = grep_output(
        tmp_path, {"pkg/a.py": body}, RegexGrepInput(query="^line (4|6)$", context_lines=1)
    )
    text = tool_result_text(succeeded({"preview": preview, "artifact": ARTIFACT}))
    sha = preview["matches"][0]["content_sha256"]
    revision = preview["matches"][0]["revision"]
    assert text.splitlines() == [
        "grep .: 2 matches",
        f"== pkg/a.py content_sha256={sha} revision={revision} ({len(body)} bytes)",
        "3-line 3",
        "4:line 4",
        "5-line 5",
        "6:line 6",
        "7-line 7",
    ]


def test_separated_hits_truncation_and_literal_fallback_are_stated(tmp_path):
    body = b"x = f(1)\n" + b"pass\n" * 10 + b"y = f(1)\n"
    preview = grep_output(
        tmp_path, {"m.py": body}, RegexGrepInput(query="f(1)", context_lines=0, max_results=2)
    )
    preview_truncated = {**preview, "truncated": True, "truncation_reason": "result_limit"}
    preview_truncated["scan_complete"] = False
    text = tool_result_text(succeeded({"preview": preview_truncated, "artifact": ARTIFACT}))
    lines = text.splitlines()
    assert lines[0] == (
        "grep .: 2 matches; truncated (result_limit); full results: read_artifact "
        f"artifact_id={ARTIFACT['artifact_id']}; the regex matched nothing, these are literal "
        "matches of the query"
    )
    assert lines[2:] == ["1:x = f(1)", "--", "12:y = f(1)"]


def test_zero_matches_report_files_searched_and_gaps(tmp_path):
    preview = grep_output(tmp_path, {"a.py": b"alpha\n"}, RegexGrepInput(query="beta"))
    assert tool_result_text(succeeded(preview)) == "grep .: no matches (1 files searched)"
    gapped = {**preview, "scan_complete": False}
    gapped["stats"] = {**preview["stats"], "binary_files": 2}
    assert tool_result_text(succeeded(gapped)) == (
        "grep .: no matches (1 files searched); skipped 2 binary_files"
    )


def test_legacy_literal_grep_has_revision_only(tmp_path):
    preview = grep_output(tmp_path, {"a.py": b"needle\n"}, GrepInput(query="needle"))
    revision = preview["matches"][0]["revision"]
    assert tool_result_text(succeeded(preview)).splitlines() == [
        "grep .: 1 match",
        f"== a.py revision={revision}",
        "1:needle",
    ]


def test_glob_lists_paths(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/a.py").write_bytes(b"")
    with Workspace(tmp_path) as workspace:
        output = search.glob(
            workspace, search.GlobInput(pattern="pkg/*.py"), ReadOperation()
        ).model_dump(mode="json")
    assert tool_result_text(succeeded(output)) == "glob .: 1 path\npkg/a.py"


def test_read_file_snapshot_renders_raw_text_with_identity(tmp_path):
    body = 'def f(x):\n    return "a\\tb"\nprint(f(1))\n'
    (tmp_path / "m.py").write_text(body)
    with Workspace(tmp_path) as workspace:
        output = read_file_snapshot(
            workspace, ReadFileInput(path="m.py", max_lines=2), ReadOperation()
        ).model_dump(mode="json")
    text = tool_result_text(succeeded(output))
    assert text == (
        f"m.py lines 1-2 of a {len(body)}-byte file; truncated (line_limit), next_line=3\n"
        f"content_sha256={output['content_sha256']} revision={output['revision']}\n"
        "----\n"
        'def f(x):\n    return "a\\tb"\n'
    )


def test_process_output_keeps_exit_status_and_stream_text():
    output = {
        "version": "trusted-process-output/v2",
        "profile": "testbed",
        "process_id": str(uuid4()),
        "state": "exited",
        "returncode": 1,
        "stop_reason": "exited",
        "stdout": {"observed_bytes": 3, "observed_sha256": "0" * 64, "truncated": False},
        "stderr": {"observed_bytes": 9000, "observed_sha256": "1" * 64, "truncated": False},
        "complete": True,
        "diagnostic_preview": {
            "stdout": {"text": "ok\n", "size_bytes": 3, "truncated": False},
            "stderr": {"text": "Error: x\n", "size_bytes": 1024, "truncated": True},
        },
        "artifact": ARTIFACT,
    }
    assert tool_result_text(succeeded(output)) == (
        "exit 1; stdout 3 bytes; stderr 9000 bytes; full output: read_artifact "
        f"artifact_id={ARTIFACT['artifact_id']}\n"
        "[stdout]\nok\n\n"
        "[stderr: first 1024 of 9000 bytes]\nError: x\n"
    )


def test_other_results_failures_and_unknown_shapes_stay_canonical_json():
    cases = [
        succeeded({"ok": True}),
        succeeded({"path": "m.py", "text": "x", "definition": None}),
        succeeded(None),
        {"outcome": "failed", "output": None, "error": {"code": "x"}, "diff_artifact": None},
        {**succeeded({"preview": {"paths": []}, "artifact": ARTIFACT})},
    ]
    for public in cases:
        assert tool_result_text(public) == encode_json(public)
    broken_grep = succeeded(
        {
            "path": ".",
            "stats": {},
            "scan_complete": True,
            "truncated": False,
            "truncation_reason": None,
            "matches": [],
        }
    )
    assert tool_result_text(broken_grep) == encode_json(broken_grep)


def test_messages_send_rendered_tool_results(tmp_path):
    preview = grep_output(tmp_path, {"a.py": b"needle\n"}, RegexGrepInput(query="needle"))
    grep_call = ToolCallContent(
        call_id=uuid4(),
        provider_call_id="call-1",
        tool="grep",
        tool_version="4",
        effect_class=EffectClass.READ_ONLY,
    )
    request = model_request()
    request = request.model_copy(
        update={
            "history": (
                *request.history,
                item(grep_call),
                item(
                    ToolResultContent(
                        call_id=grep_call.call_id, outcome="succeeded", output=preview
                    )
                ),
            )
        }
    )
    compact = next(
        m for m in messages_for(request, tool_result_format="text") if m["role"] == "tool"
    )
    assert compact["content"].startswith("grep .: 1 match\n== a.py content_sha256=")
    default = next(m for m in messages_for(request) if m["role"] == "tool")
    assert json.loads(default["content"])["output"] == preview


def test_openai_chat_option_is_off_by_default_and_outside_the_fingerprint(tmp_path):
    preview = grep_output(tmp_path, {"a.py": b"needle\n"}, RegexGrepInput(query="needle"))
    config = OpenAIChatConfig(model="m", output_token_parameter="max_tokens")
    assert "tool_result_format" not in config.model_dump()
    compact = config.model_copy(update={"tool_result_format": "text"})
    assert compact.model_dump()["tool_result_format"] == "text"
    grep_call = ToolCallContent(
        call_id=uuid4(),
        provider_call_id="call-1",
        tool="grep",
        tool_version="4",
        effect_class=EffectClass.READ_ONLY,
    )
    request = model_request().model_copy(
        update={
            "history": (
                *model_request().history,
                item(grep_call),
                item(
                    ToolResultContent(
                        call_id=grep_call.call_id, outcome="succeeded", output=preview
                    )
                ),
            )
        }
    )
    body, _ = build_request(request, compact)
    tool_message = next(m for m in body["messages"] if m["role"] == "tool")
    assert tool_message["content"].startswith("grep .: 1 match")
    body, _ = build_request(request, config)
    tool_message = next(m for m in body["messages"] if m["role"] == "tool")
    assert json.loads(tool_message["content"])["output"] == preview


def test_patch_v2_and_process_v3_render_digests_diff_and_stream_tails():
    patch = {
        "transaction_id": "b4c1ba3e-60b2-58af-9f63-4ee3d6786820",
        "files": 2,
        "state": "published",
        "origin": "execution",
        "diff_sha256": "2" * 64,
        "spec_version": "harnessix.workspace-patch-output/v2",
        "changes": [
            {"path": "a.py", "content_sha256": "3" * 64},
            {"path": "b.py", "content_sha256": None},
        ],
        "diff_preview": {
            "text": "--- a/a.py\n+++ b/a.py\n",
            "total_bytes": 9000,
            "truncated": True,
        },
    }
    assert tool_result_text(succeeded(patch)) == (
        "patch published: 2 files; transaction_id=b4c1ba3e-60b2-58af-9f63-4ee3d6786820\n"
        f"a.py content_sha256={'3' * 64}\n"
        "b.py deleted\n"
        "[diff: first part of 9000 bytes]\n"
        "--- a/a.py\n+++ b/a.py\n"
    )
    process = {
        "version": "trusted-process-output/v3",
        "state": "exited",
        "returncode": 1,
        "stop_reason": "exited",
        "stdout": {"observed_bytes": 5},
        "stderr": {"observed_bytes": 20000},
        "complete": True,
        "diagnostic_preview": {
            "stdout": {"text": None, "size_bytes": 0, "truncated": True, "offset": 0},
            "stderr": {"text": "Error\n", "size_bytes": 6, "truncated": True, "offset": 19994},
        },
    }
    assert tool_result_text(succeeded(process)) == (
        "exit 1; stdout 5 bytes; stderr 20000 bytes\n"
        "[stdout: not shown, binary or control characters]\n"
        "[stderr: last 6 of 20000 bytes]\nError\n"
    )
