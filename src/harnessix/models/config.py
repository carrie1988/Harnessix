"""模型Provider适配：定义Provider连接、重试与HTTP限制配置。"""

from __future__ import annotations

from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from harnessix.domain.models import ContractModel


class ChatCapabilities(ContractModel):
    """约束模型单响应能力；多调用接收不等于并发执行，后者由Runtime控制。"""

    tool_calls: bool = True
    parallel_tool_calls: bool = True
    streaming_usage: Literal[True] = True

    @model_validator(mode="after")
    def validate_tools(self) -> Self:
        if self.parallel_tool_calls and not self.tool_calls:
            raise ValueError("并行工具能力要求工具调用能力")
        return self


class ModelHTTPConfig(ContractModel):
    base_url: str = Field(max_length=2048)
    model: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/-]+$")
    api_key_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")
    capabilities: ChatCapabilities = Field(default_factory=ChatCapabilities)
    max_output_tokens: int = Field(default=1024, ge=1, le=1_000_000)
    timeout_seconds: float = Field(default=60, gt=0, le=3600, allow_inf_nan=False)
    io_timeout_seconds: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    max_attempts: int = Field(default=2, ge=1, le=5)
    retry_delay_seconds: float = Field(default=0.5, ge=0, le=10, allow_inf_nan=False)
    max_request_bytes: int = Field(default=2_097_152, ge=1024, le=16_777_216)
    max_response_bytes: int = Field(default=2_097_152, ge=1024, le=16_777_216)
    max_frame_bytes: int = Field(default=262_144, ge=128, le=1_048_576)
    max_chunks: int = Field(default=10_000, ge=1, le=100_000)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        url = urlsplit(value)
        # 访问 port 触发标准库对非数字和越界端口的验证。
        _ = url.port
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(character.isspace() for character in value)
        ):
            raise ValueError("端点必须是无用户信息、查询串或 fragment 的 HTTPS URL")
        return value.rstrip("/")


class OpenAIChatConfig(ModelHTTPConfig):
    base_url: str = Field(default="https://api.openai.com/v1", max_length=2048)
    api_key_env: str = Field(default="OPENAI_API_KEY", pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")
    output_token_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    # 未显式配置时不改变既有序列化和恢复指纹；显式布尔值才进入Provider契约。
    enable_thinking: bool | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    thinking_budget: int | None = Field(
        default=None,
        ge=1,
        le=262_144,
        exclude_if=lambda value: value is None,
        description="显式思考Token上限；须使用包含思考与回答的max_completion_tokens",
    )
    # 仅在TCP连接或TLS握手失败时重连（请求字节尚未发出，不产生计费或新的模型尝试）。
    connect_retries: int = Field(default=0, ge=0, le=5, exclude_if=lambda value: value == 0)
    # 工具参数严格解析失败时只修复字符串内原始控制字符与非法反斜杠转义；其他畸形仍为协议失败。
    repair_tool_arguments: bool = Field(default=False, exclude_if=lambda value: value is False)
    # 思考开关线上格式：默认DashScope的enable_thinking；thinking_type发送thinking.type。
    thinking_format: Literal["thinking_type"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    # 前缀缓存布局：稳定前缀（指令＋历史）在前，逐请求变化的预算/目录说明移到末尾；
    # explicit_tail另在前缀末端打缓存标记，stable_tail供服务端自动前缀缓存、不打标记。
    prompt_cache: Literal["explicit_tail", "stable_tail"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_thinking_budget(self) -> Self:
        if self.thinking_budget is not None and (
            self.enable_thinking is not True
            or self.output_token_parameter != "max_completion_tokens"
            or self.thinking_budget >= self.max_output_tokens
        ):
            raise ValueError("思考预算要求显式开启思考、整体输出上限及非空回答余量")
        if self.thinking_format is not None and (
            self.enable_thinking is None or self.thinking_budget is not None
        ):
            raise ValueError("thinking_type格式要求显式思考开关且不支持思考预算")
        return self


class AnthropicConfig(ModelHTTPConfig):
    base_url: str = Field(default="https://api.anthropic.com", max_length=2048)
    api_key_env: str = Field(default="ANTHROPIC_API_KEY", pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")
