from __future__ import annotations

from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from harnessix.domain.models import ContractModel


class ChatCapabilities(ContractModel):
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


class AnthropicConfig(ModelHTTPConfig):
    base_url: str = Field(default="https://api.anthropic.com", max_length=2048)
    api_key_env: str = Field(default="ANTHROPIC_API_KEY", pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")
