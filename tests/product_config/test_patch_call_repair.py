"""提案阶段补丁修复走真实产品路径：精确锚定的edit由宿主补SHA，其余前置条件照旧拒绝。"""

from __future__ import annotations

import hashlib
import json

import pytest

from harnessix.agent.models import (
    ToolCallContent,
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
