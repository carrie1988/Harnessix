from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from harnessix.domain.errors import ToolNotFoundError
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.domain.ports import ActionExecutor


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    version: str
    description: str
    input_model: type[BaseModel]
    effect_class: EffectClass
    risk_level: RiskLevel
    executor: ActionExecutor
    requires_idempotency: bool = False
    requires_approval: bool = False
    supports_reconciliation: bool = False

    def descriptor(self) -> ToolDescriptor:
        return ToolDescriptor(
            name=self.name,
            version=self.version,
            description=self.description,
            input_schema=self.input_model.model_json_schema(),
            effect_class=self.effect_class,
            risk_level=self.risk_level,
            requires_idempotency=self.requires_idempotency,
            requires_approval=self.requires_approval,
            supports_reconciliation=self.supports_reconciliation,
        )


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as error:
            raise ToolNotFoundError(name) from error

    def list_descriptors(self) -> list[ToolDescriptor]:
        return [self._tools[name].descriptor() for name in sorted(self._tools)]
