from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from harnessix.execution.contracts import ExecutionContract, canonical_digest
from harnessix.tools.contracts import Revision

HookSourceKind = Literal["bundled", "managed", "user", "workspace"]
HookEventName = Literal[
    "session_started",
    "session_ended",
    "turn_started",
    "turn_completed",
    "before_action",
    "after_action",
]
HookMode = Literal["blocking", "advisory"]
HookFailurePolicy = Literal["fail_closed", "record_only"]
HookDecisionKind = Literal["allow", "deny"]
HookRunState = Literal[
    "ready",
    "running",
    "succeeded",
    "failed",
    "blocked",
    "cancelled",
    "interrupted",
]


class HookMatcher(ExecutionContract):
    action_source: str | None = Field(default=None, pattern=r"^(\*|[a-z][a-z0-9_.-]{0,63})$")
    action_source_id: str | None = Field(default=None, min_length=1, max_length=256)
    action_tool: str | None = Field(default=None, pattern=r"^(\*|[A-Za-z][A-Za-z0-9_.-]{0,255})$")

    @model_validator(mode="after")
    def complete_source(self) -> Self:
        if self.action_source_id is not None and self.action_source is None:
            raise ValueError("Hook Matcher指定来源身份时必须指定来源")
        return self


class HookDefinition(ExecutionContract):
    spec_version: Literal["harnessix.hook-definition/v1"] = "harnessix.hook-definition/v1"
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    source_kind: HookSourceKind
    source_version: str = Field(min_length=1, max_length=128)
    hook_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    qualified_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}/[a-z][a-z0-9_.-]{0,63}$")
    event: HookEventName
    matcher: HookMatcher | None = None
    order: int = Field(ge=-10_000, le=10_000)
    mode: HookMode
    failure_policy: HookFailurePolicy
    timeout_ms: int = Field(ge=100, le=60_000)
    action_tool: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
    action_tool_version: str = Field(min_length=1, max_length=128)
    action_tool_fingerprint: Revision
    definition_sha256: Revision

    @model_validator(mode="after")
    def valid_definition(self) -> Self:
        if self.qualified_id != f"{self.source_id}/{self.hook_id}":
            raise ValueError("Hook限定身份与来源不一致")
        if self.event in {"before_action", "after_action"}:
            if self.matcher is None:
                raise ValueError("Action Hook必须声明Matcher")
        elif self.matcher is not None:
            raise ValueError("非Action Hook不能声明Matcher")
        if self.event == "before_action":
            if self.mode != "blocking" or self.failure_policy != "fail_closed":
                raise ValueError("before_action必须Blocking且Fail Closed")
        elif self.mode != "advisory" or self.failure_policy != "record_only":
            raise ValueError("非before_action Hook必须Advisory且Record Only")
        if self.definition_sha256 != hook_definition_digest(self):
            raise ValueError("Hook定义摘要不一致")
        return self


def hook_definition_digest(definition: HookDefinition) -> str:
    return canonical_digest(
        definition.model_dump(mode="json", exclude={"definition_sha256"}, warnings="error")
    )


def build_hook_definition(
    *,
    source_id: str,
    source_kind: HookSourceKind,
    source_version: str,
    hook_id: str,
    event: HookEventName,
    matcher: HookMatcher | None,
    order: int,
    mode: HookMode,
    failure_policy: HookFailurePolicy,
    timeout_ms: int,
    action_tool: str,
    action_tool_version: str,
    action_tool_fingerprint: str,
) -> HookDefinition:
    candidate = HookDefinition.model_construct(
        _fields_set=None,
        source_id=source_id,
        source_kind=source_kind,
        source_version=source_version,
        hook_id=hook_id,
        qualified_id=f"{source_id}/{hook_id}",
        event=event,
        matcher=matcher,
        order=order,
        mode=mode,
        failure_policy=failure_policy,
        timeout_ms=timeout_ms,
        action_tool=action_tool,
        action_tool_version=action_tool_version,
        action_tool_fingerprint=action_tool_fingerprint,
        definition_sha256="0" * 64,
    )
    return HookDefinition(
        **candidate.model_dump(exclude={"definition_sha256"}),
        definition_sha256=hook_definition_digest(candidate),
    )


