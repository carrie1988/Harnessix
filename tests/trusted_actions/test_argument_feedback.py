"""Trusted参数拒绝只投影冻结合同字段，不公开调用值或回调异常。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from tests.trusted_actions import test_plan_error_boundaries as boundaries
from tests.trusted_actions.test_plan_error_boundaries import (
    _assert_preflight_stores,
    _composition,
)
from tests.trusted_actions.test_router import context, invocation


@pytest.mark.parametrize("arguments", [{}, {"path": None}, {"PRIVATE_FIELD": "PRIVATE_VALUE"}])
def test_decoder_rejection_reports_only_registered_field_facts(
    tmp_path: Path, arguments: dict
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _, actions, plans, audit, executor, binding = _composition(
        root, "decode", ValueError("CALLBACK_PRIVATE_VALUE")
    )
    try:
        call = invocation(binding).model_copy(update={"arguments": arguments})
        with pytest.raises(KernelError) as failure:
            actions.plan(call, context(root))
        assert failure.value.code == "tool_invalid_arguments"
        message = failure.value.message
        assert "必填字段：path；" in message
        assert f"缺少必填字段：{'无' if 'path' in arguments else 'path'}；" in message
        assert "允许字段：path。" in message
        assert "PRIVATE" not in message
        assert executor.calls == executor.reconciliations == 0
        _assert_preflight_stores(tmp_path)
    finally:
        plans.close()
        audit.close()


def test_sensitive_field_rejection_precedes_field_feedback(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _, actions, plans, audit, executor, binding = _composition(
        root, "decode", ValueError("CALLBACK_PRIVATE_VALUE")
    )
    try:
        call = invocation(binding).model_copy(
            update={"arguments": {"private_key": "PRIVATE_VALUE"}}
        )
        with pytest.raises(KernelError) as failure:
            actions.plan(call, context(root))
        assert failure.value.code == "raw_secret_rejected"
        assert "PRIVATE_VALUE" not in failure.value.message
        assert "必填字段" not in failure.value.message
        assert executor.calls == executor.reconciliations == 0
        _assert_preflight_stores(tmp_path)
    finally:
        plans.close()
        audit.close()


def test_feedback_uses_frozen_explicit_schema_not_generic_model(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    registered = {
        "type": "object",
        "properties": {"public_name": {}},
        "required": ["public_name"],
        "additionalProperties": False,
    }
    tool = boundaries.descriptor().model_copy(update={"input_schema": registered})
    monkeypatch.setattr(boundaries, "descriptor", lambda: tool)
    _, actions, plans, audit, executor, binding = _composition(
        root, "decode", ValueError("PRIVATE_VALUE")
    )
    # 注册后的原对象被宿主改动，也不得漂移注册时已经复制并验证的Schema。
    registered["properties"] = {"MUTATED_PRIVATE_FIELD": {}}
    registered["required"] = ["MUTATED_PRIVATE_FIELD"]
    try:
        call = invocation(binding).model_copy(update={"arguments": {}})
        with pytest.raises(KernelError) as failure:
            actions.plan(call, context(root))
        assert failure.value.code == "tool_invalid_arguments"
        assert "缺少必填字段：public_name" in failure.value.message
        assert "允许字段：public_name。" in failure.value.message
        assert "path" not in failure.value.message and "PRIVATE" not in failure.value.message
        assert executor.calls == executor.reconciliations == 0
        _assert_preflight_stores(tmp_path)
    finally:
        plans.close()
        audit.close()
