"""内置及标准扩展成功摘要的字段权限与计划绑定；不授予任意JSON公开权。"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import JsonValue

from harnessix.execution.contracts import canonical_digest
from harnessix.execution.public_tool_contracts import (
    GitPushReceipt,
    McpToolCallOutput,
    PublicWorkspacePatchOutput,
    PublicWorkspacePatchOutputV2,
    SkillContent,
    SkillResourceContent,
)
from harnessix.trusted_actions.contracts import ActionRoutePlan


def skill_name_resource_identifier(
    catalog_sha256: str, name: object, path: object = None
) -> dict[str, object]:
    """模型入口只给名称时，规划阶段冻结到资源的目录身份。"""
    identifier: dict[str, object] = {"catalog": catalog_sha256, "name": name}
    if path is not None:
        identifier["path"] = path
    return identifier


def validate_builtin_success(
    plan: ActionRoutePlan,
    family: Literal["patch", "git", "mcp", "skill"],
    output: JsonValue,
) -> None:
    """只在上游原生预算后验证；使用原JSON计算审计摘要，不回写DTO默认字段。"""

    encoded = json.dumps(output, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    arguments = plan.invocation.arguments
    if family == "patch":
        _validate_patch_summary(plan, output, encoded)
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
            not _skill_content_is_planned(plan, content)
            or type(name) is not str
            or (qualified != name if "/" in name else qualified.rsplit("/", 1)[-1] != name)
        ):
            raise ValueError("公开Skill正文不属于当前目录及清单")


def _skill_content_is_planned(
    plan: ActionRoutePlan, content: SkillContent | SkillResourceContent
) -> bool:
    """显式调用核对入参摘要；模型入口只给名称，核对规划时冻结到资源的目录。"""
    arguments = plan.invocation.arguments
    if "catalog_sha256" in arguments:
        return content.catalog_sha256 == arguments.get(
            "catalog_sha256"
        ) and content.manifest_sha256 == arguments.get("expected_manifest_sha256")
    planned = canonical_digest(
        skill_name_resource_identifier(
            content.catalog_sha256, arguments.get("name"), arguments.get("path")
        )
    )
    return any(item.identifier_sha256 == planned for item in plan.resources)


def _validate_patch_summary(plan: ActionRoutePlan, output: JsonValue, encoded: str) -> None:
    """按spec_version选择闭合v1/v2合同；v2每个文件路径须正好对应计划的一个Workspace写资源。"""

    v2 = isinstance(output, dict) and output.get("spec_version") == (
        "harnessix.workspace-patch-output/v2"
    )
    patch = (
        PublicWorkspacePatchOutputV2.model_validate_json(encoded)
        if v2
        else PublicWorkspacePatchOutput.model_validate_json(encoded)
    )
    written = sorted(
        resource.identifier_sha256
        for resource in plan.resources
        if resource.kind == "workspace" and resource.access == "write"
    )
    files = plan.invocation.arguments.get("files")
    count = len(files) if type(files) is list else None
    if plan.binding.tool == "rollback_workspace_patch":
        count = len(written)
    if patch.transaction_id != plan.execution.plan_id or patch.files != count:
        raise ValueError("公开Patch摘要不属于当前计划")
    if isinstance(patch, PublicWorkspacePatchOutputV2) and written != sorted(
        canonical_digest({"location": "workspace", "path": change.path}) for change in patch.changes
    ):
        raise ValueError("公开Patch文件不属于当前计划")
