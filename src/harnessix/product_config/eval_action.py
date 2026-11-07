"""Historical Eval固定测试Profile的产品同源Trusted Action组合。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self, cast

from pydantic import BaseModel, JsonValue

from harnessix.agent.approvals import tool_fingerprint
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import ToolDescriptor
from harnessix.execution.contracts import canonical_digest
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.processes.test_contracts import RunTestsInput, TestProfile
from harnessix.processes.trusted_output import trusted_process_public_output
from harnessix.product_config.action_catalog import (
    ProductActionCatalog,
    ProductActionCatalogEntry,
)
from harnessix.product_config.action_contracts import (
    build_product_action_capability,
    build_product_action_capability_report,
    build_product_action_config,
)
from harnessix.product_config.eval_process import (
    VerifiedEvalTestProfile,
    _build_owner,
    _checked_arguments,
    _decode_run_tests,
    _descriptor,
    _process_spec,
    _resolved_action,
    _validate_route,
)
from harnessix.product_config.host_process_action import (
    HostProcessActionExecutor,
    HostProcessBinding,
    HostProcessOutputProvider,
)
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.contracts import build_trusted_tool_binding
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ResolvedAction,
    TrustedActionDefinition,
    TrustedActionRouter,
)
from harnessix.trusted_actions.store import SQLiteActionAuditStore


def _binding(owner: VerifiedEvalTestProfile) -> HostProcessBinding:
    """测试失败是需反馈给模型的事实，非零退出码不算执行失败。"""

    return HostProcessBinding(
        label=owner.profile.name,
        supervisor=owner.supervisor,
        environment=owner.environment.environment,
        check_arguments=lambda value: _checked_arguments(value, owner),
        validate_route=lambda route, arguments: _validate_route(
            owner, route, cast(RunTestsInput, arguments)
        ),
        process_spec=lambda route, _arguments: _process_spec(owner, route),
        workspace=lambda _route: owner.environment.root,
        public_output=lambda document: trusted_process_public_output(document, include_passed=True),
        nonzero_exit_fails=False,
        preflight_codes=frozenset(
            {"eval_action_workspace_mismatch", "eval_test_profile_plan_mismatch"}
        ),
    )


def _definition(
    owner: VerifiedEvalTestProfile,
    router: TrustedActionRouter,
) -> tuple[TrustedActionDefinition, HostProcessActionExecutor, ToolDescriptor]:
    descriptor = _descriptor(owner)
    binding = build_trusted_tool_binding(
        source="builtin",
        source_id="harnessix.product",
        tool=descriptor.name,
        tool_version=descriptor.version,
        tool_fingerprint=tool_fingerprint(descriptor),
        input_schema_sha256=canonical_digest(descriptor.input_schema),
        effect_class=descriptor.effect_class,
        risk_level=descriptor.risk_level,
        recovery_mode="durable_ledger",
        executor_id="eval.run-tests",
    )
    executor = HostProcessActionExecutor(_binding(owner), router)

    def decode(arguments: dict[str, JsonValue]) -> BaseModel:
        return _decode_run_tests(owner.profile.name, arguments)

    def resolve(arguments: BaseModel, context: ActionPlanningContext) -> ResolvedAction:
        return _resolved_action(owner, _checked_arguments(arguments, owner), context)

    definition = TrustedActionDefinition(
        binding=binding,
        input_model=RunTestsInput,
        resolve=resolve,
        executor=executor,
        input_schema=descriptor.input_schema,
        decode_arguments=decode,
    )
    return definition, executor, descriptor


@dataclass(slots=True)
class EvalTrustedActionComposition:
    """Eval进程内持有的Catalog、Gateway及三类持久执行账本。"""

    plans: SQLiteExecutionPlanStore
    audit: SQLiteActionAuditStore
    supervisor: PosixProcessSupervisor
    catalog: ProductActionCatalog
    gateway: RouterBackedAgentActionGateway
    _closed: bool = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.gateway.close()
        await self.supervisor.aclose()
        self.audit.close()
        self.plans.close()


async def build_eval_trusted_action_composition(
    state_root: Path,
    workspace: Path,
    launcher: Path,
    profile: TestProfile,
    artifacts: SQLiteArtifactStore,
    *,
    workspace_scope: str,
) -> EvalTrustedActionComposition:
    """探测并原子安装run_tests Catalog、Router、Gateway和持久Owner。"""

    plans = SQLiteExecutionPlanStore(state_root / "execution-plans.db")
    audit = SQLiteActionAuditStore(state_root / "action-audit.db")
    supervisor: PosixProcessSupervisor | None = None
    try:
        supervisor = PosixProcessSupervisor(state_root / "process-state")
        owner = _build_owner(workspace, launcher, profile, supervisor)
        router = TrustedActionRouter(
            plans=plans,
            audit=audit,
            workspace_root=owner.environment.workspace_root,
        )
        definition, executor, descriptor = _definition(owner, router)
        evidence = build_product_action_capability(
            capability_id=descriptor.name,
            kind="process_profile",
            status="verified",
            reason_code="verified",
            platform="posix",
            binding_digest=definition.binding.binding_digest,
            executor_evidence_digest=owner.executor_evidence_sha256,
        )
        report = build_product_action_capability_report(
            build_product_action_config(workspace_patch_enabled=False),
            (evidence,),
        )
        catalog = ProductActionCatalog(
            report,
            (
                ProductActionCatalogEntry(
                    description=descriptor.description,
                    definition=definition,
                    evidence=evidence,
                ),
            ),
        )
        catalog.install(router)
        gateway = RouterBackedAgentActionGateway(
            router,
            catalog.definitions(),
            lambda thread, _turn, _call: owner.environment.context(thread.workspace),
            presentations={descriptor.name: "process"},
            outputs={
                descriptor.name: HostProcessOutputProvider(
                    executor,
                    artifacts,
                    workspace_scope=workspace_scope,
                )
            },
        )
        return EvalTrustedActionComposition(plans, audit, supervisor, catalog, gateway)
    except BaseException:
        if supervisor is not None:
            await supervisor.aclose()
        audit.close()
        plans.close()
        raise
