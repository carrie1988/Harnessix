from __future__ import annotations

from dataclasses import replace

import pytest

from harnessix.bootstrap import build_registry
from harnessix.domain.errors import ToolNotFoundError
from harnessix.domain.models import EffectClass, ToolDescriptor
from harnessix.settings import Settings


def test_runtime_owns_effect_classification(tmp_path: object) -> None:
    registry = build_registry(Settings())

    issue = registry.get("demo.issue.create")

    assert issue.effect_class is EffectClass.IDEMPOTENT_WRITE
    assert issue.requires_idempotency is True
    assert issue.supports_reconciliation is True


def test_unknown_tool_fails_closed() -> None:
    registry = build_registry(Settings())

    with pytest.raises(ToolNotFoundError):
        registry.get("missing.tool")


def test_parallel_capability_is_additive_and_write_registration_fails_closed() -> None:
    registry = build_registry(Settings())
    descriptor = registry.get("demo.issue.create").descriptor()
    payload = descriptor.model_dump(exclude={"supports_parallel_calls"})

    assert ToolDescriptor.model_validate(payload).supports_parallel_calls is False
    assert "supports_parallel_calls" in ToolDescriptor.model_json_schema()["properties"]

    invalid = replace(
        registry.get("demo.issue.create"),
        name="demo.invalid.parallel-write",
        supports_parallel_calls=True,
    )
    with pytest.raises(ValueError, match="只读"):
        registry.register(invalid)
    with pytest.raises(ToolNotFoundError):
        registry.get(invalid.name)
