"""固定Process Profile的广告字段与严格解码器必须一致。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.product_config.action_contracts import build_product_process_profile
from harnessix.product_config.process_action import (
    RunProfileInput,
    decode_run_profile,
    process_profile_descriptor,
    product_process_binding,
)
from harnessix.trusted_actions.router import TrustedActionDefinition, TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from tests.product_config.test_process_action import _profile
from tests.trusted_actions.test_plan_error_boundaries import _assert_preflight_stores
from tests.trusted_actions.test_router import context, invocation


@pytest.mark.parametrize("policy", ["none", "bounded_test_selector"])
def test_profile_descriptor_explains_required_fixed_profile(tmp_path: Path, policy: str) -> None:
    profile = build_product_process_profile(
        **_profile(tmp_path / "engine").model_dump(
            exclude={"spec_version", "profile_sha256", "selector_policy"}
        ),
        selector_policy=policy,
    )
    tool = process_profile_descriptor(profile)
    assert '"profile": "unit-tests"' in tool.description
    assert "工具名不能替代" in tool.description
    assert "可省略" in tool.description
    schema = tool.input_schema
    assert schema["required"] == ["profile"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["selectors"]["maxItems"] == (0 if policy == "none" else 32)
    assert schema["properties"]["profile"]["const"] == "unit-tests"
    assert "必填" in schema["properties"]["profile"]["description"]
    # 元数据变更必须进入原Fingerprint，不能用旧批准授权新合同。
    old_schema = dict(schema)
    old_schema["properties"] = {
        "profile": {"type": "string", "const": "unit-tests"},
        "selectors": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 512},
            "maxItems": 32,
            "default": [],
        },
    }
    assert tool_fingerprint(tool) != tool_fingerprint(
        tool.model_copy(update={"input_schema": old_schema})
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"selectors": []},
        {"profile": None},
        {"profile": "other"},
        {"profile": "unit-tests", "program": "PRIVATE_PROGRAM"},
        {"profile": "unit-tests", "selectors": ["../tests"]},
        {"profile": "unit-tests", "selectors": ["tests;rm data"]},
    ],
)
def test_profile_feedback_does_not_relax_decoder(arguments: dict) -> None:
    with pytest.raises(ValueError):
        decode_run_profile("unit-tests", "bounded_test_selector", arguments)


def test_none_selector_policy_still_rejects_selection() -> None:
    with pytest.raises(ValueError):
        decode_run_profile("unit-tests", "none", {"profile": "unit-tests", "selectors": ["tests"]})


def test_previous_descriptor_fingerprint_cannot_plan_or_reuse_approval(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    profile = _profile(tmp_path / "engine")
    tool = process_profile_descriptor(profile)
    binding = product_process_binding(profile)
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    router = TrustedActionRouter(plans=plans, audit=audit, workspace_root=lambda _: root)

    def forbidden(*_args):
        pytest.fail("旧Fingerprint不得到达Decoder、Resolver或Executor")

    router.register(
        TrustedActionDefinition(
            binding,
            RunProfileInput,
            forbidden,
            forbidden,
            input_schema=tool.input_schema,
            decode_arguments=forbidden,
        )
    )
    old = tool.model_copy(
        update={
            "description": (
                f"在固定无网络只读容器中运行“{profile.description}”（Profile {profile.profile_id} "
                f"{profile.version}）；只能提供受限测试选择器，程序、镜像、资源、环境和Secret由宿主冻结。"
            )
        }
    )
    call = invocation(binding).model_copy(
        update={
            "arguments": {"profile": "unit-tests"},
            "tool_fingerprint": tool_fingerprint(old),
        }
    )
    try:
        with pytest.raises(KernelError) as failure:
            router.plan(call, context(root))
        assert failure.value.code == "trusted_tool_contract_changed"
        _assert_preflight_stores(tmp_path)
    finally:
        plans.close()
        audit.close()
