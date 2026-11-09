"""提案阶段的确定性调用修复：存档读取改道，补丁参数结构修复与精确锚定edit的SHA补齐。"""

from __future__ import annotations

import hashlib

import pytest

from harnessix.agent.call_repair import (
    fill_patch_preconditions,
    repair_patch_arguments,
    repair_proposal,
    reroute_artifact_read,
)
from harnessix.delivery.trusted_action import workspace_patch_descriptor
from harnessix.models.contracts import ToolCallCompleted, ToolCallRejected
from harnessix.product_config.action_contracts import build_product_native_process_profile
from harnessix.product_config.native_process import native_process_descriptor

ARTIFACT = "01a1baa7-30d1-75c3-a388-635e4f1e75c6"
OFFERED = {"read_file", "read_artifact", "grep"}


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        # 评测中实际出现的写法：沿用read_artifact的参数名调用read_file。
        (
            {"artifact_id": ARTIFACT, "offset": 0, "limit": 100},
            {"artifact_id": ARTIFACT, "offset": 0, "limit": 100},
        ),
        ({"artifact_id": ARTIFACT}, {"artifact_id": ARTIFACT}),
        ({"path": ARTIFACT, "limit": 500}, {"artifact_id": ARTIFACT, "limit": 200}),
        (
            {"artifact_id": ARTIFACT, "path": ARTIFACT, "offset": 40},
            {"artifact_id": ARTIFACT, "offset": 40},
        ),
    ],
)
def test_artifact_shaped_read_file_becomes_read_artifact(arguments, expected):
    assert reroute_artifact_read("read_file", arguments, OFFERED) == ("read_artifact", expected)


@pytest.mark.parametrize(
    ("tool", "arguments", "offered"),
    [
        # 工作区路径与存档编号同时出现：意图不唯一。
        ("read_file", {"artifact_id": ARTIFACT, "path": "src/app.py"}, OFFERED),
        # 存档参数之外还有行分页字段：不猜测。
        ("read_file", {"artifact_id": ARTIFACT, "start_line": 3}, OFFERED),
        ("read_file", {"artifact_id": "not-an-artifact"}, OFFERED),
        ("read_file", {"path": "src/app.py", "offset": 0, "limit": 10}, OFFERED),
        # 本步未开放read_artifact：不得借改道调用目录之外的工具。
        ("read_file", {"artifact_id": ARTIFACT}, {"read_file"}),
        ("grep", {"artifact_id": ARTIFACT}, OFFERED),
    ],
)
def test_ambiguous_or_unoffered_reads_are_left_to_strict_validation(tool, arguments, offered):
    assert reroute_artifact_read(tool, arguments, offered) is None


PATCH = workspace_patch_descriptor("standard")
DEFINITIONS = {name: PATCH for name in OFFERED} | {"apply_patch_batch": PATCH}


def test_only_completed_proposals_are_repaired():
    rejected = ToolCallRejected(call_id="c1", reason="unregistered_tool", argument_chars=2)
    assert repair_proposal(rejected, DEFINITIONS, reroute=True) is rejected
    proposal = ToolCallCompleted(
        call_id="c2", tool="read_file", arguments={"artifact_id": ARTIFACT}
    )
    repaired = repair_proposal(proposal, DEFINITIONS, reroute=True)
    assert isinstance(repaired, ToolCallCompleted)
    assert (repaired.call_id, repaired.tool) == ("c2", "read_artifact")
    # 强制工具选择时不改写工具名。
    assert repair_proposal(proposal, DEFINITIONS, reroute=False) is proposal
    assert repair_proposal(proposal, {}, reroute=True) is proposal


EDIT = {"old_text": "old", "new_text": "new"}


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        # 外层单文件字段并入files（字符串mode按文本权限换算，edit上的默认mode去掉）。
        (
            {"operation": "edit", "path": "a.py", "mode": "420", "files": [{"edits": [EDIT]}]},
            {"files": [{"edits": [EDIT], "operation": "edit", "path": "a.py"}]},
        ),
        # 文件项中的null视为未给出。
        (
            {"path": "a.py", "files": [{"path": None, "operation": "edit", "edits": [EDIT]}]},
            {"files": [{"path": "a.py", "operation": "edit", "edits": [EDIT]}]},
        ),
        # 只有edits时补上edit；空content去掉；read_file结果里的content_sha256并入。
        (
            {"files": [{"path": "a.py", "edits": [EDIT], "content": None, "content_sha256": "s"}]},
            {
                "files": [
                    {"path": "a.py", "edits": [EDIT], "operation": "edit", "expected_sha256": "s"}
                ]
            },
        ),
        (
            {"files": [{"operation": "replace", "path": "a.py", "content": "x", "edits": None}]},
            {"files": [{"operation": "replace", "path": "a.py", "content": "x"}]},
        ),
        (
            {"files": [{"operation": "create", "path": "a.py", "content": "x", "mode": "0755"}]},
            {"files": [{"operation": "create", "path": "a.py", "content": "x", "mode": 493}]},
        ),
    ],
)
def test_patch_structure_is_repaired_without_guessing(arguments, expected):
    assert repair_patch_arguments(arguments) == expected


