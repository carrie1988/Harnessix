from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor, utc_now
from harnessix.execution.contracts import canonical_digest
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.product_config.action_catalog import (
    ProductActionCatalog,
    ProductActionCatalogEntry,
)
from harnessix.product_config.action_contracts import (
    ProductActionCapabilityEvidence,
    build_product_action_capability,
    build_product_action_capability_report,
    build_product_action_config,
)
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    TrustedToolBinding,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ResolvedAction,
    TrustedActionDefinition,
    TrustedActionRouter,
    canonical_action_resource,
)
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.contracts import WorkspaceResourceRequest


class FileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1, max_length=256)


@dataclass
class FakeExecutor:
    async def execute(
        self,
        _plan: object,
        _arguments: BaseModel,
    ) -> ActionExecutionOutcome:
        return ActionExecutionOutcome(kind="succeeded", output={"ok": True})

    async def reconcile(
        self,
        _plan: object,
        _arguments: BaseModel,
    ) -> ActionExecutionOutcome:
        return ActionExecutionOutcome(kind="manual_intervention", error_code="not_expected")


def descriptor(name: str = "workspace.patch") -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        version="1",
        description=f"事务修改Workspace内一个有界文本文件（{name}）",
        input_schema=FileInput.model_json_schema(),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        requires_idempotency=True,
        requires_approval=True,
        supports_reconciliation=True,
        supports_parallel_calls=False,
    )


def binding(
    *,
    name: str = "workspace.patch",
    fingerprint: str | None = None,
) -> TrustedToolBinding:
    tool = descriptor(name)
    return build_trusted_tool_binding(
        source="builtin",
        source_id="harnessix.product",
        tool=tool.name,
        tool_version=tool.version,
        tool_fingerprint=fingerprint or tool_fingerprint(tool),
        input_schema_sha256=canonical_digest(tool.input_schema),
        effect_class=tool.effect_class,
        risk_level=tool.risk_level,
        recovery_mode="durable_ledger",
        executor_id="product.workspace-patch",
    )


def definition(tool: TrustedToolBinding) -> TrustedActionDefinition:
    def resolve(arguments: BaseModel, _: ActionPlanningContext) -> ResolvedAction:
        checked = FileInput.model_validate(arguments)
        return ResolvedAction(
            resources=(
                canonical_action_resource(
                    kind="workspace",
                    access="write",
                    identifier={"location": "workspace", "path": checked.path},
                ),
            ),
            workspace_resources=(WorkspaceResourceRequest(path=checked.path, access="write"),),
        )

    return TrustedActionDefinition(tool, FileInput, resolve, FakeExecutor())


def evidence(
    tool: TrustedToolBinding,
    *,
    probed_at: datetime | None = None,
) -> ProductActionCapabilityEvidence:
    return build_product_action_capability(
        capability_id=tool.tool,
        kind="workspace_patch",
        status="verified",
        reason_code="verified",
        platform="windows" if os.name == "nt" else "posix",
        binding_digest=tool.binding_digest,
        executor_evidence_digest=canonical_digest("product.workspace-patch/1"),
        probed_at=probed_at,
        ttl_seconds=60,
    )


def catalog_entry(
    tool: TrustedToolBinding,
    proof: ProductActionCapabilityEvidence,
) -> ProductActionCatalogEntry:
    return ProductActionCatalogEntry(
        description=descriptor(tool.tool).description,
        definition=definition(tool),
        evidence=proof,
    )


def catalog(
    tool: TrustedToolBinding,
    proof: ProductActionCapabilityEvidence,
    *,
    created_at: datetime | None = None,
) -> ProductActionCatalog:
    report = build_product_action_capability_report(
        build_product_action_config(),
        (proof,),
        created_at=created_at,
    )
    return ProductActionCatalog(report, (catalog_entry(tool, proof),))


