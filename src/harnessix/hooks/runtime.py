"""匹配生命周期Hook并经Trusted Action执行；授权、运行事件和恢复事实持久化。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID, uuid5

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, RiskLevel, utc_now
from harnessix.execution.contracts import canonical_digest
from harnessix.hooks.contracts import (
    HookActionInput,
    HookActionOutput,
    HookDefinition,
    HookDispatch,
    HookDispatchResult,
    HookRegistrySnapshot,
    HookRunPlan,
    HookRunSnapshot,
    HookTrustGrant,
    hook_registry_snapshot_digest,
    hook_run_plan_digest,
)
from harnessix.hooks.store import SQLiteHookStore
from harnessix.secrets.guard import SecretLeakGuard
from harnessix.trusted_actions.contracts import TrustedToolBinding
from harnessix.trusted_actions.router import ExtensionActionPort

_HOOK_RUN_NAMESPACE = UUID("9a2d4c4b-0bc2-4209-8f1a-94f01032df03")
_TERMINAL_STATES = frozenset({"succeeded", "failed", "blocked", "cancelled", "interrupted"})


class HookRuntime:
    """确定顺序、持久状态且只能调用ExtensionActionPort的Hook运行时。"""

    def __init__(
        self,
        *,
        registry_id: str,
        definitions: Sequence[HookDefinition],
        trust_grants: Sequence[HookTrustGrant],
        ports: Mapping[str, ExtensionActionPort],
        store: SQLiteHookStore,
        protected_secret_values: tuple[bytes, ...] = (),
        captured_at: datetime | None = None,
    ) -> None:
        checked_definitions = tuple(
            HookDefinition.model_validate_json(item.model_dump_json()) for item in definitions
        )
        checked_grants = tuple(
            HookTrustGrant.model_validate_json(item.model_dump_json()) for item in trust_grants
        )
        if len(checked_definitions) > 512 or len(checked_grants) > 512:
            raise KernelError("hook_registry_limit", "Hook定义或授权数量超过上限")
        if len({item.qualified_id for item in checked_definitions}) != len(checked_definitions):
            raise KernelError("hook_definition_duplicate", "Hook限定身份重复")
        if len({(item.source_id, item.hook_id) for item in checked_grants}) != len(checked_grants):
            raise KernelError("hook_trust_grant_duplicate", "Hook Trust Grant重复")
        now = captured_at or utc_now()
        used_grants: list[HookTrustGrant] = []
        bindings: dict[str, TrustedToolBinding] = {}
        for definition in checked_definitions:
            if definition.source_kind != "bundled":
                grant = next(
                    (
                        item
                        for item in checked_grants
                        if item.source_id == definition.source_id
                        and item.hook_id == definition.hook_id
                        and item.definition_sha256 == definition.definition_sha256
                    ),
                    None,
                )
                if grant is None or (grant.expires_at is not None and grant.expires_at <= now):
                    raise KernelError("hook_trust_required", "非Bundled Hook缺少当前定义授权")
                used_grants.append(grant)
            port = ports.get(definition.source_id)
            if port is None:
                raise KernelError("hook_action_port_missing", "Hook来源缺少Action Port")
            binding = self._find_binding(port, definition)
            self._validate_binding(binding)
            bindings[definition.qualified_id] = binding
        ordered = tuple(
            sorted(
                checked_definitions,
                key=lambda item: (item.event, item.order, item.qualified_id),
            )
        )
        generation = store.next_registry_generation(registry_id)
        candidate = HookRegistrySnapshot.model_construct(
            _fields_set=None,
            registry_id=registry_id,
            generation=generation,
            captured_at=now,
            definitions=ordered,
            trust_grant_sha256=tuple(sorted(item.grant_sha256 for item in used_grants)),
            registry_sha256="0" * 64,
        )
        try:
            registry = HookRegistrySnapshot(
                **candidate.model_dump(exclude={"registry_sha256"}),
                registry_sha256=hook_registry_snapshot_digest(candidate),
            )
        except ValidationError:
            raise KernelError("hook_registry_invalid", "Hook Registry合同无效") from None
        self._registry = store.save_registry(registry)
        self._ports = dict(ports)
        self._bindings = bindings
        self._store = store
        self._guard = SecretLeakGuard(tuple(bytes(value) for value in protected_secret_values))
        self._dispatch_lock = asyncio.Lock()

    @property
    def registry(self) -> HookRegistrySnapshot:
        return self._registry.model_copy(deep=True)

    def recover_interrupted(self) -> tuple[UUID, ...]:
        return self._store.recover_interrupted()

    async def dispatch(self, dispatch: HookDispatch) -> HookDispatchResult:
        checked = HookDispatch.model_validate_json(dispatch.model_dump_json())
        async with self._dispatch_lock:
            self._verify_registry()
            selected = tuple(
                definition
                for definition in self._registry.definitions
                if definition.event == checked.event and self._matches(definition, checked)
            )
            runs: list[HookRunSnapshot] = []
            for definition in selected:
                current = await self._run(definition, checked)
                runs.append(current)
                if definition.mode == "blocking" and current.state != "succeeded":
                    break
            allowed = not any(
                run.plan.definition.mode == "blocking" and run.state != "succeeded" for run in runs
            )
            return HookDispatchResult(
                dispatch_id=checked.dispatch_id,
                event=checked.event,
                allowed=allowed,
                runs=tuple(runs),
            )

    async def _run(self, definition: HookDefinition, dispatch: HookDispatch) -> HookRunSnapshot:
        port = self._ports[definition.source_id]
        binding = self._find_binding(port, definition)
        if binding != self._bindings[definition.qualified_id]:
            raise KernelError("hook_action_binding_changed", "Hook Action绑定已经变化")
        action_input = HookActionInput(
            registry_sha256=self._registry.registry_sha256,
            definition_sha256=definition.definition_sha256,
            hook_id=definition.hook_id,
            dispatch_id=dispatch.dispatch_id,
            event=dispatch.event,
            thread_id=dispatch.thread_id,
            turn_id=dispatch.turn_id,
            target_action_source=dispatch.target_action_source,
            target_action_source_id_sha256=(
                canonical_digest(dispatch.target_action_source_id)
                if dispatch.target_action_source_id is not None
                else None
            ),
            target_action_tool=dispatch.target_action_tool,
            target_action_plan_id=dispatch.target_action_plan_id,
            arguments_sha256=dispatch.arguments_sha256,
            outcome_sha256=dispatch.outcome_sha256,
        )
        run_id = uuid5(
            _HOOK_RUN_NAMESPACE,
            f"{dispatch.dispatch_id}:{definition.definition_sha256}",
        )
        candidate = HookRunPlan.model_construct(
            _fields_set=None,
            run_id=run_id,
            registry_id=self._registry.registry_id,
            registry_generation=self._registry.generation,
            registry_sha256=self._registry.registry_sha256,
            definition=definition,
            dispatch=dispatch,
            action_plan_id=run_id,
            input_sha256=canonical_digest(action_input.model_dump(mode="json")),
            plan_sha256="0" * 64,
        )
        plan = HookRunPlan(
            **candidate.model_dump(exclude={"plan_sha256"}),
            plan_sha256=hook_run_plan_digest(candidate),
        )
        current = self._store.begin(plan)
        if current.state in _TERMINAL_STATES:
            return current
        if current.state == "running":
            raise KernelError("hook_run_in_progress", "Hook Run仍由其他执行者处理")
        try:
            action_plan = port.plan(
                invocation_id=run_id,
                tool=binding.tool,
                tool_version=binding.tool_version,
                tool_fingerprint=binding.tool_fingerprint,
                arguments=action_input.model_dump(mode="json"),
            )
        except KernelError:
            return self._store.transition(
                run_id,
                expected={"ready"},
                target="failed",
                error_code="hook_action_plan_failed",
            )
        if action_plan.state != "ready":
            return self._store.transition(
                run_id,
                expected={"ready"},
                target="failed",
                error_code="hook_action_not_ready",
            )
        self._store.transition(run_id, expected={"ready"}, target="running")
        try:
            async with asyncio.timeout(definition.timeout_ms / 1000):
                outcome = await port.execute(run_id)
        except TimeoutError:
            return self._store.transition(
                run_id,
                expected={"running"},
                target="failed",
                error_code="hook_timeout",
            )
        except asyncio.CancelledError:
            self._store.transition(
                run_id,
                expected={"running"},
                target="cancelled",
                error_code="hook_cancelled",
            )
            raise
        except Exception:
            return self._store.transition(
                run_id,
                expected={"running"},
                target="failed",
                error_code="hook_action_failed",
            )
        if outcome.kind != "succeeded" or outcome.output is None:
            return self._store.transition(
                run_id,
                expected={"running"},
                target="failed",
                error_code="hook_action_failed",
            )
        try:
            self._guard.assert_safe(outcome.output)
            parsed = HookActionOutput.model_validate(outcome.output)
            if definition.mode == "advisory" and parsed.decision == "deny":
                raise ValueError("Advisory Hook不能拒绝")
        except (KernelError, ValidationError, ValueError, TypeError):
            return self._store.transition(
                run_id,
                expected={"running"},
                target="failed",
                error_code="hook_output_invalid",
            )
        output_sha256 = canonical_digest(parsed.model_dump(mode="json"))
        if parsed.decision == "deny":
            return self._store.transition(
                run_id,
                expected={"running"},
                target="blocked",
                decision="deny",
                output_sha256=output_sha256,
                error_code="hook_denied",
            )
        return self._store.transition(
            run_id,
            expected={"running"},
            target="succeeded",
            decision="allow",
            output_sha256=output_sha256,
        )

    def _verify_registry(self) -> None:
        if (
            self._store.load_registry(
                self._registry.registry_id,
                generation=self._registry.generation,
                digest=self._registry.registry_sha256,
            )
            != self._registry
        ):
            raise KernelError("hook_registry_changed", "Hook Registry持久绑定已经变化")

    @staticmethod
    def _matches(definition: HookDefinition, dispatch: HookDispatch) -> bool:
        matcher = definition.matcher
        if matcher is None:
            return True
        return (
            matcher.action_source in {"*", dispatch.target_action_source}
            and (
                matcher.action_source_id is None
                or matcher.action_source_id == dispatch.target_action_source_id
            )
            and matcher.action_tool in {"*", dispatch.target_action_tool}
        )

    @staticmethod
    def _find_binding(port: ExtensionActionPort, definition: HookDefinition) -> TrustedToolBinding:
        matches = tuple(
            binding
            for binding in port.bindings()
            if binding.tool == definition.action_tool
            and binding.tool_version == definition.action_tool_version
            and binding.tool_fingerprint == definition.action_tool_fingerprint
        )
        if len(matches) != 1:
            raise KernelError("hook_action_binding_missing", "Hook Action绑定不存在或已经变化")
        return matches[0]

    @staticmethod
    def _validate_binding(binding: TrustedToolBinding) -> None:
        if (
            binding.source != "hook"
            or binding.effect_class is not EffectClass.READ_ONLY
            or binding.risk_level is not RiskLevel.LOW
            or binding.recovery_mode != "none"
            or binding.input_schema_sha256 != canonical_digest(HookActionInput.model_json_schema())
        ):
            raise KernelError("hook_action_binding_unsafe", "Hook Action绑定不符合只读安全合同")
