"""公共Agent Protocol：定义版本化数据合同及其跨字段一致性校验。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

AGENT_PROTOCOL_VERSION: Literal["1.0"] = "1.0"
MAX_SAFE_JSON_INTEGER = (1 << 53) - 1
MAX_PROTOCOL_TEXT_CHARS = 1_000_000
MAX_PROTOCOL_COLLECTION = 8192


class ProtocolModel(BaseModel):
    """公共协议模型；线上JSON固定camelCase，Python端使用snake_case。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        strict=True,
    )


type JsonRpcId = (
    Annotated[
        StrictStr,
        Field(min_length=1, max_length=128),
    ]
    | Annotated[
        StrictInt,
        Field(ge=-MAX_SAFE_JSON_INTEGER, le=MAX_SAFE_JSON_INTEGER),
    ]
)


class JsonRpcRequest(ProtocolModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: JsonRpcId
    method: str = Field(min_length=1, max_length=128)
    params: dict[str, JsonValue] = Field(default_factory=dict, max_length=256)


class JsonRpcNotification(ProtocolModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: str = Field(min_length=1, max_length=128)
    params: dict[str, JsonValue] = Field(default_factory=dict, max_length=256)


class JsonRpcErrorData(ProtocolModel):
    code: str = Field(min_length=1, max_length=128)
    retryable: bool = False
    path: tuple[str | int, ...] = Field(default_factory=tuple, max_length=64)
    cursor: int | None = Field(default=None, ge=0, strict=True)

    @field_validator("path")
    @classmethod
    def bounded_path(cls, value: tuple[str | int, ...]) -> tuple[str | int, ...]:
        for part in value:
            if isinstance(part, bool):
                raise ValueError("错误字段路径不能包含布尔值")
            if isinstance(part, str) and (not part or len(part) > 128):
                raise ValueError("错误字段路径字符串长度无效")
            if isinstance(part, int) and not 0 <= part <= MAX_PROTOCOL_COLLECTION:
                raise ValueError("错误字段路径下标越界")
        return value


class JsonRpcError(ProtocolModel):
    code: int = Field(ge=-32768, le=32767, strict=True)
    message: str = Field(min_length=1, max_length=512)
    data: JsonRpcErrorData


class JsonRpcSuccessResponse(ProtocolModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: JsonRpcId
    result: JsonValue


class JsonRpcErrorResponse(ProtocolModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: JsonRpcId | None
    error: JsonRpcError


class ClientInfo(ProtocolModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    version: str = Field(min_length=1, max_length=64)


class ProtocolLimits(ProtocolModel):
    max_message_bytes: int = Field(default=1_048_576, ge=4096, le=8_388_608, strict=True)
    max_pending_requests: int = Field(default=64, ge=1, le=1024, strict=True)
    max_outbound_messages: int = Field(default=256, ge=8, le=4096, strict=True)
    max_replay_events: int = Field(default=256, ge=1, le=1000, strict=True)


class ClientCapabilities(ProtocolModel):
    item_deltas: bool = False
    server_requests: bool = True
    artifact_pages: bool = True
    replay: bool = True
    ignored_notification_methods: bool = True


class InitializeParams(ProtocolModel):
    protocol_version: Literal["1.0"]
    client_info: ClientInfo
    client_instance_id: UUID
    capabilities: ClientCapabilities = Field(default_factory=ClientCapabilities)
    limits: ProtocolLimits = Field(default_factory=ProtocolLimits)


class ServerInfo(ProtocolModel):
    name: Literal["harnessix-code"] = "harnessix-code"
    version: str = Field(min_length=1, max_length=64)


class ServerCapabilities(ProtocolModel):
    methods: tuple[str, ...] = Field(min_length=1, max_length=128)
    notifications: tuple[str, ...] = Field(default_factory=tuple, max_length=128)
    server_requests: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    replay: bool = True
    artifact_pages: bool = True
    item_deltas: bool = False

    @model_validator(mode="after")
    def stable_capabilities(self) -> ServerCapabilities:
        for values in (self.methods, self.notifications, self.server_requests):
            if values != tuple(sorted(set(values))):
                raise ValueError("协议能力必须去重并按字典序稳定排序")
            if any(not value or len(value) > 128 for value in values):
                raise ValueError("协议能力方法名长度无效")
        return self


class InitializeResult(ProtocolModel):
    protocol_version: Literal["1.0"] = AGENT_PROTOCOL_VERSION
    server_info: ServerInfo
    capabilities: ServerCapabilities
    limits: ProtocolLimits


class InitializedParams(ProtocolModel):
    pass


class CommandParams(ProtocolModel):
    request_id: str = Field(min_length=1, max_length=256)


class ThreadCreateParams(CommandParams):
    workspace: str = Field(min_length=1, max_length=4096)

    @field_validator("workspace")
    @classmethod
    def local_absolute_workspace(cls, value: str) -> str:
        if "\x00" in value or not Path(value).is_absolute():
            raise ValueError("Workspace必须是App Server宿主上的绝对路径")
        return value


class ThreadGetParams(ProtocolModel):
    thread_id: UUID


class ThreadListParams(ProtocolModel):
    cursor: str | None = Field(default=None, min_length=1, max_length=512)
    limit: int = Field(default=50, ge=1, le=200, strict=True)
    archived: bool | None = None


class ThreadResumeParams(ProtocolModel):
    thread_id: UUID


class ThreadForkParams(CommandParams):
    source_thread_id: UUID
    through_turn_id: UUID | None = None


class ThreadArchiveParams(CommandParams):
    thread_id: UUID
    reason: str | None = Field(default=None, min_length=1, max_length=1000)


class PublicBudget(ProtocolModel):
    max_steps: int = Field(ge=1, le=1000, strict=True)
    max_tokens: int = Field(ge=1, strict=True)
    timeout_seconds: float = Field(gt=0, le=86400, allow_inf_nan=False, strict=True)
    max_output_chars: int = Field(ge=1, le=1_000_000, strict=True)
    max_tool_calls_per_step: int = Field(ge=1, le=128, strict=True)


class TurnStartParams(CommandParams):
    thread_id: UUID
    prompt: str = Field(min_length=1, max_length=MAX_PROTOCOL_TEXT_CHARS)
    budget: PublicBudget | None = None


class TurnRetryParams(CommandParams):
    thread_id: UUID
    source_turn_id: UUID
    budget: PublicBudget | None = None


class TurnResumeParams(CommandParams):
    thread_id: UUID
    turn_id: UUID


class TurnCancelParams(CommandParams):
    thread_id: UUID
    turn_id: UUID


class TurnSteerParams(CommandParams):
    thread_id: UUID
    turn_id: UUID
    text: str = Field(min_length=1, max_length=MAX_PROTOCOL_TEXT_CHARS)


class PublicApprovalDecision(ProtocolModel):
    outcome: Literal["approved", "rejected"]
    actor: str = Field(min_length=1, max_length=256)
    reason: str | None = Field(default=None, max_length=2000)


class ApprovalRespondParams(CommandParams):
    thread_id: UUID
    turn_id: UUID
    approval_id: UUID
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: PublicApprovalDecision


class QuestionRespondParams(CommandParams):
    thread_id: UUID
    turn_id: UUID
    question_id: UUID
    answer: str = Field(min_length=1, max_length=4000)


class EventsReplayParams(ProtocolModel):
    thread_id: UUID
    after_cursor: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=256, ge=1, le=1000, strict=True)


class EventsNextParams(ProtocolModel):
    thread_id: UUID
    after_cursor: int = Field(default=0, ge=0, strict=True)
    wait_ms: int = Field(default=30_000, ge=0, le=30_000, strict=True)
    limit: int = Field(default=256, ge=1, le=1000, strict=True)


class ArtifactReadParams(ProtocolModel):
    thread_id: UUID
    artifact_id: UUID
    offset: int = Field(default=0, ge=0, le=10000, strict=True)
    limit: int = Field(default=100, ge=1, le=200, strict=True)


type AgentCommandParams = (
    ThreadCreateParams
    | ThreadForkParams
    | ThreadArchiveParams
    | TurnStartParams
    | TurnRetryParams
    | TurnResumeParams
    | TurnCancelParams
    | TurnSteerParams
    | ApprovalRespondParams
    | QuestionRespondParams
)
type AgentQueryParams = (
    ThreadGetParams
    | ThreadListParams
    | ThreadResumeParams
    | EventsReplayParams
    | EventsNextParams
    | ArtifactReadParams
)


class PublicFailure(ProtocolModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(max_length=2000)
    retryable: bool = False
    category: Literal[
        "input",
        "provider",
        "tool",
        "approval",
        "budget",
        "cancelled",
        "interrupted",
        "storage",
        "conflict",
        "internal",
    ]


class PublicUsage(ProtocolModel):
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)
    total_tokens: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def exact_total(self) -> PublicUsage:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("协议用量总数与输入输出不一致")
        return self


class PublicArtifactRef(ProtocolModel):
    artifact_id: UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, le=1_048_576, strict=True)
    records: int = Field(ge=0, le=10000, strict=True)
    format: Literal["jsonl/v1"]
    complete: bool
    expires_at: AwareDatetime


class PublicTextContent(ProtocolModel):
    kind: Literal["user_message", "assistant_message", "reasoning_summary"]
    text: str = Field(max_length=MAX_PROTOCOL_TEXT_CHARS)


class PublicToolCallContent(ProtocolModel):
    kind: Literal["tool_call"] = "tool_call"
    call_id: UUID
    tool: str = Field(min_length=1, max_length=256)
    tool_version: str = Field(min_length=1, max_length=128)
    effect_class: Literal["read_only", "idempotent_write", "non_idempotent_write", "destructive"]
    arguments: dict[str, JsonValue] = Field(default_factory=dict, max_length=256)
    requires_approval: bool


class PublicToolResultContent(ProtocolModel):
    kind: Literal["tool_result"] = "tool_result"
    call_id: UUID
    outcome: Literal["succeeded", "failed", "cancelled", "unknown"]
    output: JsonValue = None
    error: PublicFailure | None = None
    action_id: UUID | None = None
    diff_artifact: PublicArtifactRef | None = None


class PublicApprovalRequestContent(ProtocolModel):
    kind: Literal["approval_request"] = "approval_request"
    approval_type: Literal["tool", "patch", "patch_batch", "process"]
    approval_id: UUID
    call_id: UUID
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1, max_length=128)
    decision: PublicApprovalDecision | None = None
    diff_artifact: PublicArtifactRef | None = None


class PublicQuestionRequestContent(ProtocolModel):
    kind: Literal["question_request"] = "question_request"
    question_id: UUID
    call_id: UUID
    question: str = Field(min_length=1, max_length=4000)
    options: tuple[str, ...] = Field(default_factory=tuple, max_length=8)

    @field_validator("options")
    @classmethod
    def valid_options(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(
            not option or len(option) > 500 for option in value
        ):
            raise ValueError("提问选项必须唯一且长度有效")
        return value


class PublicQuestionAnswerContent(ProtocolModel):
    kind: Literal["question_answer"] = "question_answer"
    question_id: UUID
    call_id: UUID
    answer: str = Field(min_length=1, max_length=4000)


class PublicProcessStateContent(ProtocolModel):
    kind: Literal["process_action_state"] = "process_action_state"
    call_id: UUID
    action_id: UUID
    status: str = Field(min_length=1, max_length=64)
    origin: Literal["execution", "recovery"]


class PublicPlanStep(ProtocolModel):
    step_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=2000)
    status: Literal["pending", "in_progress", "completed"]


class PublicPlanContent(ProtocolModel):
    kind: Literal["plan"] = "plan"
    steps: tuple[PublicPlanStep, ...] = Field(min_length=1, max_length=32)
    supersedes: UUID | None = None


class PublicCompactionContent(ProtocolModel):
    kind: Literal["context_compaction"] = "context_compaction"
    source_items: int = Field(ge=1, le=4096, strict=True)
    tokens_before: int = Field(ge=1, strict=True)
    tokens_after: int = Field(ge=0, strict=True)
    tokenizer: str = Field(min_length=1, max_length=128)


class PublicErrorContent(ProtocolModel):
    kind: Literal["error"] = "error"
    failure: PublicFailure


PublicItemContent = Annotated[
    PublicTextContent
    | PublicToolCallContent
    | PublicToolResultContent
    | PublicApprovalRequestContent
    | PublicQuestionRequestContent
    | PublicQuestionAnswerContent
    | PublicProcessStateContent
    | PublicPlanContent
    | PublicCompactionContent
    | PublicErrorContent,
    Field(discriminator="kind"),
]


class PublicItem(ProtocolModel):
    item_id: UUID
    status: Literal["started", "completed", "failed", "cancelled"]
    content: PublicItemContent
    error: PublicFailure | None = None


class TurnView(ProtocolModel):
    spec_version: Literal["harnessix.agent-protocol-turn/v1"] = "harnessix.agent-protocol-turn/v1"
    turn_id: UUID
    request_id: str = Field(min_length=1, max_length=256)
    retry_of_turn_id: UUID | None = None
    status: str = Field(min_length=1, max_length=64)
    budget: PublicBudget
    usage: PublicUsage
    model_steps: int = Field(ge=0, le=1000, strict=True)
    error: PublicFailure | None = None
    created_at: AwareDatetime
    completed_at: AwareDatetime | None = None


class ThreadArchiveView(ProtocolModel):
    archived_at: AwareDatetime
    reason: str | None = Field(default=None, max_length=1000)


class ThreadView(ProtocolModel):
    spec_version: Literal["harnessix.agent-protocol-thread/v1"] = (
        "harnessix.agent-protocol-thread/v1"
    )
    thread_id: UUID
    workspace: str = Field(min_length=1, max_length=4096)
    cursor: int = Field(ge=0, strict=True)
    active_turn_id: UUID | None = None
    turn_count: int = Field(ge=0, strict=True)
    latest_turn: TurnView | None = None
    forked_from_thread_id: UUID | None = None
    archive: ThreadArchiveView | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ThreadResult(ProtocolModel):
    thread: ThreadView


class ThreadListResult(ProtocolModel):
    threads: tuple[ThreadView, ...] = Field(max_length=200)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=512)


class TurnResult(ProtocolModel):
    turn: TurnView


class ThreadPublicEvent(ProtocolModel):
    type: Literal["thread_created", "thread_forked", "thread_archived"]
    workspace: str | None = Field(default=None, min_length=1, max_length=4096)
    source_thread_id: UUID | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=1000)


