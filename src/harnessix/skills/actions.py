"""可信Skill扩展：把扩展声明转换为统一Trusted Action定义。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from pydantic import BaseModel, JsonValue

from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, RiskLevel
from harnessix.execution.contracts import canonical_digest
from harnessix.secrets.guard import SecretLeakGuard
from harnessix.skills.contracts import (
    SkillCatalogSnapshot,
    SkillLoadInput,
    SkillManifestSnapshot,
    SkillResourceReadInput,
)
from harnessix.skills.runtime import SkillRegistry
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    ActionRoutePlan,
    ActionRouteSnapshot,
    TrustedToolBinding,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ExtensionActionPort,
    ResolvedAction,
    ResourceResolver,
    TrustedActionDefinition,
    canonical_action_resource,
)

SKILL_LOAD_TOOL = "skill.load"
SKILL_RESOURCE_TOOL = "skill.read_resource"


@dataclass(frozen=True, slots=True)
class _SkillExecutor:
    registry: SkillRegistry
    operation: str
    guard: SecretLeakGuard

    async def execute(self, plan: ActionRoutePlan, arguments: BaseModel) -> ActionExecutionOutcome:
        del plan
        try:
            value: BaseModel
            if self.operation == "load":
                parsed = SkillLoadInput.model_validate(arguments)
                value = self.registry.load(parsed)
            else:
                parsed_resource = SkillResourceReadInput.model_validate(arguments)
                value = self.registry.read_resource(parsed_resource)
            output = cast(JsonValue, value.model_dump(mode="json", warnings="error"))
            self.guard.assert_safe(output)
            return ActionExecutionOutcome(kind="succeeded", output=output)
        except KernelError as error:
            return ActionExecutionOutcome(kind="failed", error_code=error.code)

    async def reconcile(
        self, plan: ActionRoutePlan, arguments: BaseModel
    ) -> ActionExecutionOutcome:
        del plan, arguments
        return ActionExecutionOutcome(
            kind="manual_intervention",
            error_code="skill_reconciliation_not_supported",
        )


def build_skill_action_definitions(
    registry: SkillRegistry,
    catalog: SkillCatalogSnapshot,
    *,
    protected_secret_values: tuple[bytes, ...] = (),
) -> tuple[TrustedActionDefinition, TrustedActionDefinition]:
    """为一个已持久目录创建两项只读Action；目录变化必须重新注册。"""

    checked = SkillCatalogSnapshot.model_validate_json(catalog.model_dump_json())
    if registry.catalog(digest=checked.catalog_sha256) != checked:
        raise KernelError("skill_catalog_mismatch", "Skill Action目录与运行时不一致")
    guard = SecretLeakGuard(tuple(bytes(value) for value in protected_secret_values))

    def load_resolver(arguments: BaseModel, _: ActionPlanningContext) -> ResolvedAction:
        parsed = SkillLoadInput.model_validate(arguments)
        return ResolvedAction(
            resources=(
                canonical_action_resource(
                    kind="external",
                    access="read",
                    identifier={
                        "catalog": parsed.catalog_sha256,
                        "manifest": parsed.expected_manifest_sha256,
                    },
                ),
            )
        )

    def resource_resolver(arguments: BaseModel, _: ActionPlanningContext) -> ResolvedAction:
        parsed = SkillResourceReadInput.model_validate(arguments)
        return ResolvedAction(
            resources=(
                canonical_action_resource(
                    kind="external",
                    access="read",
                    identifier={
                        "catalog": parsed.catalog_sha256,
                        "manifest": parsed.expected_manifest_sha256,
                        "path": parsed.path,
                    },
                ),
            )
        )

    def definition(
        tool: str,
        model: type[BaseModel],
        resolver: ResourceResolver,
        operation: str,
    ) -> TrustedActionDefinition:
        schema = model.model_json_schema()
        binding = build_trusted_tool_binding(
            source="skill",
            source_id=checked.catalog_id,
            tool=tool,
            tool_version=f"{checked.generation}.{checked.catalog_sha256[:16]}",
            tool_fingerprint=canonical_digest(
                {"operation": operation, "catalog": checked.catalog_sha256}
            ),
            input_schema_sha256=canonical_digest(schema),
            effect_class=EffectClass.READ_ONLY,
            risk_level=RiskLevel.LOW,
            recovery_mode="none",
            executor_id=f"skill.{operation}",
        )
        return TrustedActionDefinition(
            binding=binding,
            input_model=model,
            resolve=resolver,
            executor=_SkillExecutor(registry, operation, guard),
        )

    return (
        definition(SKILL_LOAD_TOOL, SkillLoadInput, load_resolver, "load"),
        definition(
            SKILL_RESOURCE_TOOL,
            SkillResourceReadInput,
            resource_resolver,
            "read_resource",
        ),
    )


class SkillActionGateway:
    """向模型公开目录摘要，并且只经Skill ExtensionActionPort读取正文。"""

    def __init__(self, catalog: SkillCatalogSnapshot, port: ExtensionActionPort) -> None:
        self._catalog = SkillCatalogSnapshot.model_validate_json(catalog.model_dump_json())
        self._port = port
        for tool in (SKILL_LOAD_TOOL, SKILL_RESOURCE_TOOL):
            self._binding(tool)

    def skills(self) -> tuple[SkillManifestSnapshot, ...]:
        return tuple(item.model_copy(deep=True) for item in self._catalog.skills)

    @property
    def catalog(self) -> SkillCatalogSnapshot:
        return self._catalog.model_copy(deep=True)

    def plan_load(
        self,
        *,
        invocation_id: UUID,
        name: str,
        expected_manifest_sha256: str,
    ) -> ActionRouteSnapshot:
        binding = self._binding(SKILL_LOAD_TOOL)
        return self._port.plan(
            invocation_id=invocation_id,
            tool=binding.tool,
            tool_version=binding.tool_version,
            tool_fingerprint=binding.tool_fingerprint,
            arguments={
                "catalog_sha256": self._catalog.catalog_sha256,
                "name": name,
                "expected_manifest_sha256": expected_manifest_sha256,
            },
        )

    def plan_resource(
        self,
        *,
        invocation_id: UUID,
        name: str,
        expected_manifest_sha256: str,
        path: str,
    ) -> ActionRouteSnapshot:
        binding = self._binding(SKILL_RESOURCE_TOOL)
        return self._port.plan(
            invocation_id=invocation_id,
            tool=binding.tool,
            tool_version=binding.tool_version,
            tool_fingerprint=binding.tool_fingerprint,
            arguments={
                "catalog_sha256": self._catalog.catalog_sha256,
                "name": name,
                "expected_manifest_sha256": expected_manifest_sha256,
                "path": path,
            },
        )

    async def execute(self, plan_id: UUID) -> ActionExecutionOutcome:
        return await self._port.execute(plan_id)

    def status(self, plan_id: UUID) -> ActionRouteSnapshot:
        return self._port.status(plan_id)

    def _binding(self, tool: str) -> TrustedToolBinding:
        matches = tuple(binding for binding in self._port.bindings() if binding.tool == tool)
        if len(matches) != 1:
            raise KernelError("skill_action_not_trusted", "Skill Action未通过可信宿主绑定")
        binding = matches[0]
        expected = canonical_digest(
            {
                "operation": "load" if tool == SKILL_LOAD_TOOL else "read_resource",
                "catalog": self._catalog.catalog_sha256,
            }
        )
        if binding.source_id != self._catalog.catalog_id or binding.tool_fingerprint != expected:
            raise KernelError("skill_catalog_changed", "Skill Action与目录摘要不一致")
        return binding
