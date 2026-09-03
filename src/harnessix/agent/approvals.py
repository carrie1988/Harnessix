from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import UUID

from harnessix.agent.models import ApprovalRequestContent, Item, Thread, ToolCallContent, Turn
from harnessix.domain.models import ToolDescriptor, utc_now

READ_ONLY_POLICY_VERSION = "kernel-read-only/v1"


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def tool_fingerprint(definition: ToolDescriptor) -> str:
    return _fingerprint(definition.model_dump(mode="json"))


def request_fingerprint(
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    *,
    policy_version: str = READ_ONLY_POLICY_VERSION,
) -> str:
    """绑定当前只读契约；不是 Workspace 内容哈希或未来的 OS 授权凭证。"""
    return execution_fingerprint(
        thread.thread_id, turn.turn_id, thread.workspace, call, policy_version=policy_version
    )


def execution_fingerprint(
    thread_id: UUID,
    turn_id: UUID,
    workspace: str,
    call: ToolCallContent,
    *,
    policy_version: str = READ_ONLY_POLICY_VERSION,
) -> str:
    """审批和进程内作用域共享同一摘要格式，不引入第二套权限。"""
    return _fingerprint(
        {
            "policy": policy_version,
            "thread_id": str(thread_id),
            "turn_id": str(turn_id),
            "workspace": workspace,
            "call": call.model_dump(mode="json"),
        }
    )


def approval_for(turn: Turn, call: ToolCallContent) -> Item | None:
    return next(
        (
            item
            for item in turn.items
            if isinstance(item.content, ApprovalRequestContent)
            and item.content.call_id == call.call_id
        ),
        None,
    )


def remaining_seconds(turn: Turn) -> float:
    # 持久墙钟截止时间：暂停、离线和重启均不刷新原 Turn 的预算。
    return (
        turn.created_at + timedelta(seconds=turn.budget.timeout_seconds) - utc_now()
    ).total_seconds()