class TurnStartedPublicEvent(ProtocolModel):
    type: Literal["turn_started"] = "turn_started"
    request_id: str = Field(min_length=1, max_length=256)
    retry_of_turn_id: UUID | None = None
    budget: PublicBudget


class TurnStatePublicEvent(ProtocolModel):
    type: Literal["turn_state_changed"] = "turn_state_changed"
    status: str = Field(min_length=1, max_length=64)
    error: PublicFailure | None = None


class ItemPublicEvent(ProtocolModel):
    type: Literal["item_started", "item_finished"]
    item: PublicItem


class UsagePublicEvent(ProtocolModel):
    type: Literal["usage_updated"] = "usage_updated"
    step: int = Field(ge=1, le=1000, strict=True)
    usage: PublicUsage


PublicEventData = Annotated[
    ThreadPublicEvent
    | TurnStartedPublicEvent
    | TurnStatePublicEvent
    | ItemPublicEvent
    | UsagePublicEvent,
    Field(discriminator="type"),
]


class PublicEvent(ProtocolModel):
    spec_version: Literal["harnessix.agent-protocol-event/v1"] = "harnessix.agent-protocol-event/v1"
    event_id: UUID
    thread_id: UUID
    turn_id: UUID | None = None
    cursor: int = Field(ge=1, strict=True)
    occurred_at: AwareDatetime
    data: PublicEventData


