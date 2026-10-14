"""仅宿主可见的单响应工具互斥约束；不修改模型或Session合同。"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from harnessix.agent.errors import KernelError


def validate_exclusive_tool_groups(
    groups: tuple[tuple[str, ...], ...], advertised_names: Collection[str]
) -> None:
    """不隐式转换或修正规则；重复组按成员集合判断，与声明顺序无关。"""
    if type(groups) is not tuple:
        raise KernelError("model_tool_view_invalid", "模型工具互斥组契约无效")
    seen: set[frozenset[str]] = set()
    for group in groups:
        if (
            type(group) is not tuple
            or len(group) < 2
            or any(
                type(name) is not str or not 1 <= len(name) <= 256 or name not in advertised_names
                for name in group
            )
        ):
            raise KernelError("model_tool_view_invalid", "模型工具互斥组契约无效")
        members = frozenset(group)
        if len(members) != len(group) or members in seen:
            raise KernelError("model_tool_view_invalid", "模型工具互斥组契约无效")
        seen.add(members)


def require_exclusive_tool_calls(
    tool_names: Iterable[str], groups: tuple[tuple[str, ...], ...]
) -> None:
    """完整响应发布前一次校验；同名重复调用不构成不同工具冲突。"""
    if not groups:
        return
    proposed = set(tool_names)
    if any(len(proposed.intersection(group)) > 1 for group in groups):
        raise KernelError("invalid_provider_output", "模型响应包含互斥工具调用")
