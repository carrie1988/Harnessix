"""提案阶段补丁修复走真实产品路径：精确锚定的edit由宿主补SHA，其余前置条件照旧拒绝。"""

from __future__ import annotations

import hashlib
import json

import pytest

from harnessix.agent.models import (
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    TurnStatus,
)
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from tests.agent.helpers import answer
from tests.product_config.test_product_patch_rollback import approve, product, result

STALE = "0" * 64
PRECONDITION = "workspace_patch_precondition_failed"


def raw_patch_step(arguments):
    return [
        ResponseStarted(response_id="patch-response"),
        ToolCallCompleted(call_id="patch-call", tool="apply_patch_batch", arguments=arguments),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


async def run_patch(runtime, provider, root, arguments):
    provider.steps = (raw_patch_step(arguments), answer("已处理"))
    thread = await runtime.create_thread(str(root))
    turn = await runtime.run_turn(thread.thread_id, "修改文件", request_id="patch")
    call = next(item.content for item in turn.items if isinstance(item.content, ToolCallContent))
    return thread, turn, call


def edit(old_text, new_text, sha=STALE, **extra):
    return {
        "operation": "edit",
        "path": "src/modified.py",
        "expected_sha256": sha,
        "edits": [{"old_text": old_text, "new_text": new_text}],
        **extra,
    }


@pytest.mark.parametrize(
    "arguments",
    [
        # 过期SHA：模型刚改过文件后沿用旧摘要（holdout中15次SHA不符的主要来源）。
        {"files": [edit("old", "new")]},
        # 缺SHA、附带默认mode与空content。
        {"files": [edit("old", "new", sha=None, mode="420", content=None)]},
        # 单文件字段写在外层。
        {
            "operation": "edit",
            "path": "src/modified.py",
            "mode": 420,
            "files": [{"edits": [{"old_text": "old", "new_text": "new"}]}],
        },
    ],
)
async def test_anchored_edit_gets_the_current_sha_and_still_needs_approval(tmp_path, arguments):
    async with product(tmp_path) as (root, runtime, provider, *_):
        thread, waiting, call = await run_patch(runtime, provider, root, arguments)

        assert waiting.status is TurnStatus.WAITING_APPROVAL
        # 记录与审批的是规范调用：宿主观察到的当前SHA，执行前仍按它核对。
        current = hashlib.sha256(b"old\n").hexdigest()
        assert call.arguments == {
            "files": [
                {
                    "operation": "edit",
                    "path": "src/modified.py",
                    "expected_sha256": current,
                    "edits": [{"old_text": "old", "new_text": "new"}],
                }
            ]
        }
        completed = await approve(runtime, thread.thread_id, waiting)
        assert result(completed).outcome == "succeeded"
        assert (root / "src/modified.py").read_bytes() == b"new\n"


@pytest.mark.parametrize(
    "arguments",
    [
        # 字符串化的files与外层单文件字段：还原必须先于结构修复，否则修复看不到数组。
        {
            "path": "src/modified.py",
            "files": json.dumps([{"edits": [{"old_text": "old", "new_text": "new"}]}]),
        },
        # 字符串化的files里带默认mode：修复去掉mode并补SHA。
        {"files": json.dumps([edit("old", "new", mode=420)])},
    ],
)
async def test_stringified_patch_arguments_are_restored_before_repair(tmp_path, arguments):
    async with product(tmp_path, normalize_arguments=True) as (root, runtime, provider, *_):
        _, waiting, call = await run_patch(runtime, provider, root, arguments)

        assert waiting.status is TurnStatus.WAITING_APPROVAL
        assert call.arguments == {
            "files": [
                {
                    "operation": "edit",
                    "path": "src/modified.py",
                    "expected_sha256": hashlib.sha256(b"old\n").hexdigest(),
                    "edits": [{"old_text": "old", "new_text": "new"}],
                }
            ]
        }


async def test_ambiguous_anchor_keeps_the_model_sha_and_is_rejected(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, *_):
        (root / "src/modified.py").write_bytes(b"old\nold\n")
        _, turn, call = await run_patch(runtime, provider, root, {"files": [edit("old", "new")]})

        assert call.arguments["files"][0]["expected_sha256"] == STALE
        output = result(turn)
        assert output.outcome == "failed" and output.error.code == PRECONDITION
        assert not any(
            isinstance(item.content, TrustedActionApprovalRequestContent) for item in turn.items
        )
        assert (root / "src/modified.py").read_bytes() == b"old\nold\n"


async def test_replace_with_a_stale_sha_is_still_rejected(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, *_):
        replace = {
            "operation": "replace",
            "path": "src/modified.py",
            "expected_sha256": STALE,
            "content": "new\n",
            "mode": 420,
        }
        _, turn, call = await run_patch(runtime, provider, root, {"files": [replace]})

        assert call.arguments == {"files": [replace]}
        output = result(turn)
        assert output.outcome == "failed" and output.error.code == PRECONDITION
        assert (root / "src/modified.py").read_bytes() == b"old\n"


def tool_step(tool, call_id, **arguments):
    return [
        ResponseStarted(response_id=f"{call_id}-response"),
        ToolCallCompleted(call_id=call_id, tool=tool, arguments=arguments),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


def patch_result(turn):
    """本轮最后一个工具结果：前面可能有read_file等只读调用。"""
    return [item.content for item in turn.items if isinstance(item.content, ToolResultContent)][-1]


def patch_call(turn):
    calls = [item.content for item in turn.items if isinstance(item.content, ToolCallContent)]
    return next(call for call in calls if call.tool == "apply_patch_batch")


def replace_without_sha(**extra):
    return {"operation": "replace", "path": "src/modified.py", "content": "new\n", **extra}


async def test_replace_without_a_sha_uses_the_digest_of_the_models_last_read(tmp_path):
    # 回归（babel-15649 AyJ2RqG、preact-3567 gKqcYKS、rubocop-13705 w64GyQh等10次）：模型读过文件后
    # 整文件替换，漏了expected_sha256与mode。宿主补上它读到的摘要与现有权限，仍需审批。
    async with product(tmp_path) as (root, runtime, provider, *_):
        (root / "src/modified.py").chmod(0o755)
        provider.steps = (
            tool_step("read_file", "read", path="src/modified.py"),
            tool_step("apply_patch_batch", "patch", files=[replace_without_sha(mode=None)]),
            answer("已处理"),
        )
        thread = await runtime.create_thread(str(root))
        waiting = await runtime.run_turn(thread.thread_id, "重写文件", request_id="replace")

        assert waiting.status is TurnStatus.WAITING_APPROVAL
        assert patch_call(waiting).arguments == {
            "files": [
                replace_without_sha(expected_sha256=hashlib.sha256(b"old\n").hexdigest(), mode=493)
            ]
        }
        completed = await approve(runtime, thread.thread_id, waiting)
        assert patch_result(completed).outcome == "succeeded"
        assert (root / "src/modified.py").read_bytes() == b"new\n"
        assert (root / "src/modified.py").stat().st_mode & 0o777 == 0o755


async def test_replace_without_a_sha_is_rejected_when_the_file_changed_after_the_read(tmp_path):
    # 补的是模型读到的摘要而不是当前文件的：读取之后被命令改过的文件不会被盲目覆盖。
    async with product(tmp_path) as (root, runtime, provider, *_):
        provider.steps = (tool_step("read_file", "read", path="src/modified.py"), answer("已读"))
        thread = await runtime.create_thread(str(root))
        await runtime.run_turn(thread.thread_id, "读取文件", request_id="read")
        (root / "src/modified.py").write_bytes(b"changed by a command\n")
        provider.steps = (
            tool_step("apply_patch_batch", "patch", files=[replace_without_sha(mode=420)]),
            answer("已处理"),
        )
        turn = await runtime.run_turn(thread.thread_id, "重写文件", request_id="replace")

        output = patch_result(turn)
        assert output.outcome == "failed" and output.error.code == PRECONDITION
        assert output.error.message.startswith(
            "files[0]: expected_sha256 does not match the current file"
        )
        assert (root / "src/modified.py").read_bytes() == b"changed by a command\n"


async def test_replace_of_a_file_the_model_never_read_names_the_missing_digest(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, *_):
        _, turn, call = await run_patch(
            runtime, provider, root, {"files": [replace_without_sha(mode=420)]}
        )

        assert "expected_sha256" not in call.arguments["files"][0]
        output = result(turn)
        assert output.outcome == "failed" and output.error.code == "tool_invalid_arguments"
        assert "files[0]: this replace entry is missing expected_sha256" in output.error.message
        assert "content_sha256 that read_file returned" in output.error.message
        assert (root / "src/modified.py").read_bytes() == b"old\n"


OTHER = b"a = 1\nb = 2\na = 1\n"


def two_file_patch(*other_edits):
    return {
        "files": [
            edit("old", "new", sha=hashlib.sha256(b"old\n").hexdigest()),
            {
                "operation": "edit",
                "path": "src/other.py",
                "expected_sha256": hashlib.sha256(OTHER).hexdigest(),
                "edits": [{"old_text": old, "new_text": new} for old, new in other_edits],
            },
        ]
    }


@pytest.mark.parametrize(
    ("other_edits", "code", "message"),
    [
        # 回归（fmt-2457 5KyxPNq step 65等6次）：原消息只有“编辑锚点不唯一，请提供更完整上下文”。
        (
            [("b = 2", "b = 3"), ("a = 1", "a = 9")],
            "patch_ambiguous_context",
            "files[1].edits[1]: old_text matches more than one place (lines 1, 3); add the line "
            "before or after the place you mean to old_text and new_text until it matches once",
        ),
        (
            [("b = 5", "b = 3")],
            "patch_context_not_found",
            "files[1].edits[0]: old_text was not found in the file, and neither was its first "
            "non-blank line; read_file the part you want to change and copy old_text from the "
            "current text",
        ),
        (
            [("a = 1\nb = 2", "a = 0\nb = 2"), ("b = 2\na = 1", "b = 2\na = 3")],
            "patch_overlapping_edits",
            "files[1].edits[0] and edits[1] overlap: both old_text cover line 2; merge them into "
            "one edit",
        ),
    ],
)
async def test_a_rejected_edit_names_its_file_and_edit_and_what_the_file_has(
    tmp_path, other_edits, code, message
):
    async with product(tmp_path) as (root, runtime, provider, *_):
        (root / "src/other.py").write_bytes(OTHER)
        _, turn, _ = await run_patch(runtime, provider, root, two_file_patch(*other_edits))

        output = result(turn)
        assert output.outcome == "failed" and output.error.code == code
        assert output.error.message == message
        # 事务语义不变：一处被拒，整批都不应用，也不进入审批。
        assert not any(
            isinstance(item.content, TrustedActionApprovalRequestContent) for item in turn.items
        )
        assert (root / "src/modified.py").read_bytes() == b"old\n"
        assert (root / "src/other.py").read_bytes() == OTHER


@pytest.mark.parametrize(
    ("entry", "code", "message"),
    [
        # 回归（lucene-13494 phnSs4e step 28等6次）：把局部片段当作replace的完整content。
        (
            {
                "operation": "replace",
                "path": "src/big.py",
                "expected_sha256": hashlib.sha256(b"line\n" * 40).hexdigest(),
                "content": "line\n",
                "mode": 420,
            },
            "workspace_patch_unsafe_full_replace",
            "files[0]: replace content is 5 bytes but the file is 200 bytes",
        ),
        # 回归（preact-2927 XLvETTb step 14等4次）。
        (
            {"operation": "create", "path": "src/big.py", "content": "x\n", "mode": 420},
            "workspace_patch_target_exists",
            "files[0]: create targets a path that already exists",
        ),
        (
            {
                "operation": "delete",
                "path": "src/absent.py",
                "expected_sha256": STALE,
            },
            "workspace_patch_target_missing",
            "files[0]: delete targets a path that is not an existing regular file",
        ),
        # 回归（fmt-3901 JLRTfoR step 18等5次）：工作区以外的绝对路径。
        (
            {"operation": "create", "path": "/tmp/scratch.py", "content": "x\n", "mode": 420},
            "workspace_path_denied",
            "A path in files is not a workspace path",
        ),
    ],
)
async def test_remaining_patch_rejections_say_what_was_found_and_the_next_step(
    tmp_path, entry, code, message
):
    async with product(tmp_path) as (root, runtime, provider, *_):
        (root / "src/big.py").write_bytes(b"line\n" * 40)
        _, turn, _ = await run_patch(runtime, provider, root, {"files": [entry]})

        output = result(turn)
        assert output.outcome == "failed" and output.error.code == code
        assert output.error.message.startswith(message) and output.error.message.isascii()
        assert (root / "src/big.py").read_bytes() == b"line\n" * 40


async def test_two_edit_entries_for_one_file_are_applied_as_one(tmp_path):
    # 回归（gson-2061 mWKyQ4E、preact-4245 gXn7ppg）：同一文件的两处编辑分成了两个文件项，
    # 原来只得到不含任何位置的“参数不符合契约”。
    async with product(tmp_path) as (root, runtime, provider, *_):
        (root / "src/modified.py").write_bytes(b"old\nkeep\n")
        sha = hashlib.sha256(b"old\nkeep\n").hexdigest()
        arguments = {"files": [edit("old", "new", sha=sha), edit("keep", "kept", sha=None)]}
        thread, waiting, call = await run_patch(runtime, provider, root, arguments)

        assert waiting.status is TurnStatus.WAITING_APPROVAL
        assert call.arguments["files"] == [
            {
                "operation": "edit",
                "path": "src/modified.py",
                "expected_sha256": sha,
                "edits": [
                    {"old_text": "old", "new_text": "new"},
                    {"old_text": "keep", "new_text": "kept"},
                ],
            }
        ]
        completed = await approve(runtime, thread.thread_id, waiting)
        assert result(completed).outcome == "succeeded"
        assert (root / "src/modified.py").read_bytes() == b"new\nkept\n"


async def test_an_edit_that_changes_nothing_is_reported_not_silently_dropped(tmp_path):
    # babel-14532 VLJ5nVw：模型随后用同一old_text改了new_text，说明它本想修改；去掉该编辑
    # 而让整批成功，会让它以为改动已经应用。
    async with product(tmp_path) as (root, runtime, provider, *_):
        entry = edit("old", "new")
        entry["edits"].append({"old_text": "same", "new_text": "same"})
        _, turn, _ = await run_patch(runtime, provider, root, {"files": [entry]})

        output = result(turn)
        assert output.outcome == "failed" and output.error.code == "tool_invalid_arguments"
        assert "files[0].edits[1]: old_text and new_text are identical" in output.error.message
        assert (root / "src/modified.py").read_bytes() == b"old\n"
