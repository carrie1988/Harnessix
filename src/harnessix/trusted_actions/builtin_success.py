"""内置及标准扩展成功摘要的字段权限与计划绑定；不授予任意JSON公开权。"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import JsonValue

from harnessix.execution.public_tool_contracts import (
    GitPushReceipt,
    McpToolCallOutput,
    PublicWorkspacePatchOutput,
    SkillContent,
    SkillResourceContent,
)
from harnessix.trusted_actions.contracts import ActionRoutePlan


def validate_builtin_success(
    plan: ActionRoutePlan,
    family: Literal["patch", "git", "mcp", "skill"],
    output: JsonValue,
) -> None:
    """只在上游原生预算后验证；使用原JSON计算审计摘要，不回写DTO默认字段。"""

    encoded = json.dumps(output, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    arguments = plan.invocation.arguments
    if family == "patch":
        patch = PublicWorkspacePatchOutput.model_validate_json(encoded)
        files = arguments.get("files")
        count = len(files) if type(files) is list else None
        if plan.binding.tool == "rollback_workspace_patch":
            count = sum(
                resource.kind == "workspace" and resource.access == "write"
                for resource in plan.resources
            )
        if patch.transaction_id != plan.execution.plan_id or patch.files != count:
            raise ValueError("公开Patch摘要不属于当前计划")
    elif family == "git":
        receipt = GitPushReceipt.model_validate_json(encoded)
        expected = (
            arguments.get("push_id"),
            arguments.get("remote_name"),
            arguments.get("remote_ref"),
            arguments.get("local_oid"),
            arguments.get("remote_url_sha256"),
        )
        actual = (
            str(receipt.push_id),
            receipt.remote_name,
            receipt.remote_ref,
            receipt.remote_oid,
            receipt.remote_url_sha256,
        )
        if actual != expected:
            raise ValueError("公开Git收据不属于当前Push意图")
    elif family == "mcp":
        result = McpToolCallOutput.model_validate_json(encoded)
        if result.is_error:
            raise ValueError("成功MCP结果不能携带错误终态")
    else:
        content: SkillContent | SkillResourceContent
        if plan.binding.tool == "skill.load":
            content = SkillContent.model_validate_json(encoded)
        else:
            content = SkillResourceContent.model_validate_json(encoded)
            if content.path != arguments.get("path"):
                raise ValueError("公开Skill资源不属于当前请求路径")
        name = arguments.get("name")
        qualified = content.qualified_name
        if (
            content.catalog_sha256 != arguments.get("catalog_sha256")
            or content.manifest_sha256 != arguments.get("expected_manifest_sha256")
            or type(name) is not str
            or (qualified != name if "/" in name else qualified.rsplit("/", 1)[-1] != name)
        ):
            raise ValueError("公开Skill正文不属于当前目录及清单")
