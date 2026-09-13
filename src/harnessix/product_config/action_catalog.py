"""产品Action目录：从同一已验证绑定生成模型描述与Router注册。"""

from __future__ import annotations

from dataclasses import dataclass

from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor, utc_now
from harnessix.execution.contracts import canonical_digest
from harnessix.product_config.action_contracts import (
    ProductActionCapabilityEvidence,
    ProductActionCapabilityReport,
)
from harnessix.trusted_actions.router import TrustedActionDefinition, TrustedActionRouter

_PRODUCT_SOURCE_ID = "harnessix.product"


@dataclass(frozen=True, slots=True)
class ProductActionCatalogEntry:
    """一个已验证产品能力的描述、执行定义与探测证据。"""

    description: str
    definition: TrustedActionDefinition
    evidence: ProductActionCapabilityEvidence


class ProductActionCatalog:
    """保证模型广告集合与Trusted Action可执行集合来自同一事实。"""

    def __init__(
        self,
        report: ProductActionCapabilityReport,
        entries: tuple[ProductActionCatalogEntry, ...],
    ) -> None:
        checked_report = ProductActionCapabilityReport.model_validate_json(
            report.model_dump_json(warnings="error")
        )
        ordered = tuple(sorted(entries, key=lambda item: item.evidence.capability_id))
        if entries != ordered:
            raise KernelError("product_action_catalog_invalid", "产品Action目录必须稳定排序")
        entry_ids = [item.evidence.capability_id for item in entries]
        if len(set(entry_ids)) != len(entry_ids):
            raise KernelError("product_action_catalog_invalid", "产品Action目录能力重复")
        verified = tuple(item for item in checked_report.capabilities if item.status == "verified")
        if tuple(item.capability_id for item in verified) != tuple(entry_ids):
            raise KernelError(
                "product_action_catalog_mismatch",
                "产品Action能力报告与可执行目录不一致",
            )
        descriptors: list[ToolDescriptor] = []
        for entry, evidence in zip(entries, verified, strict=True):
            if entry.evidence != evidence:
                raise KernelError(
                    "product_action_catalog_mismatch",
                    "产品Action目录使用了不同能力证据",
                )
            descriptors.append(_validate_entry(entry))
        if len({item.name for item in descriptors}) != len(descriptors):
            raise KernelError("product_action_catalog_invalid", "产品Action Tool名称重复")
        self._report = checked_report
        self._entries = entries
        self._descriptors = tuple(descriptors)

    @property
    def report(self) -> ProductActionCapabilityReport:
        return self._report.model_copy(deep=True)

    def definitions(self) -> tuple[ToolDescriptor, ...]:
        return tuple(item.model_copy(deep=True) for item in self._descriptors)

    def install(self, router: TrustedActionRouter) -> None:
        """在证据有效期内注册全部定义，并复核Router观察到的精确绑定。"""

        now = utc_now()
        if self._report.created_at > now or any(
            item.expires_at <= now for item in self._report.capabilities
        ):
            raise KernelError("product_action_capability_expired", "产品Action能力证据已过期")
        if router.bindings(source="builtin", source_id=_PRODUCT_SOURCE_ID):
            raise KernelError(
                "product_action_catalog_mismatch",
                "产品Action命名空间已被其他注册占用",
            )
        router.register_many(tuple(entry.definition for entry in self._entries))
        expected = {
            (
                entry.definition.binding.source,
                entry.definition.binding.source_id,
                entry.definition.binding.tool,
            ): entry.definition.binding.binding_digest
            for entry in self._entries
        }
        observed = {
            (binding.source, binding.source_id, binding.tool): binding.binding_digest
            for binding in router.bindings(source="builtin", source_id=_PRODUCT_SOURCE_ID)
        }
        if observed != expected:
            raise KernelError(
                "product_action_catalog_mismatch",
                "产品Action广告与Router注册结果不一致",
            )


def _validate_entry(entry: ProductActionCatalogEntry) -> ToolDescriptor:
    definition = entry.definition
    binding = definition.binding
    evidence = entry.evidence
    if (
        binding.source != "builtin"
        or binding.source_id != _PRODUCT_SOURCE_ID
        or evidence.capability_id != binding.tool
        or evidence.binding_digest != binding.binding_digest
        or not entry.description.strip()
        or "\x00" in entry.description
        or "\r" in entry.description
        or "\n" in entry.description
        or len(entry.description) > 1000
    ):
        raise KernelError("product_action_catalog_mismatch", "产品Action目录绑定无效")
    schema = (
        definition.input_model.model_json_schema()
        if definition.input_schema is None
        else definition.input_schema
    )
    if canonical_digest(schema) != binding.input_schema_sha256:
        raise KernelError("product_action_catalog_mismatch", "产品Action输入Schema不一致")
    descriptor = ToolDescriptor(
        name=binding.tool,
        version=binding.tool_version,
        description=entry.description,
        input_schema=schema,
        effect_class=binding.effect_class,
        risk_level=binding.risk_level,
        requires_idempotency=binding.effect_class
        in {EffectClass.NON_IDEMPOTENT_WRITE, EffectClass.DESTRUCTIVE},
        requires_approval=(
            binding.effect_class is not EffectClass.READ_ONLY
            or binding.risk_level is not RiskLevel.LOW
        ),
        supports_reconciliation=binding.recovery_mode != "none",
        supports_parallel_calls=binding.effect_class is EffectClass.READ_ONLY,
    )
    if canonical_digest(descriptor.model_dump(mode="json")) != binding.tool_fingerprint:
        raise KernelError("product_action_catalog_mismatch", "产品Action Tool指纹不一致")
    return descriptor