def test_catalog_generates_descriptor_and_installs_the_same_verified_binding(
    tmp_path: Path,
) -> None:
    tool = binding()
    proof = evidence(tool)
    product_catalog = catalog(tool, proof)
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: tmp_path,
    )

    product_catalog.install(router)

    assert product_catalog.definitions() == (descriptor(),)
    assert product_catalog.report.capabilities == (proof,)
    assert router.bindings(source="builtin", source_id="harnessix.product") == (tool,)
    plans.close()
    audit.close()


def test_omitted_capability_is_reported_but_not_advertised_or_registered(
    tmp_path: Path,
) -> None:
    omitted = build_product_action_capability(
        capability_id="workspace.patch",
        kind="workspace_patch",
        status="omitted",
        reason_code="platform_not_supported",
        platform="windows" if os.name == "nt" else "posix",
    )
    report = build_product_action_capability_report(build_product_action_config(), (omitted,))
    product_catalog = ProductActionCatalog(report, ())
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: tmp_path,
    )

    product_catalog.install(router)

    assert product_catalog.definitions() == ()
    assert product_catalog.report.capabilities == (omitted,)
    assert router.bindings() == ()
    plans.close()
    audit.close()


def test_catalog_rejects_report_entry_and_fingerprint_drift() -> None:
    tool = binding()
    proof = evidence(tool)
    report = build_product_action_capability_report(build_product_action_config(), (proof,))
    with pytest.raises(KernelError) as missing:
        ProductActionCatalog(report, ())
    assert missing.value.code == "product_action_catalog_mismatch"

    changed = binding(fingerprint=canonical_digest("changed-descriptor"))
    changed_proof = evidence(changed)
    with pytest.raises(KernelError) as fingerprint:
        catalog(changed, changed_proof)
    assert fingerprint.value.code == "product_action_catalog_mismatch"


def test_catalog_rejects_expired_evidence_before_router_install(tmp_path: Path) -> None:
    tool = binding()
    proof = evidence(tool, probed_at=utc_now() - timedelta(minutes=2))
    product_catalog = catalog(tool, proof, created_at=proof.probed_at)
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: tmp_path,
    )

    with pytest.raises(KernelError) as expired:
        product_catalog.install(router)

    assert expired.value.code == "product_action_capability_expired"
    assert router.bindings() == ()
    plans.close()
    audit.close()


def test_catalog_rejects_expired_omission_before_router_install(tmp_path: Path) -> None:
    probed_at = utc_now() - timedelta(minutes=2)
    omitted = build_product_action_capability(
        capability_id="process.unit-tests",
        kind="process_profile",
        status="omitted",
        reason_code="container_unavailable",
        platform="windows" if os.name == "nt" else "posix",
        probed_at=probed_at,
        ttl_seconds=60,
    )
    report = build_product_action_capability_report(
        build_product_action_config(),
        (omitted,),
        created_at=probed_at,
    )
    product_catalog = ProductActionCatalog(report, ())
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: tmp_path,
    )

    with pytest.raises(KernelError) as expired:
        product_catalog.install(router)

    assert expired.value.code == "product_action_capability_expired"
    assert router.bindings() == ()
    plans.close()
    audit.close()


def test_catalog_install_is_atomic_when_any_router_binding_conflicts(tmp_path: Path) -> None:
    first = binding(name="workspace.patch-a")
    conflicting = binding(name="workspace.patch-z")
    first_proof = evidence(first)
    conflicting_proof = evidence(conflicting)
    report = build_product_action_capability_report(
        build_product_action_config(),
        (first_proof, conflicting_proof),
    )
    product_catalog = ProductActionCatalog(
        report,
        (
            catalog_entry(first, first_proof),
            catalog_entry(conflicting, conflicting_proof),
        ),
    )
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: tmp_path,
    )
    router.register(definition(conflicting))

    with pytest.raises(KernelError) as duplicate:
        product_catalog.install(router)

    assert duplicate.value.code == "product_action_catalog_mismatch"
    assert router.bindings() == (conflicting,)
    plans.close()
    audit.close()