class HookTrustGrant(ExecutionContract):
    spec_version: Literal["harnessix.hook-trust-grant/v1"] = "harnessix.hook-trust-grant/v1"
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    hook_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    definition_sha256: Revision
    granted_by_sha256: Revision
    granted_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    grant_sha256: Revision

    @model_validator(mode="after")
    def valid_grant(self) -> Self:
        if self.expires_at is not None and self.expires_at <= self.granted_at:
            raise ValueError("Hook授权过期时间无效")
        if self.grant_sha256 != hook_trust_grant_digest(self):
            raise ValueError("Hook授权摘要不一致")
        return self


def hook_trust_grant_digest(grant: HookTrustGrant) -> str:
    return canonical_digest(grant.model_dump(mode="json", exclude={"grant_sha256"}))


def build_hook_trust_grant(
    definition: HookDefinition,
    *,
    granted_by: str,
    granted_at: datetime,
    expires_at: datetime | None = None,
) -> HookTrustGrant:
    candidate = HookTrustGrant.model_construct(
        _fields_set=None,
        source_id=definition.source_id,
        hook_id=definition.hook_id,
        definition_sha256=definition.definition_sha256,
        granted_by_sha256=canonical_digest(granted_by),
        granted_at=granted_at,
        expires_at=expires_at,
        grant_sha256="0" * 64,
    )
    return HookTrustGrant(
        **candidate.model_dump(exclude={"grant_sha256"}),
        grant_sha256=hook_trust_grant_digest(candidate),
    )


class HookRegistrySnapshot(ExecutionContract):
    spec_version: Literal["harnessix.hook-registry-snapshot/v1"] = (
        "harnessix.hook-registry-snapshot/v1"
    )
    registry_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    generation: int = Field(ge=1)
    captured_at: AwareDatetime
    definitions: tuple[HookDefinition, ...] = Field(max_length=512)
    trust_grant_sha256: tuple[Revision, ...] = Field(max_length=512)
    registry_sha256: Revision

    @model_validator(mode="after")
    def canonical_registry(self) -> Self:
        keys = [(item.event, item.order, item.qualified_id) for item in self.definitions]
        if (
            keys != sorted(keys)
            or len({item.qualified_id for item in self.definitions}) != len(self.definitions)
            or list(self.trust_grant_sha256) != sorted(self.trust_grant_sha256)
            or len(set(self.trust_grant_sha256)) != len(self.trust_grant_sha256)
            or self.registry_sha256 != hook_registry_snapshot_digest(self)
        ):
            raise ValueError("Hook Registry快照不规范")
        return self


def hook_registry_snapshot_digest(registry: HookRegistrySnapshot) -> str:
    return canonical_digest(
        registry.model_dump(
            mode="json",
            exclude={"generation", "captured_at", "registry_sha256"},
            warnings="error",
        )
    )


class HookDispatch(ExecutionContract):
    spec_version: Literal["harnessix.hook-dispatch/v1"] = "harnessix.hook-dispatch/v1"
    dispatch_id: UUID
    event: HookEventName
    thread_id: UUID
    turn_id: UUID | None = None
    target_action_source: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    target_action_source_id: str | None = Field(default=None, min_length=1, max_length=256)
    target_action_tool: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
    target_action_plan_id: UUID | None = None
    arguments_sha256: Revision | None = None
    outcome_sha256: Revision | None = None
    occurred_at: AwareDatetime
    dispatch_sha256: Revision

    @model_validator(mode="after")
    def valid_shape(self) -> Self:
        action_fields = (
            self.target_action_source,
            self.target_action_source_id,
            self.target_action_tool,
            self.target_action_plan_id,
        )
        if self.event in {"before_action", "after_action"}:
            if any(item is None for item in action_fields):
                raise ValueError("Action Hook事件缺少目标Action身份")
        elif any(item is not None for item in action_fields):
            raise ValueError("非Action Hook事件不能携带目标Action身份")
        if (self.event == "before_action") != (self.arguments_sha256 is not None):
            raise ValueError("before_action参数摘要不一致")
        if (self.event == "after_action") != (self.outcome_sha256 is not None):
            raise ValueError("after_action结果摘要不一致")
        if self.event in {"turn_started", "turn_completed", "before_action", "after_action"}:
            if self.turn_id is None:
                raise ValueError("Turn与Action Hook事件必须携带Turn身份")
        elif self.turn_id is not None:
            raise ValueError("Session Hook事件不能携带Turn身份")
        if self.dispatch_sha256 != hook_dispatch_digest(self):
            raise ValueError("Hook Dispatch摘要不一致")
        return self


