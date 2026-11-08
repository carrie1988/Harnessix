"""提案阶段的确定性调用修复：把模型常见的错误调用改写为等价的正式调用。

只在调用形成前改写，依据是评测中被拒调用的实际分布；改写后的规范调用才进入审批、审计与执行，
不放宽任何校验。无法唯一确定意图的调用原样保留，由工具原有的严格校验照旧拒绝并给出提示。
"""

from __future__ import annotations

from collections.abc import Container

from pydantic import JsonValue

from harnessix.models.contracts import ToolCallCompleted, ToolCallRejected
from harnessix.tools.argument_repair import ARTIFACT_ID, is_artifact_read

_ARTIFACT_PAGE_FIELDS = ("offset", "limit")
_MAX_ARTIFACT_PAGE = 200


def repair_proposal(
    proposal: ToolCallCompleted | ToolCallRejected, offered: Container[str]
) -> ToolCallCompleted | ToolCallRejected:
    """返回修复后的提案；offered是本步可执行的工具名，修复不得调用其外的工具。"""
    if not isinstance(proposal, ToolCallCompleted):
        return proposal
    rerouted = reroute_artifact_read(proposal.tool, proposal.arguments, offered)
    if rerouted is None:
        return proposal
    tool, arguments = rerouted
    return proposal.model_copy(update={"tool": tool, "arguments": arguments})


def reroute_artifact_read(
    tool: str, arguments: dict[str, JsonValue], offered: Container[str]
) -> tuple[str, dict[str, JsonValue]] | None:
    """read_file携带存档编号与分页参数时改为read_artifact；本步未开放read_artifact时不改道。

    path若是工作区路径（与存档编号不同）则意图不唯一，不改道；页大小超过上限时截到上限。
    """
    if tool != "read_file" or "read_artifact" not in offered or not is_artifact_read(arguments):
        return None
    path = arguments.get("path")
    artifact_id = arguments.get("artifact_id", path)
    if (
        not isinstance(artifact_id, str)
        or ARTIFACT_ID.match(artifact_id) is None
        or (path is not None and path != artifact_id)
        or arguments.keys() - {"artifact_id", "path", *_ARTIFACT_PAGE_FIELDS}
    ):
        return None
    rerouted: dict[str, JsonValue] = {"artifact_id": artifact_id}
    for field in _ARTIFACT_PAGE_FIELDS:
        if field in arguments:
            rerouted[field] = arguments[field]
    limit = rerouted.get("limit")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > _MAX_ARTIFACT_PAGE:
        rerouted["limit"] = _MAX_ARTIFACT_PAGE
    return "read_artifact", rerouted
