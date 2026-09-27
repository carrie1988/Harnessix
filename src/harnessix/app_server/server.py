"""管理单连接Agent Protocol状态、参数校验与方法分派；领域执行委托应用服务。"""

from __future__ import annotations

from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from uuid import UUID

from pydantic import JsonValue, ValidationError

from harnessix.agent.errors import KernelError
from harnessix.app_server.frame_publication import (
    admit_message,
    closing_response,
    error_frame,
    publish_frame,
)
from harnessix.app_server.frame_publication import encode as _encode
from harnessix.app_server.handshake import (
    PreparedInitialization,
    prepare_initialization,
    validation_path,
)
from harnessix.app_server.service import AgentApplicationService, AgentServiceError
from harnessix.protocol.codec import ProtocolDecodeError, decode_client_frame
from harnessix.protocol.contracts import (
    ApprovalRespondParams,
    ArtifactReadParams,
    EventsNextParams,
    EventsReplayParams,
    InitializedParams,
    JsonRpcId,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcSuccessResponse,
    ProtocolLimits,
    ProtocolModel,
    QuestionRespondParams,
    ThreadArchiveParams,
    ThreadCreateParams,
    ThreadForkParams,
    ThreadGetParams,
    ThreadListParams,
    ThreadResumeParams,
    TurnCancelParams,
    TurnResumeParams,
    TurnRetryParams,
    TurnStartParams,
    TurnSteerParams,
    validate_protocol_input,
)

SERVER_METHODS = (
    "approval/respond",
    "events/next",
    "events/replay",
    "initialize",
    "question/respond",
    "thread/archive",
    "thread/create",
    "thread/fork",
    "thread/get",
    "thread/list",
    "thread/resume",
    "turn/cancel",
    "turn/resume",
    "turn/retry",
    "turn/start",
    "turn/steer",
)


class ConnectionState(StrEnum):
    """单个JSON-RPC连接的初始化、关闭和请求状态。"""

    NEW = "new"
    INITIALIZED_PENDING_ACK = "initialized_pending_ack"
    READY = "ready"
    CLOSING = "closing"
    CLOSED = "closed"


class InvalidProtocolParams(ValueError):
    def __init__(self, error: ValidationError) -> None:
        super().__init__("协议参数无效")
        self.error = error


def _params[Params: ProtocolModel](model: type[Params], value: dict[str, JsonValue]) -> Params:
    try:
        return validate_protocol_input(model, value)
    except ValidationError as error:
        raise InvalidProtocolParams(error) from None


def _product_version() -> str:
    try:
        return version("harnessix")
    except PackageNotFoundError:
        return "0.0.0+source"


