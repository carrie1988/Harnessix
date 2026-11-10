"""可信Skill扩展：把扩展声明转换为统一Trusted Action定义。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from pydantic import BaseModel, Field, JsonValue, ValidationError

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.execution.contracts import ExecutionContract, canonical_digest
from harnessix.secrets.guard import SecretLeakGuard
from harnessix.skills.contracts import (
    SkillCatalogSnapshot,
    SkillLoadInput,
    SkillManifestSnapshot,
    SkillResourceReadInput,
)
from harnessix.skills.runtime import SkillRegistry
from harnessix.trusted_actions.builtin_success import skill_name_resource_identifier
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
# 模型工具合同与目录内容无关：编辑Skill后，未完成的Turn仍能恢复。
SKILL_AGENT_TOOL_VERSION = "1"


@dataclass(frozen=True, slots=True)
class _SkillExecutor:
    registry: SkillRegistry
    operation: str
    guard: SecretLeakGuard
    # 模型入口只给名称，这里按冻结目录补齐摘要；None表示入参已是完整请求。
    complete_request: Callable[[BaseModel], BaseModel] | None = None

    async def execute(self, plan: ActionRoutePlan, arguments: BaseModel) -> ActionExecutionOutcome:
        del plan
        try:
            request = (
                arguments if self.complete_request is None else self.complete_request(arguments)
            )
            value: BaseModel
            if self.operation == "load":
                value = self.registry.load(SkillLoadInput.model_validate(request))
            else:
                value = self.registry.read_resource(SkillResourceReadInput.model_validate(request))
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


class SkillLoadToolInput(ExecutionContract):
    """模型调用skill.load的入参；目录与清单摘要由宿主补齐。"""

    name: str = Field(
        min_length=1,
        max_length=128,
        description="Skill name exactly as listed, in source/name form when the list shows one.",
    )


class SkillResourceToolInput(SkillLoadToolInput):
    """模型调用skill.read_resource的入参。"""

    path: str = Field(
        min_length=1,
        max_length=4096,
        description="Relative path from the resources list returned by skill.load.",
    )


@dataclass(frozen=True, slots=True)
class SkillAgentTools:
    """同一事实生成的模型工具描述与可注册Definition。"""

    descriptors: tuple[ToolDescriptor, ...]
    definitions: tuple[TrustedActionDefinition, ...]


@dataclass(frozen=True, slots=True)
class _NamedSkillRequests:
    """按冻结目录把模型只给名称的入参补齐为完整请求；名称错误是普通工具失败。"""

    registry: SkillRegistry
    catalog: SkillCatalogSnapshot

    def resolve(self, arguments: BaseModel, _: ActionPlanningContext) -> ResolvedAction:
        identifier = skill_name_resource_identifier(
            self.catalog.catalog_sha256,
            cast(SkillLoadToolInput, arguments).name,
            getattr(arguments, "path", None),
        )
        resource = canonical_action_resource(kind="external", access="read", identifier=identifier)
        return ResolvedAction(resources=(resource,))

    def load(self, arguments: BaseModel) -> BaseModel:
        name = cast(SkillLoadToolInput, arguments).name
        return SkillLoadInput(
            catalog_sha256=self.catalog.catalog_sha256,
            name=name,
            expected_manifest_sha256=self.registry.resolve(self.catalog, name).manifest_sha256,
        )

    def resource(self, arguments: BaseModel) -> BaseModel:
        parsed = cast(SkillResourceToolInput, arguments)
        manifest = self.registry.resolve(self.catalog, parsed.name)
        try:
            return SkillResourceReadInput(
                catalog_sha256=self.catalog.catalog_sha256,
                name=parsed.name,
                expected_manifest_sha256=manifest.manifest_sha256,
                path=parsed.path,
            )
        except ValidationError:
            raise KernelError("skill_resource_path_denied", "Skill资源路径无效") from None


def _agent_tool(
    requests: _NamedSkillRequests,
    guard: SecretLeakGuard,
    descriptor: ToolDescriptor,
    model: type[BaseModel],
    operation: str,
    complete_request: Callable[[BaseModel], BaseModel],
) -> TrustedActionDefinition:
    binding = build_trusted_tool_binding(
        source="skill",
        source_id=requests.catalog.catalog_id,
        tool=descriptor.name,
        tool_version=descriptor.version,
        tool_fingerprint=tool_fingerprint(descriptor),
        input_schema_sha256=canonical_digest(descriptor.input_schema),
        effect_class=EffectClass.READ_ONLY,
        risk_level=RiskLevel.LOW,
        recovery_mode="none",
        executor_id=f"skill.{operation}",
    )
    executor = _SkillExecutor(requests.registry, operation, guard, complete_request)
    return TrustedActionDefinition(binding, model, requests.resolve, executor)


def _agent_descriptor(name: str, description: str, model: type[BaseModel]) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        version=SKILL_AGENT_TOOL_VERSION,
        description=description,
        input_schema=model.model_json_schema(),
        effect_class=EffectClass.READ_ONLY,
        risk_level=RiskLevel.LOW,
        requires_idempotency=False,
        requires_approval=False,
        supports_reconciliation=False,
        supports_parallel_calls=True,
    )


def build_skill_agent_tools(
    registry: SkillRegistry,
    catalog: SkillCatalogSnapshot,
    *,
    protected_secret_values: tuple[bytes, ...] = (),
) -> SkillAgentTools:
    """为冻结目录构造模型工具，工具合同不随目录内容变化。"""

    checked = SkillCatalogSnapshot.model_validate_json(catalog.model_dump_json())
    if registry.catalog(digest=checked.catalog_sha256) != checked:
        raise KernelError("skill_catalog_mismatch", "Skill Action目录与运行时不一致")
    guard = SecretLeakGuard(tuple(bytes(value) for value in protected_secret_values))
    requests = _NamedSkillRequests(registry, checked)
    load = _agent_descriptor(
        SKILL_LOAD_TOOL,
        "Load the full instructions of an installed skill by name.",
        SkillLoadToolInput,
    )
    resource = _agent_descriptor(
        SKILL_RESOURCE_TOOL,
        "Read one text file listed in the resources of a loaded skill.",
        SkillResourceToolInput,
    )
    return SkillAgentTools(
        descriptors=(load, resource),
        definitions=(
            _agent_tool(requests, guard, load, SkillLoadToolInput, "load", requests.load),
            _agent_tool(
                requests,
                guard,
                resource,
                SkillResourceToolInput,
                "read_resource",
                requests.resource,
            ),
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