def hook_dispatch_digest(dispatch: HookDispatch) -> str:
    return canonical_digest(dispatch.model_dump(mode="json", exclude={"dispatch_sha256"}))


def build_hook_dispatch(
    *,
    dispatch_id: UUID,
    event: HookEventName,
    thread_id: UUID,
    turn_id: UUID | None,
    occurred_at: datetime,
    target_action_source: str | None = None,
    target_action_source_id: str | None = None,
    target_action_tool: str | None = None,
    target_action_plan_id: UUID | None = None,
    arguments_sha256: str | None = None,
    outcome_sha256: str | None = None,
) -> HookDispatch:
    candidate = HookDispatch.model_construct(
        _fields_set=None,
        dispatch_id=dispatch_id,
        event=event,
        thread_id=thread_id,
        turn_id=turn_id,
        target_action_source=target_action_source,
        target_action_source_id=target_action_source_id,
        target_action_tool=target_action_tool,
        target_action_plan_id=target_action_plan_id,
        arguments_sha256=arguments_sha256,
        outcome_sha256=outcome_sha256,
        occurred_at=occurred_at,
        dispatch_sha256="0" * 64,
    )
    return HookDispatch(
        **candidate.model_dump(exclude={"dispatch_sha256"}),
        dispatch_sha256=hook_dispatch_digest(candidate),
    )


class HookActionInput(ExecutionContract):
    registry_sha256: Revision
    definition_sha256: Revision
    hook_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    dispatch_id: UUID
    event: HookEventName
    thread_id: UUID
    turn_id: UUID | None = None
    target_action_source: str | None = None
    target_action_source_id_sha256: Revision | None = None
    target_action_tool: str | None = None
    target_action_plan_id: UUID | None = None
    arguments_sha256: Revision | None = None
    outcome_sha256: Revision | None = None

    @model_validator(mode="after")
    def valid_shape(self) -> Self:
        action_fields = (
            self.target_action_source,
            self.target_action_source_id_sha256,
            self.target_action_tool,
            self.target_action_plan_id,
        )
        if self.event in {"before_action", "after_action"}:
            if any(item is None for item in action_fields):
                raise ValueError("Hook Action输入缺少目标身份")
        elif any(item is not None for item in action_fields):
            raise ValueError("非Action Hook输入不能携带目标身份")
        if (self.event == "before_action") != (self.arguments_sha256 is not None):
            raise ValueError("Hook Action输入参数摘要不一致")
        if (self.event == "after_action") != (self.outcome_sha256 is not None):
            raise ValueError("Hook Action输入结果摘要不一致")
        if self.event in {"turn_started", "turn_completed", "before_action", "after_action"}:
            if self.turn_id is None:
                raise ValueError("Hook Action输入缺少Turn身份")
        elif self.turn_id is not None:
            raise ValueError("Session Hook输入不能携带Turn身份")
        return self


class HookActionOutput(ExecutionContract):
    decision: HookDecisionKind
    reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,127}$")

    @model_validator(mode="after")
    def reason_for_denial(self) -> Self:
        if (self.decision == "deny") != (self.reason_code is not None):
            raise ValueError("Hook拒绝结果必须且只能携带reason_code")
        return self


class HookRunPlan(ExecutionContract):
    spec_version: Literal["harnessix.hook-run-plan/v1"] = "harnessix.hook-run-plan/v1"
    run_id: UUID
    registry_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    registry_generation: int = Field(ge=1)
    registry_sha256: Revision
    definition: HookDefinition
    dispatch: HookDispatch
    action_plan_id: UUID
    input_sha256: Revision
    plan_sha256: Revision

    @model_validator(mode="after")
    def valid_plan(self) -> Self:
        if self.run_id != self.action_plan_id:
            raise ValueError("Hook Run与Action计划身份必须一致")
        if self.definition.event != self.dispatch.event:
            raise ValueError("Hook定义与Dispatch事件不一致")
        if self.plan_sha256 != hook_run_plan_digest(self):
            raise ValueError("Hook Run Plan摘要不一致")
        return self


def hook_run_plan_digest(plan: HookRunPlan) -> str:
    return canonical_digest(plan.model_dump(mode="json", exclude={"plan_sha256"}))


