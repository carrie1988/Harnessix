"""结构化失败结果的有限公开策略；执行身份可信不等于返回正文可公开。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Literal, cast

from pydantic import JsonValue

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.domain.models import ToolDescriptor
from harnessix.domain.public_output_schema import (
    capture_public_output_schema,
    validate_public_output,
)
from harnessix.execution.contracts import canonical_digest
from harnessix.processes.public_output import (
    PublicEvalOutputSummary,
    PublicProcessOutputSummary,
    PublicProcessOutputSummaryV2,
)
from harnessix.trusted_actions.builtin_success import validate_builtin_success
from harnessix.trusted_actions.contracts import ActionExecutionOutcome, ActionRoutePlan
from harnessix.trusted_actions.public_errors import PublicActionStage

FailureFamily = Literal["custom", "patch", "git", "process", "eval", "mcp", "skill"]
PUBLIC_FAILURE_POLICY_VERSION = "harnessix.public-action-failure/v2"

# 内核生成的分类同样需要在恢复投影时保留；不接受任意error_code前缀。
_COMMON_CODES = frozenset(
    {
        "executor_output_invalid",
        "executor_output_limit",
        "executor_output_timeout",
        "write_output_invalid_unknown",
        "write_output_limit_unknown",
        "write_output_timeout_unknown",
        "reconciliation_output_invalid",
        "reconciliation_output_limit",
        "reconciliation_output_timeout",
        "action_failed",
        "action_effect_unknown",
        "action_manual_intervention",
        "action_failure_output_invalid",
        "executor_timeout",
        "write_effect_timeout_unknown",
        "executor_cancelled",
        "cancelled_write_effect_unknown",
        "executor_error",
        "unexpected_write_error",
        "uncertain_external_effect",
        "reconciliation_timeout",
        "reconciliation_cancelled",
        "reconciliation_error",
        "reconciliation_not_supported",
        "reconciliation_attempts_exhausted",
        "host_interrupted",
        "approval_rejected",
    }
)
_PROCESS_STOPS = {
    "timeout": "process_timeout",
    "cancelled": "process_cancelled",
    "output_limit": "process_output_limit",
    "input_limit": "process_input_limit",
    "io_error": "process_io_error",
    "closed": "process_closed",
    "cleanup_failed": "process_cleanup_failed",
    "host_lost": "process_host_lost",
    "unknown": "process_state_unknown",
    "launch_failed": "process_launch_failed",
}
_PROCESS_CODES = frozenset(
    {
        *_PROCESS_STOPS.values(),
        "process_nonzero_exit",
        "process_failed",
        "process_output_unavailable",
        "process_state_unavailable",
        "process_preflight_failed",
        "process_effect_unknown",
    }
)
_PATCH_EXECUTE = frozenset({"delivery_not_applied", "delivery_effect_unknown"})
_PATCH_RECONCILE = _PATCH_EXECUTE | frozenset(
    {
        "delivery_partial_effect",
        "delivery_plan_missing",
        "delivery_source_changed",
    }
)
# 只来自Push及其实际仓库绑定/固定命令调用链，不借用Commit/Worktree错误全集。
_GIT_SHARED = frozenset(
    {
        "git_push_action_mismatch",
        "git_push_input_invalid",
        "git_repository_changed",
        "git_remote_changed",
        "git_remote_url_invalid",
        "git_remote_output_invalid",
        "git_repository_invalid",
        "git_config_unsupported",
        "git_filter_unsupported",
        "git_sparse_checkout_unsupported",
        "git_tree_invalid",
        "git_tree_unsupported",
        "git_attributes_unsupported",
        "git_object_format_unsupported",
        "git_alternates_unsupported",
        "delivery_dirty_conflict",
        "git_executable_changed",
        "git_executable_invalid",
        "git_binding_changed",
        "git_protocol_invalid",
        "git_process_failed",
        "git_command_failed",
        "git_output_invalid",
    }
)
_MCP_EXECUTE = frozenset(
    {
        "mcp_sandbox_binding_changed",
        "mcp_connection_closed",
        "mcp_connection_unavailable",
        "mcp_connection_failed",
        "mcp_tool_not_found",
        "mcp_tool_contract_changed",
        "mcp_tool_schema_changed",
        "mcp_catalog_invalid",
        "mcp_catalog_timeout",
        "mcp_tool_timeout",
        "mcp_tool_connection_lost",
        "mcp_input_required_unsupported",
        "mcp_result_invalid",
        "mcp_result_schema_invalid",
        "mcp_result_too_large",
        "mcp_tool_error",
    }
)
_SKILL_EXECUTE = frozenset(
    {
        "skill_catalog_not_found",
        "skill_store_corrupt",
        "skill_catalog_mismatch",
        "skill_name_conflict",
        "skill_not_found",
        "skill_contract_changed",
        "skill_content_changed",
        "skill_source_changed",
        "skill_source_unavailable",
        "skill_content_limit",
        "skill_resource_limit",
        "skill_resource_not_found",
        "skill_resource_path_denied",
        "skill_resource_invalid_utf8",
        "skill_path_denied",
        "skill_read_failed",
        "skill_manifest_invalid",
        "skill_invalid_utf8",
        "skill_frontmatter_missing",
        "skill_frontmatter_limit",
        "skill_frontmatter_invalid",
        "skill_content_empty",
        "secret_redaction_failed",
    }
)
_FAILURE_CODES: dict[FailureFamily, dict[PublicActionStage, frozenset[str]]] = {
    "custom": {"execute": frozenset(), "reconcile": frozenset()},
    "patch": {"execute": _PATCH_EXECUTE, "reconcile": _PATCH_RECONCILE},
    "git": {
        "execute": _GIT_SHARED
        | frozenset(
            {
                "git_push_local_changed",
                "git_push_remote_changed",
                "git_push_non_fast_forward",
                "git_push_rejected",
                "git_push_uncertain",
            }
        ),
        "reconcile": _GIT_SHARED | frozenset({"git_push_not_applied", "git_push_remote_diverged"}),
    },
    "process": {
        "execute": _PROCESS_CODES,
        "reconcile": _PROCESS_CODES
        | frozenset({"process_not_started", "process_reconciliation_failed"}),
    },
    "eval": {
        "execute": _PROCESS_CODES - {"process_nonzero_exit"},
        "reconcile": (_PROCESS_CODES - {"process_nonzero_exit"})
        | frozenset({"process_not_started", "process_reconciliation_failed"}),
    },
    "mcp": {"execute": _MCP_EXECUTE, "reconcile": frozenset({"mcp_reconciliation_not_supported"})},
    "skill": {
        "execute": _SKILL_EXECUTE,
        "reconcile": frozenset({"skill_reconciliation_not_supported"}),
    },
}
_FALLBACK_CODES = {
    "failed": "action_failed",
    "unknown": "action_effect_unknown",
    "manual_intervention": "action_manual_intervention",
}


def failure_family(plan: ActionRoutePlan) -> FailureFamily:
    """从宿主已冻结Binding选择合同，不从错误码、正文或模型参数选择公开权限。"""

    binding = plan.binding
    if binding.source == "builtin" and binding.source_id == "harnessix.product":
        identity = (binding.tool, binding.executor_id)
        if identity in {
            ("apply_patch_batch", "product.workspace-patch"),
            ("rollback_workspace_patch", "product.workspace-patch-rollback"),
        }:
            return "patch"
        if identity == ("git.push", "delivery.git-push"):
            return "git"
        if identity == ("run_tests", "eval.run-tests"):
            return "eval"
        profile = binding.executor_id.removeprefix("product.process-profile.")
        if (
            binding.executor_id == f"product.process-profile.{profile}"
            and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", profile)
            and binding.tool == f"run_profile.{profile}"
        ):
            return "process"
    if binding.source == "mcp" and re.fullmatch(r"mcp\.[0-9a-f]{32}", binding.executor_id):
        return "mcp"
    if binding.source == "skill" and (binding.tool, binding.executor_id) in {
        ("skill.load", "skill.load"),
        ("skill.read_resource", "skill.read_resource"),
    }:
        return "skill"
    return "custom"


def _registered_code(
    plan: ActionRoutePlan, code: str | None, stage: PublicActionStage | None
) -> bool:
    policy = _FAILURE_CODES[failure_family(plan)]
    allowed = policy[stage] if stage is not None else policy["execute"] | policy["reconcile"]
    return code in _COMMON_CODES or code in allowed


def _validate_process_failure(
    plan: ActionRoutePlan,
    outcome: ActionExecutionOutcome,
    output: JsonValue,
) -> None:
    """严格验证摘要及计划/状态事实；不把验证模型的默认字段重写进原JSON。"""

    family = failure_family(plan)
    summary = _process_summary(plan, output)
    if summary.state == "unknown":
        valid = (
            outcome.kind in {"unknown", "manual_intervention"}
            and outcome.error_code == "process_state_unknown"
        )
    elif summary.state == "failed":
        valid = outcome.kind == "failed" and outcome.error_code == "process_launch_failed"
    else:
        expected = (
            _PROCESS_STOPS.get(summary.stop_reason, "process_failed")
            if summary.stop_reason != "exited"
            else "process_nonzero_exit"
        )
        valid = outcome.kind == "failed" and outcome.error_code == expected
        if summary.stop_reason == "exited":
            valid = valid and summary.returncode != 0 and family != "eval"
    if not valid:
        raise ValueError("失败分类与Process终态事实不匹配")


def normalize_failure_outcome(
    plan: ActionRoutePlan,
    outcome: ActionExecutionOutcome,
    *,
    stage: PublicActionStage | None,
    allow_pending_output: bool = False,
) -> ActionExecutionOutcome:
    """公开失败码默认拒绝；内容错误不改变已发生效果的kind或外部身份。"""

    if outcome.kind == "succeeded":
        return outcome
    fallback = _FALLBACK_CODES[outcome.kind]
    known = _registered_code(plan, outcome.error_code, stage)
    code = outcome.error_code if known else fallback
    keep_output = False
    keep_artifact = False
    if known and failure_family(plan) in {"process", "eval"}:
        if outcome.output is not None and outcome.artifact_sha256 is not None:
            try:
                _validate_process_failure(plan, outcome, outcome.output)
            except (ValueError, TypeError):
                code = "action_failure_output_invalid"
            else:
                keep_output = keep_artifact = True
        elif (
            outcome.output is None and outcome.artifact_sha256 is not None and allow_pending_output
        ):
            # 恢复必须由Owner重建后再验证，不能把缺少正文当作摘要已经通过。
            keep_artifact = outcome.error_code in _PROCESS_CODES
        elif outcome.output is not None or outcome.artifact_sha256 is not None:
            code = "action_failure_output_invalid"
    return outcome.model_copy(
        update={
            "error_code": code,
            "output": outcome.output if keep_output else None,
            "artifact_sha256": outcome.artifact_sha256 if keep_artifact else None,
        }
    )


def validate_public_projection(
    plan: ActionRoutePlan,
    outcome: ActionExecutionOutcome,
    projected: JsonValue,
    *,
    expected_output_sha256: str,
    expected_artifact_sha256: str,
    descriptor: ToolDescriptor | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    """所有Owner投影都绑定双摘要；成功也不能授权追加任意正文。"""

    try:
        if not isinstance(projected, dict):
            raise ValueError
        summary = {key: value for key, value in projected.items() if key != "artifact"}
        family = failure_family(plan)
        if outcome.kind != "succeeded":
            if family not in {"process", "eval"}:
                raise ValueError
            _validate_process_failure(plan, outcome, summary)
        else:
            validate_success_summary(plan, summary, descriptor=descriptor, checkpoint=checkpoint)
        # AwareDatetime在严格JSON模式验证；其他字段仍禁止强制类型转换。
        reference = ArtifactRef.model_validate_json(json.dumps(projected.get("artifact")))
        if (
            canonical_digest(summary) != expected_output_sha256
            or reference.sha256 != expected_artifact_sha256
        ):
            raise ValueError
    except TurnCancelled:
        raise
    except KernelError as error:
        if error.code == "trusted_action_output_timeout":
            raise KernelError(error.code, "Action输出投影超时") from None
        raise KernelError("trusted_action_output_mismatch", "Action输出与审计终态不匹配") from None
    except Exception:
        raise KernelError("trusted_action_output_mismatch", "Action输出与审计终态不匹配") from None


def public_success_schema(
    plan: ActionRoutePlan, descriptor: ToolDescriptor | None
) -> dict[str, JsonValue] | None:
    """custom公开授权必须来自与冻结Binding精确匹配的宿主描述。"""

    if failure_family(plan) != "custom":
        return None
    binding = plan.binding
    try:
        if descriptor is None or descriptor.public_output_schema is None:
            raise ValueError
        # 先限制新增Schema，再计算完整描述Hash，避免新字段先进入序列化。
        schema = capture_public_output_schema(descriptor.public_output_schema)
        if (
            descriptor.name != binding.tool
            or descriptor.version != binding.tool_version
            or canonical_digest(descriptor.model_dump(mode="json")) != binding.tool_fingerprint
        ):
            raise ValueError
        return schema
    except TurnCancelled:
        raise
    except Exception:
        raise KernelError(
            "trusted_action_output_mismatch", "Action输出缺少匹配的公开合同"
        ) from None


def validate_success_summary(
    plan: ActionRoutePlan,
    output: JsonValue,
    *,
    descriptor: ToolDescriptor | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    """正式来源不可被扩展Schema放宽；custom无显式、精确绑定合同则拒绝正文。"""

    family = failure_family(plan)
    try:
        if family in {"process", "eval"}:
            _validate_process_success(plan, output)
        elif family in {"patch", "git", "mcp", "skill"}:
            validate_builtin_success(
                plan, cast(Literal["patch", "git", "mcp", "skill"], family), output
            )
        else:
            schema = public_success_schema(plan, descriptor)
            assert schema is not None
            validate_public_output(schema, output, checkpoint=checkpoint)
    except TurnCancelled:
        raise
    except KernelError as error:
        if error.code == "trusted_action_output_timeout":
            raise KernelError(error.code, "Action输出投影超时") from None
        raise KernelError("trusted_action_output_mismatch", "Action输出与审计终态不匹配") from None
    except Exception:
        raise KernelError("trusted_action_output_mismatch", "Action输出与审计终态不匹配") from None


def _validate_process_success(plan: ActionRoutePlan, output: JsonValue) -> None:
    """Process成功要求零退出；Eval非零退出仍是合法业务结论。"""

    family = failure_family(plan)
    summary = _process_summary(plan, output)
    if (
        summary.state != "exited"
        or summary.stop_reason != "exited"
        or (family == "process" and summary.returncode != 0)
    ):
        raise ValueError("成功Process摘要与计划或终态不匹配")


def _process_summary(
    plan: ActionRoutePlan, output: JsonValue
) -> PublicProcessOutputSummary | PublicProcessOutputSummaryV2:
    """成功与失败共用正式DTO及计划身份检查，不重写用于摘要的原JSON。"""

    family = failure_family(plan)
    model = (
        PublicEvalOutputSummary
        if family == "eval"
        else PublicProcessOutputSummaryV2
        if isinstance(output, dict) and output.get("version") == "trusted-process-output/v2"
        else PublicProcessOutputSummary
    )
    summary = model.model_validate(output)
    if (
        summary.profile != plan.invocation.arguments.get("profile")
        or summary.process_id != str(plan.execution.plan_id)
        or (
            family == "process"
            and plan.binding.executor_id != f"product.process-profile.{summary.profile}"
        )
    ):
        raise ValueError("Process摘要与计划身份不匹配")
    return summary
