from __future__ import annotations

from typing import Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from harnessix.agent.errors import FailureCategory
from harnessix.agent.models import Budget, TurnStatus
from harnessix.domain.models import ContractModel
from harnessix.models.config import (
    AnthropicConfig,
    ChatCapabilities,
    ModelHTTPConfig,
    OpenAIChatConfig,
)
from harnessix.models.contracts import ResponseFailed

ProviderKind = Literal["openai_chat", "anthropic"]
Scenario = Literal["text", "tool", "approval"]
Execution = Literal["sdk_default", "injected"]
SmokeReason = Literal[
    "passed",
    "network_not_enabled",
    "configuration_invalid",
    "dependency_missing",
    "runtime_failed",
    "check_failed",
    "internal_error",
    "cancelled",
]


class SmokeConfig(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    spec_version: Literal["harnessix.model-smoke-config/v1"] = "harnessix.model-smoke-config/v1"
    provider: ProviderKind
    base_url: str = Field(max_length=2048)
    model: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/-]+$")
    api_key_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")
    scenario: Scenario = "text"
    max_output_tokens: int = Field(default=128, ge=1, le=512)
    max_tokens: int = Field(default=2048, ge=1, le=8192)
    timeout_seconds: float = Field(default=30, gt=0, le=60, allow_inf_nan=False)
    output_token_parameter: Literal["max_completion_tokens", "max_tokens"] | None = None

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return ModelHTTPConfig.validate_url(value)

    @model_validator(mode="after")
    def validate_provider_option(self) -> Self:
        if self.provider == "anthropic" and self.output_token_parameter is not None:
            raise ValueError("Anthropic 不接受 Chat 专属参数")
        return self

    def provider_config(self) -> OpenAIChatConfig | AnthropicConfig:
        common = ModelHTTPConfig(
            base_url=self.base_url,
            model=self.model,
            api_key_env=self.api_key_env,
            capabilities=ChatCapabilities(
                tool_calls=self.scenario != "text", parallel_tool_calls=False
            ),
            max_output_tokens=self.max_output_tokens,
            max_attempts=1,
            retry_delay_seconds=0,
            timeout_seconds=self.timeout_seconds,
            io_timeout_seconds=min(10, self.timeout_seconds),
            max_request_bytes=65536,
            max_response_bytes=524288,
            max_frame_bytes=65536,
            max_chunks=2048,
        ).model_dump()
        if self.provider == "openai_chat":
            return OpenAIChatConfig(
                **common,
                output_token_parameter=self.output_token_parameter or "max_completion_tokens",
            )
        return AnthropicConfig(**common)

    def budget(self) -> Budget:
        return Budget(
            max_steps=1 if self.scenario == "text" else 2,
            max_tokens=self.max_tokens,
            timeout_seconds=self.timeout_seconds,
            max_output_chars=8192,
            max_tool_calls_per_step=1,
        )


class SmokeReport(ContractModel):
    """仅输出固定枚举与计数，不接受供应商任意字符串。"""

    spec_version: Literal["harnessix.model-smoke-report/v1"] = "harnessix.model-smoke-report/v1"
    provider: ProviderKind | None = None
    scenario: Scenario | None = None
    execution: Execution = "sdk_default"
    reason: SmokeReason
    turn_status: TurnStatus | None = None
    failure_category: FailureCategory | None = None
    provider_failure: ResponseFailed | None = None
    attempts_started: int | None = Field(default=None, ge=0, strict=True)
    known_input_tokens: int | None = Field(default=None, ge=0, strict=True)
    known_output_tokens: int | None = Field(default=None, ge=0, strict=True)
    usage_complete: bool = False
    tool_calls: int | None = Field(default=None, ge=0, strict=True)
    content_verified: bool = False
    approval_restart_verified: bool = False
    replay_verified: bool = False

    @model_validator(mode="after")
    def validate_pass(self) -> Self:
        if self.reason == "passed" and not (
            self.provider is not None
            and self.scenario is not None
            and self.turn_status == TurnStatus.COMPLETED
            and self.failure_category is None
            and self.provider_failure is None
            and self.usage_complete
            and self.content_verified
            and self.replay_verified
            and self.known_input_tokens is not None
            and self.known_output_tokens is not None
            and self.attempts_started == (1 if self.scenario == "text" else 2)
            and self.tool_calls == (0 if self.scenario == "text" else 1)
            and (self.scenario != "approval" or self.approval_restart_verified)
        ):
            raise ValueError("Smoke 通过必须满足全部场景检查")
        return self
