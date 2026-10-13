"""进程内已完成请求的封装证明；不持久化正文，重启后保守回退。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from harnessix.models._chat_mapping import build_request
from harnessix.models._history import encode_json
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ModelRequest


def _digest(value: Any) -> str:
    return hashlib.sha256(encode_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CompletedChatInput:
    """只证明本实例上一成功请求；持久用量和历史前缀仍由 Runtime 验证。"""

    thread_id: UUID
    turn_id: UUID
    step: int
    history_items: int
    history_messages_sha256: str
    tools_sha256: str
    instructions_sha256: str
    config_sha256: str

    @classmethod
    def capture(
        cls, request: ModelRequest, body: dict[str, Any], config: OpenAIChatConfig
    ) -> CompletedChatInput:
        prefix = request.model_copy(
            update={
                "tools": (),
                "instructions": None,
                "consumed_usage": None,
                "tool_choice": "auto",
                "forced_tool": None,
            }
        )
        prefix_body, _ = build_request(prefix, config)
        return cls(
            request.thread_id,
            request.turn_id,
            request.step,
            len(request.history),
            _digest(prefix_body["messages"]),
            _digest(body.get("tools", [])),
            _digest(request.instructions),
            _digest(config.model_dump(mode="json")),
        )

    def unchanged_wrapper_bytes(
        self,
        request: ModelRequest,
        body: dict[str, Any],
        prefix_body: dict[str, Any],
        config: OpenAIChatConfig,
        prefix_items: int,
    ) -> int:
        if (
            (self.thread_id, self.turn_id, self.step)
            != (request.thread_id, request.turn_id, request.step - 1)
            or self.history_items != prefix_items
            or self.history_messages_sha256 != _digest(prefix_body["messages"])
            or self.config_sha256 != _digest(config.model_dump(mode="json"))
        ):
            return 0
        redundant_bytes = 0
        tools = body.get("tools")
        if tools is not None and self.tools_sha256 == _digest(tools):
            redundant_bytes += len(encode_json(tools).encode("utf-8"))
        if request.instructions is not None and self.instructions_sha256 == _digest(
            request.instructions
        ):
            instruction = {"role": "system", "content": request.instructions}
            redundant_bytes += len(encode_json(instruction).encode("utf-8"))
        return redundant_bytes
