"""Action返回值与Owner投影的序列化前预算；不替代回调内部工作量限制。"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable
from time import monotonic
from typing import Literal, cast

from pydantic import Field, JsonValue

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import ExecutionContract


class ActionOutputBudget(ExecutionContract):
    """宿主持有的终态返回/投影预算，不能由模型参数或回调返回值修改。"""

    spec_version: Literal["harnessix.action-output-budget/v1"] = "harnessix.action-output-budget/v1"
    max_bytes: int = Field(default=1024 * 1024, ge=1, le=1024 * 1024)
    max_depth: int = Field(default=64, ge=1, le=64)
    max_nodes: int = Field(default=10256, ge=1, le=10256)
    timeout_seconds: float = Field(default=10.0, ge=0.001, le=30.0)


DEFAULT_OUTPUT_BUDGET = ActionOutputBudget()


def projection_checkpoint(cancel: CancelToken, deadline: float) -> None:
    """同步有界处理也检查取消和时限，不依赖事件循环及时运行超时回调。"""

    cancel.checkpoint()
    if monotonic() >= deadline:
        raise KernelError("trusted_action_output_timeout", "Action输出投影超时")


def projection_checkpointer(cancel: CancelToken, deadline: float) -> Callable[[], None]:
    """只识别本次公开处理新增的父Task取消；已处理计数不污染后续对账。"""

    task = asyncio.current_task()
    initial_count = task.cancelling() if task is not None else 0

    def checkpoint() -> None:
        projection_checkpoint(cancel, deadline)
        if task is not None and task.cancelling() > initial_count:
            raise asyncio.CancelledError

    return checkpoint


def _string_bytes(value: str, remaining: int) -> int:
    """先限制字符数，再计算转义后的UTF-8长度，避免先分配无界字符串。"""

    if len(value) > remaining:
        raise KernelError("trusted_action_output_limit", "Action输出投影超过资源上限")
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _scalar_bytes(value: object, remaining: int) -> int:
    """只处理精确原生标量，不调用扩展对象的字符串化或序列化方法。"""

    if type(value) is str:
        return _string_bytes(value, remaining)
    if value is None:
        return 4
    if type(value) is bool:
        return 4 if value else 5
    if type(value) is int:
        if value.bit_length() > 128:
            raise KernelError("trusted_action_output_limit", "Action输出投影超过资源上限")
        return len(str(value))
    if type(value) is float and math.isfinite(value):
        return len(json.dumps(value, allow_nan=False))
    raise KernelError("trusted_action_output_mismatch", "Action输出与审计终态不匹配")


def _inspect_projection(
    value: object, budget: ActionOutputBudget, cancel: CancelToken, deadline: float
) -> None:
    """迭代预检；在扩展栈前检查节点预算，退出标记区分环与合法共享子树。"""

    stack: list[tuple[object, int, bool]] = [(value, 1, False)]
    active: set[int] = set()
    scheduled, size = 1, 0
    while stack:
        projection_checkpoint(cancel, deadline)
        item, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(item))
            continue
        if depth > budget.max_depth:
            raise KernelError("trusted_action_output_limit", "Action输出投影超过资源上限")
        if type(item) is dict or type(item) is list:
            container = cast(dict[str, object] | list[object], item)
            if id(item) in active:
                raise KernelError("trusted_action_output_mismatch", "Action输出与审计终态不匹配")
            count = len(container)
            scheduled += count * (2 if type(item) is dict else 1)
            if scheduled > budget.max_nodes:
                raise KernelError("trusted_action_output_limit", "Action输出投影超过资源上限")
            size += 2 + max(0, count - 1) + (count if type(item) is dict else 0)
            active.add(id(item))
            stack.append((item, depth, True))
            if type(item) is dict:
                for key, child in cast(dict[object, object], item).items():
                    if type(key) is not str:
                        raise KernelError(
                            "trusted_action_output_mismatch", "Action输出与审计终态不匹配"
                        )
                    stack.extend(((key, depth + 1, False), (child, depth + 1, False)))
            else:
                stack.extend((child, depth + 1, False) for child in cast(list[object], item))
        else:
            size += _scalar_bytes(item, budget.max_bytes - size)
        if size > budget.max_bytes:
            raise KernelError("trusted_action_output_limit", "Action输出投影超过资源上限")


def bounded_projection(
    value: object, *, budget: ActionOutputBudget, cancel: CancelToken, deadline: float
) -> JsonValue:
    """预检后复制原生JSON；仅有界数据进入Pydantic、摘要及Tool Result构造。"""

    try:
        _inspect_projection(value, budget, cancel, deadline)
        projection_checkpoint(cancel, deadline)
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        # 独立核对计数；不依赖预检算法作为唯一大小证明。
        if len(encoded.encode("utf-8")) > budget.max_bytes:
            raise KernelError("trusted_action_output_limit", "Action输出投影超过资源上限")
        cloned = cast(JsonValue, json.loads(encoded))
        projection_checkpoint(cancel, deadline)
        return cloned
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise KernelError("trusted_action_output_mismatch", "Action输出与审计终态不匹配") from None
