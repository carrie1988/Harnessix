"""提案阶段的确定性调用修复：读取改道，补丁参数结构修复，以及用宿主已掌握的事实补全前置条件。"""

from __future__ import annotations

import hashlib
import json

import pytest

from harnessix.agent.call_repair import (
    fill_patch_preconditions,
    repair_patch_arguments,
    repair_proposal,
    reroute_artifact_read,
    reroute_grep_shaped_read,
)
from harnessix.delivery.trusted_action import workspace_patch_descriptor
from harnessix.delivery.trusted_action_contracts import WorkspacePatchInput
from harnessix.models.contracts import ToolCallCompleted, ToolCallRejected
from harnessix.tools.search_contracts import RegexGrepInput
from tests.agent.test_file_observations import patched, read

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
OTHER_EDIT = {"old_text": "keep", "new_text": "kept"}
SHA = "5" * 64


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
        # 新文件缺mode（或null）取默认0644。
        (
            {"files": [{"operation": "create", "path": "a.py", "content": "x", "mode": None}]},
            {"files": [{"operation": "create", "path": "a.py", "content": "x", "mode": 420}]},
        ),
        # 文本"null"的mode视为未给出（gson-2061 UnvpMCp、coreutils 3DJd6b8）。
        (
            {"files": [{"operation": "edit", "path": "a.py", "edits": [EDIT], "mode": "null"}]},
            {"files": [{"operation": "edit", "path": "a.py", "edits": [EDIT]}]},
        ),
        # delete附带的默认mode是噪声（prometheus-11859 3SmxbrW）。
        (
            {
                "files": [
                    {"operation": "delete", "path": "a.py", "expected_sha256": SHA, "mode": 420}
                ]
            },
            {"files": [{"operation": "delete", "path": "a.py", "expected_sha256": SHA}]},
        ),
        # 值为null的未知字段不携带意图（faker-2705、gin-4003、nushell-13605、lombok-3009）。
        (
            {
                "files": [
                    {
                        "operation": "edit",
                        "path": "a.py",
                        "expected_sha256": SHA,
                        "content_sha256": None,
                        "old_text_2_unused": None,
                        "edits": [{**EDIT, "note": None}],
                    }
                ]
            },
            {
                "files": [
                    {"operation": "edit", "path": "a.py", "expected_sha256": SHA, "edits": [EDIT]}
                ]
            },
        ),
        # 单处编辑直接写在文件项上（preact-4245 gXn7ppg）。
        (
            {"files": [{"operation": "edit", "path": "a.py", **EDIT}]},
            {"files": [{"operation": "edit", "path": "a.py", "edits": [EDIT]}]},
        ),
        # operation写成replace却只有edits：没有完整content，只能是精确编辑（fmt-2457 5KyxPNq）。
        (
            {
                "files": [
                    {
                        "operation": "replace",
                        "path": "a.py",
                        "expected_sha256": SHA,
                        "mode": 420,
                        "edits": [EDIT],
                    }
                ]
            },
            {
                "files": [
                    {"operation": "edit", "path": "a.py", "expected_sha256": SHA, "edits": [EDIT]}
                ]
            },
        ),
        # 同一文件的两个edit项并成一项（gson-2061 mWKyQ4E）；只有一项带SHA时沿用它。
        (
            {
                "files": [
                    {"operation": "edit", "path": "a.py", "expected_sha256": SHA, "edits": [EDIT]},
                    {"operation": "edit", "path": "b.py", "edits": [EDIT]},
                    {"operation": "edit", "path": "a.py", "edits": [OTHER_EDIT]},
                ]
            },
            {
                "files": [
                    {
                        "operation": "edit",
                        "path": "a.py",
                        "expected_sha256": SHA,
                        "edits": [EDIT, OTHER_EDIT],
                    },
                    {"operation": "edit", "path": "b.py", "edits": [EDIT]},
                ]
            },
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
        # 带值的未知字段可能是宿主无法兑现的意图（fluentd-3466 fnnPB3X的type、preact-4245的备注）。
        {"files": [{"operation": "edit", "path": "a.py", "edits": [{**EDIT, "type": "exact"}]}]},
        {"files": [{"operation": "edit", "path": "a.py", "edits": [EDIT], "replace_all": True}]},
        # old_text与new_text相同：可能是已满足，也可能是把新文本误写进两处（babel-14532 VLJ5nVw
        # 下一步用同一old_text改了new_text），去掉它会让模型以为改动已应用。
        {
            "files": [
                {
                    "operation": "edit",
                    "path": "a.py",
                    "edits": [EDIT, {"old_text": "same", "new_text": "same"}],
                }
            ]
        },
        # 同一路径的两项针对不同前镜像，或混有其他操作（rubocop-13705的delete加create）。
        {
            "files": [
                {"operation": "edit", "path": "a.py", "expected_sha256": SHA, "edits": [EDIT]},
                {"operation": "edit", "path": "a.py", "expected_sha256": "6" * 64, "edits": [EDIT]},
            ]
        },
        {
            "files": [
                {"operation": "delete", "path": "a.py", "expected_sha256": SHA},
                {"operation": "create", "path": "a.py", "content": "x", "mode": 420},
            ]
        },
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


class _Workspace:
    """提案修复看到的当前工作区：文件正文与权限位；缺失的路径读不到。"""

    def __init__(self, files=None, modes=None):
        self._files, self._modes = files or {}, modes or {}

    async def read_workspace_file(self, path):
        return self._files.get(path)

    async def workspace_file_mode(self, path):
        return self._modes.get(path)


def _patch(*entries):
    return ToolCallCompleted(
        call_id="p", tool="apply_patch_batch", arguments={"files": list(entries)}
    )


async def test_anchored_edits_get_the_current_sha_and_others_keep_theirs():
    body = b"old\nkeep\n"
    current = hashlib.sha256(body).hexdigest()
    workspace = _Workspace({"a.py": body, "dup.py": b"old\nold\n", "bin.py": b"\xff"})
    stale = {"operation": "edit", "path": "a.py", "expected_sha256": "0" * 64, "edits": [EDIT]}
    missing = {"operation": "edit", "path": "a.py", "edits": [EDIT]}
    duplicate = {**stale, "path": "dup.py"}
    unreadable = {**stale, "path": "gone.py"}
    binary = {**stale, "path": "bin.py"}
    replace = {"operation": "replace", "path": "a.py", "expected_sha256": "0" * 64, "mode": 420}

    filled = await fill_patch_preconditions(
        _patch(stale, missing, duplicate, unreadable, binary, replace), DEFINITIONS, workspace
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


async def test_anchors_that_differ_only_in_line_endings_also_get_the_current_sha():
    # CRLF文件上模型发回的多行old_text是LF换行（Beta RT-06）。补SHA与规划器用同一顺序判定唯一：
    # 先逐字匹配，一处也没有时忽略CRLF/LF差异再匹配。
    crlf = b"a = 1\r\nb = 2\r\nc = 3\r\n"
    # 两处都只在忽略换行时命中：不唯一，不补。
    twice = b"a = 1\r\nb = 2\nz\r\na = 1\nb = 2\r\n"
    # 逐字只命中LF的那一处：与规划器一样以它为准。
    both_styles = b"a = 1\r\nb = 2\r\na = 1\nb = 2\n"
    workspace = _Workspace({"crlf.py": crlf, "twice.py": twice, "both.py": both_styles})

    def stale(path):
        edit = {"old_text": "a = 1\nb = 2\n", "new_text": "a = 9\n"}
        return {"operation": "edit", "path": path, "expected_sha256": "0" * 64, "edits": [edit]}

    filled = await fill_patch_preconditions(
        _patch(stale("crlf.py"), stale("twice.py"), stale("both.py")), DEFINITIONS, workspace
    )

    assert [entry["expected_sha256"] for entry in filled.arguments["files"]] == [
        hashlib.sha256(crlf).hexdigest(),
        "0" * 64,
        hashlib.sha256(both_styles).hexdigest(),
    ]
    # 只补摘要：记录下来的仍是模型发来的old_text与new_text。
    assert [entry["edits"] for entry in filled.arguments["files"]] == [
        stale(name)["edits"] for name in ("crlf.py", "twice.py", "both.py")
    ]


async def test_only_the_workspace_patch_contract_is_filled():
    proposal = _patch({"operation": "edit", "path": "a.py", "edits": [EDIT]})
    workspace = _Workspace({"a.py": b"old\n"})
    current = hashlib.sha256(b"old\n").hexdigest()
    compact = {"apply_patch_batch": workspace_patch_descriptor("compact")}
    filled = await fill_patch_preconditions(proposal, compact, workspace)
    assert filled.arguments["files"][0]["expected_sha256"] == current
    other = PATCH.model_copy(
        update={"input_schema": {"type": "object", "properties": {"patch": {"type": "string"}}}}
    )
    foreign = {"apply_patch_batch": other}
    assert await fill_patch_preconditions(proposal, foreign, workspace) is proposal
    assert await fill_patch_preconditions(proposal, {}, workspace) is proposal
    unchanged = _patch(
        {"operation": "edit", "path": "a.py", "edits": [EDIT], "expected_sha256": current}
    )
    assert await fill_patch_preconditions(unchanged, DEFINITIONS, workspace) is unchanged


SEEN, OTHER = "a" * 64, "b" * 64


async def test_replace_and_delete_without_a_sha_get_the_digest_the_model_last_saw():
    # 回归（babel-15649、preact-3567、rubocop-13705等10次）：整文件替换没带expected_sha256，
    # 而模型此前读过该文件。补的是历史中的摘要而不是当前文件的：文件此后变化照常被拒。
    history = [*read("src/a.py", OTHER), *patched(("src/a.py", SEEN)), *read("tmp.txt", OTHER)]
    replace = {"operation": "replace", "path": "src/a.py", "content": "x\n", "mode": 420}
    delete = {"operation": "delete", "path": "tmp.txt", "expected_sha256": None}

    filled = await fill_patch_preconditions(
        _patch(replace, delete), DEFINITIONS, _Workspace(), history
    )

    assert filled.arguments["files"] == [
        {**replace, "expected_sha256": SEEN},
        {**delete, "expected_sha256": OTHER},
    ]
    WorkspacePatchInput.model_validate_json(json.dumps(filled.arguments))


async def test_a_sha_the_model_gave_and_files_it_never_saw_are_left_alone():
    history = read("src/a.py", SEEN)
    given = {
        "operation": "replace",
        "path": "src/a.py",
        "expected_sha256": OTHER,
        "content": "x\n",
        "mode": 420,
    }
    unseen = {"operation": "replace", "path": "src/new.py", "content": "x\n", "mode": 420}
    create = {"operation": "create", "path": "src/a.py", "content": "x\n", "mode": 420}
    proposal = _patch(given, unseen, create)

    assert await fill_patch_preconditions(proposal, DEFINITIONS, _Workspace(), history) is proposal


async def test_a_missing_path_is_filled_only_when_the_sha_names_one_observed_file():
    # 回归（fluentd-3466 fnnPB3X、hugo-12204 MNYNCbt）：文件项带着读到的SHA却漏了path。
    history = [*read("src/a.py", SEEN), *read("src/b.py", OTHER), *read("src/copy.py", OTHER)]
    unique = {"operation": "edit", "expected_sha256": SEEN, "edits": [EDIT]}
    shared = {"operation": "edit", "expected_sha256": OTHER, "edits": [EDIT]}
    unknown = {"operation": "edit", "expected_sha256": "c" * 64, "edits": [EDIT]}
    bare = {"operation": "edit", "edits": [EDIT]}

    filled = await fill_patch_preconditions(
        _patch(unique, shared, unknown, bare), DEFINITIONS, _Workspace(), history
    )

    assert [entry.get("path") for entry in filled.arguments["files"]] == [
        "src/a.py",
        None,
        None,
        None,
    ]


@pytest.mark.parametrize(
    ("current_mode", "filled_mode"),
    [(0o644, 420), (0o755, 493), (0o664, 420), (0o700, 493), (None, None)],
)
async def test_replace_without_a_mode_keeps_whether_the_file_is_executable(
    current_mode, filled_mode
):
    replace = {
        "operation": "replace",
        "path": "run.sh",
        "expected_sha256": SEEN,
        "content": "x\n",
        "mode": None,
    }
    workspace = _Workspace(modes={"run.sh": current_mode})

    filled = await fill_patch_preconditions(_patch(replace), DEFINITIONS, workspace)

    assert filled.arguments["files"][0]["mode"] == filled_mode


def _grep_descriptor():
    schema = RegexGrepInput.model_json_schema()
    return PATCH.model_copy(update={"name": "grep", "input_schema": schema})


@pytest.mark.parametrize(
    "arguments",
    [
        # 回归（gson-1014 vZzMGTT、preact-4245 gXn7ppg、rubocop-13705 w64GyQh）。
        {"path": "src/a.py", "query": "function getHookState", "context_lines": "10"},
        {"query": "after_send", "include": "base.rb", "max_results": "5", "context_lines": "0"},
    ],
)
def test_read_file_called_with_a_grep_query_becomes_grep(arguments):
    offered = {"read_file": PATCH, "grep": _grep_descriptor()}
    assert reroute_grep_shaped_read("read_file", arguments, offered) == ("grep", arguments)
    proposal = ToolCallCompleted(call_id="g", tool="read_file", arguments=arguments)
    assert repair_proposal(proposal, offered, reroute=True).tool == "grep"
    # 强制工具选择时不改写工具名。
    assert repair_proposal(proposal, offered, reroute=False) is proposal


@pytest.mark.parametrize(
    ("tool", "arguments", "offered"),
    [
        # 带有read_file专属字段：意图不唯一。
        ("read_file", {"path": "a.py", "query": "x", "start_line": 3}, {"grep"}),
        ("read_file", {"path": "a.py", "query": "x", "anchor": "x"}, {"grep"}),
        ("read_file", {"path": "a.py", "context_lines": 3}, {"grep"}),
        # 本步未开放grep，或调用的不是read_file。
        ("read_file", {"path": "a.py", "query": "x"}, set()),
        ("glob", {"path": "a.py", "query": "x"}, {"grep"}),
    ],
)
def test_reads_that_are_not_plain_grep_calls_keep_their_tool(tool, arguments, offered):
    definitions = {name: _grep_descriptor() for name in offered}
    assert reroute_grep_shaped_read(tool, arguments, definitions) is None


def test_read_artifact_paging_is_canonical_before_the_call_is_recorded():
    # 存档分页核验按调用记录中的参数重算：数字字符串必须在调用形成前换算，执行时再修复会让
    # 已成功的读取在核验时被判为存档损坏。
    offered = {"read_artifact": PATCH, "read_file": PATCH}
    text_paging = ToolCallCompleted(
        call_id="a",
        tool="read_artifact",
        arguments={"artifact_id": ARTIFACT, "offset": "0", "limit": "500"},
    )
    repaired = repair_proposal(text_paging, offered, reroute=False)
    assert repaired.arguments == {"artifact_id": ARTIFACT, "offset": 0, "limit": 200}
    misnamed = ToolCallCompleted(
        call_id="b", tool="read_file", arguments={"artifact_id": ARTIFACT, "offset": "40"}
    )
    rerouted = repair_proposal(misnamed, offered, reroute=True)
    assert (rerouted.tool, rerouted.arguments) == (
        "read_artifact",
        {"artifact_id": ARTIFACT, "offset": 40},
    )
    canonical = ToolCallCompleted(
        call_id="c", tool="read_artifact", arguments={"artifact_id": ARTIFACT, "limit": 5}
    )
    assert repair_proposal(canonical, offered, reroute=True) is canonical
    # 本步未开放read_artifact时不改写。
    assert repair_proposal(text_paging, {}, reroute=True) is text_paging