@pytest.mark.parametrize(
    "arguments",
    [
        # edit同时给出content；可执行权限意图不明；replace同时给出edits。
        {"files": [{"operation": "edit", "path": "a.py", "content": "x", "edits": [EDIT]}]},
        {"files": [{"operation": "edit", "path": "a.py", "mode": 493, "edits": [EDIT]}]},
        {"files": [{"operation": "replace", "path": "a.py", "content": "x", "edits": [EDIT]}]},
    ],
)
def test_ambiguous_patch_structure_is_left_to_strict_validation(arguments):
    assert repair_patch_arguments(arguments) == arguments


@pytest.mark.parametrize(
    "files",
    [
        # 多文件时外层字段归属不明；同名字段取值冲突。
        [{"edits": [EDIT]}, {"edits": [EDIT]}],
        [{"path": "b.py", "edits": [EDIT]}],
    ],
)
def test_loose_file_fields_are_not_merged_when_their_owner_is_unclear(files):
    repaired = repair_patch_arguments({"path": "a.py", "files": files})
    assert repaired["path"] == "a.py"
    assert [entry.get("path") for entry in repaired["files"]] == [f.get("path") for f in files]


def _reader(files):
    async def read(path):
        return files.get(path)

    return read


def _patch(*entries):
    return ToolCallCompleted(
        call_id="p", tool="apply_patch_batch", arguments={"files": list(entries)}
    )


async def test_anchored_edits_get_the_current_sha_and_others_keep_theirs():
    body = b"old\nkeep\n"
    current = hashlib.sha256(body).hexdigest()
    read = _reader({"a.py": body, "dup.py": b"old\nold\n", "bin.py": b"\xff"})
    stale = {"operation": "edit", "path": "a.py", "expected_sha256": "0" * 64, "edits": [EDIT]}
    missing = {"operation": "edit", "path": "a.py", "edits": [EDIT]}
    duplicate = {**stale, "path": "dup.py"}
    unreadable = {**stale, "path": "gone.py"}
    binary = {**stale, "path": "bin.py"}
    replace = {"operation": "replace", "path": "a.py", "expected_sha256": "0" * 64}

    filled = await fill_patch_preconditions(
        _patch(stale, missing, duplicate, unreadable, binary, replace), DEFINITIONS, read
    )

    files = filled.arguments["files"]
    assert [entry.get("expected_sha256") for entry in files] == [
        current,
        current,
        "0" * 64,
        "0" * 64,
        "0" * 64,
        "0" * 64,
    ]


async def test_only_the_workspace_patch_contract_is_filled():
    proposal = _patch({"operation": "edit", "path": "a.py", "edits": [EDIT]})
    read = _reader({"a.py": b"old\n"})
    current = hashlib.sha256(b"old\n").hexdigest()
    compact = {"apply_patch_batch": workspace_patch_descriptor("compact")}
    filled = await fill_patch_preconditions(proposal, compact, read)
    assert filled.arguments["files"][0]["expected_sha256"] == current
    other = PATCH.model_copy(
        update={"input_schema": {"type": "object", "properties": {"patch": {"type": "string"}}}}
    )
    assert await fill_patch_preconditions(proposal, {"apply_patch_batch": other}, read) is proposal
    assert await fill_patch_preconditions(proposal, {}, read) is proposal
    unchanged = _patch(
        {"operation": "edit", "path": "a.py", "edits": [EDIT], "expected_sha256": current}
    )
    assert await fill_patch_preconditions(unchanged, DEFINITIONS, read) is unchanged


def test_missing_required_const_field_is_filled_from_the_schema():
    command = native_process_descriptor(
        build_product_native_process_profile(
            profile_id="testbed",
            version="v1",
            description="run in the task container",
            backend="external_boundary",
            program="/bin/bash",
            arguments=("-c",),
            timeout_seconds=60,
        )
    )
    definitions = {command.name: command}
    proposal = ToolCallCompleted(call_id="c", tool=command.name, arguments={"cmd": "ls"})
    repaired = repair_proposal(proposal, definitions, reroute=True)
    assert repaired.arguments == {"cmd": "ls", "profile": "testbed"}
    # 已给出的值（即使错误）与非必填const字段都不改写，由严格校验照旧判定。
    wrong = proposal.model_copy(update={"arguments": {"cmd": "ls", "profile": "other"}})
    assert repair_proposal(wrong, definitions, reroute=True) is wrong
    patch = ToolCallCompleted(
        call_id="p",
        tool="apply_patch_batch",
        arguments={"files": [{"operation": "edit", "path": "a.py", "edits": [EDIT]}]},
    )
    assert "spec_version" not in repair_proposal(patch, DEFINITIONS, reroute=True).arguments
