"""公开失败策略来源与阶段选择；已登记码不等于任意JSON的公开资格。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.domain.models import EffectClass, RiskLevel
from harnessix.execution.contracts import canonical_digest
from harnessix.trusted_actions.contracts import ActionExecutionOutcome, build_trusted_tool_binding
from harnessix.trusted_actions.public_outcomes import normalize_failure_outcome
from tests.trusted_actions.test_public_error_leakage import _payload
from tests.trusted_actions.test_router import (
    FakeExecutor,
    FileInput,
    context,
    definition,
    invocation,
    router,
)


@pytest.mark.parametrize(
    "source,source_id,tool,executor,stage,code,accepted",
    [
        (
            "builtin",
            "harnessix.product",
            "apply_patch_batch",
            "product.workspace-patch",
            "execute",
            "delivery_not_applied",
            True,
        ),
        (
            "builtin",
            "harnessix.product",
            "apply_patch_batch",
            "product.workspace-patch",
            "reconcile",
            "delivery_partial_effect",
            True,
        ),
        (
            "builtin",
            "harnessix.product",
            "apply_patch_batch",
            "product.workspace-patch",
            "execute",
            "delivery_partial_effect",
            False,
        ),
        (
            "builtin",
            "custom",
            "apply_patch_batch",
            "product.workspace-patch",
            "execute",
            "delivery_not_applied",
            False,
        ),
        (
            "builtin",
            "harnessix.product",
            "apply_patch_batch",
            "custom.patch",
            "execute",
            "delivery_not_applied",
            False,
        ),
        (
            "builtin",
            "harnessix.product",
            "git.push",
            "delivery.git-push",
            "execute",
            "git_push_rejected",
            True,
        ),
        (
            "builtin",
            "harnessix.product",
            "git.push",
            "delivery.git-push",
            "execute",
            "git_command_failed",
            True,
        ),
        (
            "builtin",
            "harnessix.product",
            "git.push",
            "delivery.git-push",
            "reconcile",
            "git_push_remote_diverged",
            True,
        ),
        (
            "builtin",
            "harnessix.product",
            "git.push",
            "delivery.git-push",
            "execute",
            "git_push_remote_diverged",
            False,
        ),
        (
            "builtin",
            "harnessix.product",
            "git.push",
            "delivery.git-push",
            "execute",
            "git_commit_not_executable",
            False,
        ),
        (
            "mcp",
            "bounded-server",
            "mcp.test",
            "mcp." + "a" * 32,
            "execute",
            "mcp_result_invalid",
            True,
        ),
        (
            "mcp",
            "bounded-server",
            "mcp.test",
            "mcp." + "a" * 32,
            "execute",
            "mcp_result_secret_canary",
            False,
        ),
        (
            "mcp",
            "bounded-server",
            "mcp.test",
            "mcp." + "a" * 32,
            "reconcile",
            "mcp_result_invalid",
            False,
        ),
        ("mcp", "bounded-server", "mcp.test", "mcp.bad", "execute", "mcp_result_invalid", False),
        (
            "builtin",
            "bounded-server",
            "mcp.test",
            "mcp." + "a" * 32,
            "execute",
            "mcp_result_invalid",
            False,
        ),
        (
            "skill",
            "bounded-catalog",
            "skill.load",
            "skill.load",
            "execute",
            "skill_content_changed",
            True,
        ),
        (
            "skill",
            "bounded-catalog",
            "skill.read_resource",
            "skill.read_resource",
            "execute",
            "skill_resource_not_found",
            True,
        ),
        (
            "skill",
            "bounded-catalog",
            "skill.load",
            "skill.load",
            "execute",
            "secret_redaction_failed",
            True,
        ),
        (
            "skill",
            "bounded-catalog",
            "skill.load",
            "custom.skill",
            "execute",
            "skill_content_changed",
            False,
        ),
        (
            "skill",
            "bounded-catalog",
            "skill.load",
            "skill.load",
            "reconcile",
            "skill_content_changed",
            False,
        ),
    ],
)
def test_source_and_stage_contract_selects_finite_code_but_never_generic_body(
    tmp_path: Path, source, source_id, tool, executor, stage, code, accepted
):
    root = tmp_path / "workspace"
    root.mkdir()
    binding = build_trusted_tool_binding(
        source=source,
        source_id=source_id,
        tool=tool,
        tool_version="1",
        tool_fingerprint=canonical_digest(tool),
        input_schema_sha256=canonical_digest(FileInput.model_json_schema()),
        effect_class=EffectClass.READ_ONLY,
        risk_level=RiskLevel.LOW,
        recovery_mode="none",
        executor_id=executor,
    )
    actions, plans, audit = router(
        root, definition(binding, FakeExecutor(ActionExecutionOutcome(kind="succeeded")))
    )
    try:
        route = actions.plan(invocation(binding), context(root))
        external_id = uuid4()
        raw = ActionExecutionOutcome(
            kind="failed",
            error_code=code,
            output={"diagnostic": _payload()},
            artifact_sha256="b" * 64,
            external_action_id=external_id,
        )
        safe = normalize_failure_outcome(route.plan, raw, stage=stage)
        assert safe.error_code == (code if accepted else "action_failed")
        assert safe.kind == "failed" and safe.external_action_id == external_id
        assert safe.output is None and safe.artifact_sha256 is None
    finally:
        plans.close()
        audit.close()
