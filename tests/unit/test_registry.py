from __future__ import annotations

import pytest

from harnessix.bootstrap import build_registry
from harnessix.domain.errors import ToolNotFoundError
from harnessix.domain.models import EffectClass
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