class EventsReplayResult(ProtocolModel):
    thread_id: UUID
    events: tuple[PublicEvent, ...] = Field(max_length=1000)
    scanned_through: int = Field(ge=0, strict=True)
    has_more: bool

    @model_validator(mode="after")
    def ordered_events(self) -> EventsReplayResult:
        cursors = tuple(event.cursor for event in self.events)
        if cursors != tuple(sorted(set(cursors))):
            raise ValueError("Replay公开事件游标必须严格递增")
        if cursors and (cursors[-1] > self.scanned_through or cursors[0] < 1):
            raise ValueError("Replay公开事件游标超过扫描位置")
        return self


class PublicItemDelta(ProtocolModel):
    thread_id: UUID
    turn_id: UUID
    item_id: UUID
    model_step: int = Field(ge=1, le=1000, strict=True)
    stream_sequence: int = Field(ge=1, strict=True)
    delta: str = Field(max_length=MAX_PROTOCOL_TEXT_CHARS)


class EventsNextResult(ProtocolModel):
    replay: EventsReplayResult
    deltas: tuple[PublicItemDelta, ...] = Field(default_factory=tuple, max_length=1000)
    live_has_more: bool = False
    live_gap: bool = False
    timed_out: bool = False

    @model_validator(mode="after")
    def coherent_page(self) -> EventsNextResult:
        if any(delta.thread_id != self.replay.thread_id for delta in self.deltas):
            raise ValueError("实时Delta与Replay不属于同一Thread")
        identities = tuple((delta.item_id, delta.stream_sequence) for delta in self.deltas)
        if len(set(identities)) != len(identities):
            raise ValueError("实时Delta身份重复")
        if self.timed_out and (
            self.replay.events
            or self.replay.has_more
            or self.deltas
            or self.live_has_more
            or self.live_gap
        ):
            raise ValueError("超时结果不能同时携带事件或Delta")
        return self


class ArtifactPageResult(ProtocolModel):
    artifact: PublicArtifactRef
    offset: int = Field(ge=0, le=10000, strict=True)
    text: str = Field(max_length=24 * 1024)
    next_offset: int | None = Field(default=None, ge=0, le=10000, strict=True)


def protocol_json_size(value: ProtocolModel) -> int:
    """返回规范线上JSON字节数，供传输门禁复用。"""

    encoded = json.dumps(
        value.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return len(encoded)


def validate_server_output[ProtocolOutput: ProtocolModel](
    model: type[ProtocolOutput], value: object
) -> ProtocolOutput:
    """旧客户端读取服务端结果时忽略新增可选字段，已知字段仍严格校验。"""

    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return model.model_validate_json(encoded, extra="ignore")


def validate_protocol_input[ProtocolInput: ProtocolModel](
    model: type[ProtocolInput], value: object
) -> ProtocolInput:
    """按JSON语义严格解析入站合同，禁止Python调用绕过UUID/时间转换边界。"""

    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return model.model_validate_json(encoded)
