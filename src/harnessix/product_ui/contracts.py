"""产品客户端本地恢复元数据合同。"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_CLIENT_STATE_BYTES = 1024 * 1024
MAX_CLIENT_THREAD_CURSORS = 1000
MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991
_WORKSPACE_IDENTITY_MAX_BYTES = 16 * 1024


class ProductUIContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class ClientThreadCursor(ProductUIContract):
    """一个Thread最近确认的服务端持久事件游标。"""

    thread_id: UUID
    cursor: int = Field(ge=0, le=MAX_SAFE_JSON_INTEGER, strict=True)


class ClientStateV1(ProductUIContract):
    """不含会话正文的版本化客户端恢复状态。"""

    spec_version: Literal["harnessix.client-state/v1"] = "harnessix.client-state/v1"
    client_instance_id: UUID
    workspace_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    next_command_sequence: int = Field(ge=1, le=MAX_SAFE_JSON_INTEGER, strict=True)
    selected_thread_id: UUID | None = None
    thread_cursors: tuple[ClientThreadCursor, ...] = Field(
        default=(), max_length=MAX_CLIENT_THREAD_CURSORS
    )
    clean_shutdown: bool = True
    state_revision: int = Field(ge=1, le=MAX_SAFE_JSON_INTEGER, strict=True)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_canonical_state(self) -> Self:
        identities = tuple(str(item.thread_id) for item in self.thread_cursors)
        if identities != tuple(sorted(identities)) or len(set(identities)) != len(identities):
            raise ValueError("Thread Cursor必须唯一且按Thread ID排序")
        if self.digest != client_state_digest(self):
            raise ValueError("客户端状态摘要不一致")
        return self

    def cursor_for(self, thread_id: UUID) -> int:
        return next(
            (item.cursor for item in self.thread_cursors if item.thread_id == thread_id),
            0,
        )


class ClientCommandAllocation(ProductUIContract):
    request_id: str = Field(pattern=r"^tui-[0-9a-f]{32}-[0-9a-z]+$", max_length=80)
    sequence: int = Field(ge=1, le=MAX_SAFE_JSON_INTEGER, strict=True)
    state_revision: int = Field(ge=1, le=MAX_SAFE_JSON_INTEGER, strict=True)


def workspace_identity_fingerprint(workspace_identity: str) -> str:
    """摘要化产品边界已规范化的Workspace身份，不持久化原文。"""

    if type(workspace_identity) is not str:
        raise ValueError("Workspace身份必须是字符串")
    encoded = workspace_identity.encode("utf-8")
    if (
        not workspace_identity.strip()
        or b"\0" in encoded
        or len(encoded) > _WORKSPACE_IDENTITY_MAX_BYTES
    ):
        raise ValueError("Workspace身份无效")
    return hashlib.sha256(b"harnessix-workspace-identity/v1\0" + encoded).hexdigest()


def client_state_digest(state: ClientStateV1) -> str:
    body = json.dumps(
        state.model_dump(mode="json", exclude={"digest"}, warnings="error"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def new_client_state(workspace_fingerprint: str) -> ClientStateV1:
    candidate = ClientStateV1.model_construct(
        client_instance_id=uuid4(),
        workspace_fingerprint=workspace_fingerprint,
        next_command_sequence=1,
        selected_thread_id=None,
        thread_cursors=(),
        clean_shutdown=True,
        state_revision=1,
        digest="0" * 64,
    )
    return ClientStateV1(
        **candidate.model_dump(exclude={"digest"}),
        digest=client_state_digest(candidate),
    )


def evolve_client_state(state: ClientStateV1, **changes: object) -> ClientStateV1:
    """创建递增一个Revision且摘要重新封装的状态。"""

    if state.state_revision >= MAX_SAFE_JSON_INTEGER:
        raise ValueError("客户端状态Revision已耗尽")
    candidate = state.model_copy(
        update={
            **changes,
            "state_revision": state.state_revision + 1,
            "digest": "0" * 64,
        }
    )
    sealed = candidate.model_copy(update={"digest": client_state_digest(candidate)})
    return ClientStateV1.model_validate_json(sealed.model_dump_json(), strict=True)


def command_request_id(client_instance_id: UUID, sequence: int) -> str:
    if type(sequence) is not int or not 1 <= sequence <= MAX_SAFE_JSON_INTEGER:
        raise ValueError("命令序列无效")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    digits: list[str] = []
    remaining = sequence
    while remaining:
        remaining, digit = divmod(remaining, 36)
        digits.append(alphabet[digit])
    return f"tui-{client_instance_id.hex}-{''.join(reversed(digits))}"