class AgentProtocolServer:
    """单客户端JSON-RPC会话；领域状态与执行权仍归AgentRuntime。"""

    def __init__(
        self,
        service: AgentApplicationService,
        *,
        limits: ProtocolLimits | None = None,
    ) -> None:
        self.service = service
        self.methods = tuple(
            sorted(
                (
                    *SERVER_METHODS,
                    *(("artifact/read",) if service.artifact_reader is not None else ()),
                )
            )
        )
        self.limits = limits or ProtocolLimits()
        self.state = ConnectionState.NEW
        self.client_instance_id: UUID | None = None
        self.item_deltas_enabled = False

    def _error(
        self,
        request_id: JsonRpcId | None,
        rpc_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        path: tuple[str | int, ...] = (),
    ) -> bytes:
        return error_frame(request_id, rpc_code, code, message, retryable=retryable, path=path)

    def _notification(self, notification: JsonRpcNotification) -> None:
        if notification.method == "notifications/initialized":
            if self.state is not ConnectionState.INITIALIZED_PENDING_ACK:
                return
            validate_protocol_input(InitializedParams, notification.params)
            self.state = ConnectionState.READY

    async def _dispatch(self, method: str, params: dict[str, JsonValue]) -> ProtocolModel:
        assert self.client_instance_id is not None
        client = self.client_instance_id
        if method == "thread/create":
            return await self.service.create_thread(client, _params(ThreadCreateParams, params))
        if method == "thread/get":
            return await self.service.get_thread(_params(ThreadGetParams, params))
        if method == "thread/list":
            return await self.service.list_threads(_params(ThreadListParams, params))
        if method == "thread/resume":
            return await self.service.resume_thread(_params(ThreadResumeParams, params))
        if method == "thread/fork":
            return await self.service.fork_thread(client, _params(ThreadForkParams, params))
        if method == "thread/archive":
            return await self.service.archive_thread(client, _params(ThreadArchiveParams, params))
        if method == "turn/start":
            return await self.service.start_turn(client, _params(TurnStartParams, params))
        if method == "turn/retry":
            return await self.service.retry_turn(client, _params(TurnRetryParams, params))
        if method == "turn/resume":
            return await self.service.resume_turn(client, _params(TurnResumeParams, params))
        if method == "turn/cancel":
            return await self.service.cancel_turn(client, _params(TurnCancelParams, params))
        if method == "turn/steer":
            return await self.service.steer_turn(client, _params(TurnSteerParams, params))
        if method == "approval/respond":
            return await self.service.respond_approval(
                client, _params(ApprovalRespondParams, params)
            )
        if method == "question/respond":
            return await self.service.respond_question(
                client, _params(QuestionRespondParams, params)
            )
        if method == "events/next":
            return await self.service.next_events(
                _params(EventsNextParams, params),
                include_deltas=self.item_deltas_enabled,
            )
        if method == "events/replay":
            return await self.service.replay_events(_params(EventsReplayParams, params))
        if method == "artifact/read":
            return await self.service.read_artifact(_params(ArtifactReadParams, params))
        raise AgentServiceError("method_not_found", "方法未实现")

    async def _handle_request(self, message: JsonRpcRequest) -> bytes | PreparedInitialization:
        """原业务分派与错误映射；握手只准备候选，所有动态响应由外层统一保护。"""
        if message.method == "initialize":
            return prepare_initialization(
                message,
                is_new=self.state is ConnectionState.NEW,
                limits=self.limits,
                methods=self.methods,
                artifact_pages=self.service.artifact_reader is not None,
                version=_product_version(),
            )
        if self.state is not ConnectionState.READY:
            return self._error(message.id, -32012, "not_initialized", "连接尚未完成初始化")
        if message.method not in self.methods:
            return self._error(message.id, -32601, "method_not_found", "协议方法不存在")
        try:
            result = await self._dispatch(message.method, message.params)
        except InvalidProtocolParams as invalid:
            return self._error(
                message.id,
                -32602,
                "invalid_params",
                "协议参数无效",
                path=validation_path(invalid.error),
            )
        except AgentServiceError as error:
            rpc_code = -32011 if error.code == "idempotency_conflict" else -32010
            return self._error(
                message.id, rpc_code, error.code, error.message, retryable=error.retryable
            )
        except KernelError as error:
            return self._error(
                message.id, -32010, error.code, str(error), retryable=error.retryable
            )
        except Exception:
            return self._error(
                message.id, -32603, "internal_error", "服务端内部错误；原始异常未公开"
            )
        return _encode(
            JsonRpcSuccessResponse(
                id=message.id, result=result.model_dump(mode="json", by_alias=True)
            )
        )

    async def process_frame(self, frame: bytes) -> tuple[bytes, ...]:
        """完整原封套先准入，原响应字节先保护；通知无响应，拒绝不回显未授权身份。"""
        if self.state in {ConnectionState.CLOSING, ConnectionState.CLOSED}:
            return closing_response(frame, max_message_bytes=self.limits.max_message_bytes)
        try:
            message = decode_client_frame(frame, max_message_bytes=self.limits.max_message_bytes)
        except ProtocolDecodeError as error:
            # 解析/关闭控制帧仅含固定字段，无原id、键、正文或第三方诊断。
            return (self._error(None, error.rpc_code, error.code, error.message),)
        rejected = await admit_message(self.service.runtime, message)
        if rejected is not None:
            return rejected
        if self.state in {ConnectionState.CLOSING, ConnectionState.CLOSED}:
            return (
                ()
                if isinstance(message, JsonRpcNotification)
                else (self._error(None, -32015, "server_closing", "服务端正在关闭"),)
            )
        if isinstance(message, JsonRpcNotification):
            try:
                self._notification(message)
            except (ValidationError, TypeError, ValueError):
                pass
            return ()
        try:
            candidate = await self._handle_request(message)
        except Exception:
            candidate = self._error(
                message.id, -32603, "internal_error", "服务端内部错误；原始异常未公开"
            )
        original = candidate.frame if isinstance(candidate, PreparedInitialization) else candidate
        output_limit = (
            candidate.limits if isinstance(candidate, PreparedInitialization) else self.limits
        )
        published, accepted = await publish_frame(
            self.service.runtime,
            original,
            message.id,
            max_message_bytes=output_limit.max_message_bytes,
        )
        if accepted and isinstance(candidate, PreparedInitialization):
            if self.state in {ConnectionState.CLOSING, ConnectionState.CLOSED}:
                return (self._error(None, -32015, "server_closing", "服务端正在关闭"),)
            if self.state is not ConnectionState.NEW:
                conflict_response, _ = await publish_frame(
                    self.service.runtime,
                    self._error(message.id, -32600, "already_initialized", "连接已经执行初始化"),
                    message.id,
                    max_message_bytes=self.limits.max_message_bytes,
                )
                return (conflict_response,)
            # 无await提交已保护候选；失败/取消时原NEW、身份、能力和协商限额均不变。
            self.client_instance_id = candidate.client_instance_id
            self.item_deltas_enabled = candidate.item_deltas_enabled
            self.limits = candidate.limits
            self.state = ConnectionState.INITIALIZED_PENDING_ACK
        return (published,)

    async def close(self) -> None:
        if self.state is ConnectionState.CLOSED:
            return
        self.state = ConnectionState.CLOSING
        await self.service.close()
        self.state = ConnectionState.CLOSED