class HookRunSnapshot(ExecutionContract):
    spec_version: Literal["harnessix.hook-run-snapshot/v1"] = "harnessix.hook-run-snapshot/v1"
    plan: HookRunPlan
    state: HookRunState
    sequence: int = Field(ge=1, le=16)
    last_event_digest: Revision
    decision: HookDecisionKind | None = None
    output_sha256: Revision | None = None
    error_code: str | None = Field(default=None, pattern=r"^hook_[a-z0-9_]{1,119}$")
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def valid_terminal(self) -> Self:
        HookRunEvent._validate_terminal(
            self.state,
            self.decision,
            self.output_sha256,
            self.error_code,
        )
        return self


class HookRunEvent(ExecutionContract):
    spec_version: Literal["harnessix.hook-run-event/v1"] = "harnessix.hook-run-event/v1"
    run_id: UUID
    plan_sha256: Revision
    sequence: int = Field(ge=1, le=16)
    from_state: HookRunState | None
    to_state: HookRunState
    decision: HookDecisionKind | None = None
    output_sha256: Revision | None = None
    error_code: str | None = Field(default=None, pattern=r"^hook_[a-z0-9_]{1,119}$")
    occurred_at: AwareDatetime
    previous_digest: Revision | None = None
    digest: Revision

    @model_validator(mode="after")
    def valid_event(self) -> Self:
        if (self.sequence == 1) != (self.from_state is None and self.previous_digest is None):
            raise ValueError("Hook Run事件前序不一致")
        if self.sequence > 1 and (self.from_state is None or self.previous_digest is None):
            raise ValueError("Hook Run事件缺少前序状态")
        self._validate_terminal(self.to_state, self.decision, self.output_sha256, self.error_code)
        if self.digest != hook_run_event_digest(self):
            raise ValueError("Hook Run事件摘要不一致")
        return self

    @staticmethod
    def _validate_terminal(
        state: HookRunState,
        decision: HookDecisionKind | None,
        output_sha256: str | None,
        error_code: str | None,
    ) -> None:
        if state == "succeeded":
            valid = decision == "allow" and output_sha256 is not None and error_code is None
        elif state == "blocked":
            valid = decision == "deny" and output_sha256 is not None and error_code == "hook_denied"
        elif state in {"failed", "cancelled", "interrupted"}:
            valid = decision is None and output_sha256 is None and error_code is not None
        else:
            valid = decision is None and output_sha256 is None and error_code is None
        if not valid:
            raise ValueError("Hook Run终态字段不一致")


def hook_run_event_digest(event: HookRunEvent) -> str:
    return canonical_digest(event.model_dump(mode="json", exclude={"digest"}))


def build_hook_run_event(
    *,
    plan: HookRunPlan,
    sequence: int,
    from_state: HookRunState | None,
    to_state: HookRunState,
    occurred_at: datetime,
    previous_digest: str | None,
    decision: HookDecisionKind | None = None,
    output_sha256: str | None = None,
    error_code: str | None = None,
) -> HookRunEvent:
    candidate = HookRunEvent.model_construct(
        _fields_set=None,
        run_id=plan.run_id,
        plan_sha256=plan.plan_sha256,
        sequence=sequence,
        from_state=from_state,
        to_state=to_state,
        decision=decision,
        output_sha256=output_sha256,
        error_code=error_code,
        occurred_at=occurred_at,
        previous_digest=previous_digest,
        digest="0" * 64,
    )
    return HookRunEvent(
        **candidate.model_dump(exclude={"digest"}),
        digest=hook_run_event_digest(candidate),
    )


class HookDispatchResult(ExecutionContract):
    spec_version: Literal["harnessix.hook-dispatch-result/v1"] = "harnessix.hook-dispatch-result/v1"
    dispatch_id: UUID
    event: HookEventName
    allowed: bool
    runs: tuple[HookRunSnapshot, ...] = Field(max_length=512)

    @model_validator(mode="after")
    def valid_result(self) -> Self:
        if any(
            run.plan.dispatch.dispatch_id != self.dispatch_id
            or run.plan.dispatch.event != self.event
            for run in self.runs
        ):
            raise ValueError("Hook Dispatch结果包含其他Dispatch")
        blocking_failure = any(
            run.plan.definition.mode == "blocking" and run.state != "succeeded" for run in self.runs
        )
        if self.allowed == blocking_failure:
            raise ValueError("Hook Dispatch放行结果与Blocking Hook不一致")
        return self
