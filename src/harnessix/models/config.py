"""模型Provider适配：定义Provider连接、重试与HTTP限制配置。"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

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


# 允许明文HTTP的本机回环主机名（urlsplit去掉IPv6方括号后的形式）。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# 用户显式声明自托管时可用的内网范围：RFC1918、CGNAT（Tailscale等）与IPv6 ULA。
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7")
)
_PRIVATE_NAME_SUFFIXES = (".local", ".lan", ".internal", ".intranet", ".home.arpa")


def is_private_network_host(hostname: str | None) -> bool:
    """回环之外的内网主机：私网IP、单段主机名或内网专用后缀；公网IP和公网域名一律为否。"""

    if not hostname or hostname in LOOPBACK_HOSTS:
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        name = hostname.lower().rstrip(".")
        return bool(name) and ("." not in name or name.endswith(_PRIVATE_NAME_SUFFIXES))
    return any(address in network for network in _PRIVATE_NETWORKS)


def validate_endpoint_url(value: str, *, private_network: bool = False) -> str:
    """校验并规范模型端点；明文HTTP只限本机回环，或已显式声明的内网自托管端点。"""

    url = urlsplit(value)
    # 访问 port 触发标准库对非数字和越界端口的验证。
    _ = url.port
    plain_http_allowed = url.hostname in LOOPBACK_HOSTS or (
        private_network and is_private_network_host(url.hostname)
    )
    if (
        not (url.scheme == "https" or (url.scheme == "http" and plain_http_allowed))
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or any(character.isspace() for character in value)
    ):
        raise ValueError(
            "端点必须是无用户信息、查询串或 fragment 的 HTTPS URL"
            "（本机回环或已声明的内网自托管端点可用 HTTP）"
        )
    return value.rstrip("/")


def with_validated_base_url(data: object, *, private_network_field: str) -> object:
    """供model_validator(mode="before")使用：按同一对象上的显式内网声明校验base_url。"""

    if not isinstance(data, Mapping) or not isinstance(data.get("base_url"), str):
        return data
    private_network = data.get(private_network_field) is True
    return {
        **data,
        "base_url": validate_endpoint_url(data["base_url"], private_network=private_network),
    }


# 显式本机HTTP代理（如http://127.0.0.1:7890），不允许凭据或远程代理；产品另按环境变量选代理（models/proxy.py）。
LoopbackProxyUrl = Annotated[
    str | None,
    Field(
        pattern=r"^http://(127\.0\.0\.1|localhost|\[::1\]):[1-9][0-9]{0,4}$",
        exclude_if=lambda value: value is None,
    ),
]


class ModelHTTPConfig(ContractModel):
    base_url: str = Field(max_length=2048)
    model: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/-]+$")
    api_key_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")
    capabilities: ChatCapabilities = Field(default_factory=ChatCapabilities)
    max_output_tokens: int = Field(default=1024, ge=1, le=1_000_000)
    timeout_seconds: float = Field(default=60, gt=0, le=3600, allow_inf_nan=False)
    io_timeout_seconds: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    max_attempts: int = Field(default=2, ge=1, le=6)
    retry_delay_seconds: float = Field(default=0.5, ge=0, le=10, allow_inf_nan=False)
    max_request_bytes: int = Field(default=2_097_152, ge=1024, le=16_777_216)
    max_response_bytes: int = Field(default=2_097_152, ge=1024, le=16_777_216)
    max_frame_bytes: int = Field(default=262_144, ge=128, le=1_048_576)
    max_chunks: int = Field(default=10_000, ge=1, le=100_000)
    # 用户显式声明的内网自托管端点才可用明文HTTP；默认值不进入序列化与恢复指纹。
    private_network_endpoint: bool = Field(default=False, exclude_if=lambda value: value is False)

    @classmethod
    def validate_url(cls, value: str) -> str:
        """严格校验：HTTPS或本机回环HTTP，不接受内网明文端点。"""

        return validate_endpoint_url(value)

    @model_validator(mode="before")
    @classmethod
    def validated_base_url(cls, data: object) -> object:
        return with_validated_base_url(data, private_network_field="private_network_endpoint")


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
        description="显式思考Token上限；到达后模型停止思考并作答（按端点与模型核实后使用）",
    )
    # 思考强度档位（如DashScope的reasoning_effort）：随开启思考的请求原样发送；
    # 与思考Token上限二选一。
    reasoning_effort: str | None = Field(
        default=None, pattern=r"^[a-z]{1,16}$", exclude_if=lambda value: value is None
    )
    # 仅在TCP连接或TLS握手失败时重连（请求字节尚未发出，不产生计费或新的模型尝试）。
    connect_retries: int = Field(default=0, ge=0, le=5, exclude_if=lambda value: value == 0)
    proxy_url: LoopbackProxyUrl = None
    # 工具参数严格解析失败时只修复字符串内原始控制字符与非法反斜杠转义；其他畸形仍为协议失败。
    repair_tool_arguments: bool = Field(default=False, exclude_if=lambda value: value is False)
    # 工具结果线上正文：json发送规范JSON；text把搜索、读取与命令结果渲染为紧凑文本
    # （models/tool_result_text.py），其余结果仍为JSON。默认值不进入序列化与恢复指纹。
    tool_result_format: Literal["json", "text"] = Field(
        default="json", exclude_if=lambda value: value == "json"
    )
    # 思考开关线上格式：默认DashScope的enable_thinking；thinking_type发送thinking.type。
    thinking_format: Literal["thinking_type", "chat_template_kwargs"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    # 前缀缓存布局：稳定前缀（指令＋历史）在前，逐请求变化的预算/目录说明移到末尾；
    # explicit_tail另在前缀末端打缓存标记，stable_tail供服务端自动前缀缓存、不打标记。
    prompt_cache: Literal["explicit_tail", "stable_tail"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    # 自动前缀缓存只报告命中数（如DashScope隐式缓存）：缓存创建无附加费，未命中输入全部按未缓存计。
    implicit_cache_usage: bool = Field(default=False, exclude_if=lambda value: value is False)
    # 端点的强制工具选择不可靠（如百炼DeepSeek的约束解码语法编译间歇超时并返回400）时
    # 不发送tool_choice；required与指定工具仍由可执行集收窄、目录说明与Runtime校验约束。
    omit_tool_choice: bool = Field(default=False, exclude_if=lambda value: value is False)
    # 只在提供工具的步骤开启思考：摘要与收尾回复等无工具请求的输出预算小，推理会耗尽而无正文。
    thinking_tool_steps_only: bool = Field(default=False, exclude_if=lambda value: value is False)

    @model_validator(mode="after")
    def validate_implicit_cache_usage(self) -> Self:
        if self.implicit_cache_usage and self.prompt_cache != "stable_tail":
            raise ValueError("隐式缓存用量口径只用于不打缓存标记的自动前缀缓存")
        if self.thinking_tool_steps_only and self.enable_thinking is not True:
            raise ValueError("按工具步骤开启思考要求显式开启思考")
        return self

    @model_validator(mode="after")
    def validate_thinking_budget(self) -> Self:
        if self.thinking_budget is not None and (
            self.enable_thinking is not True or self.thinking_budget >= self.max_output_tokens
        ):
            raise ValueError("思考预算要求显式开启思考，且低于输出上限")
        bounded = self.thinking_budget is not None or self.reasoning_effort is not None
        if self.thinking_format is not None and (self.enable_thinking is None or bounded):
            raise ValueError("显式思考格式要求显式思考开关，且不支持思考预算与强度档位")
        if self.reasoning_effort is not None and (
            self.enable_thinking is not True or self.thinking_budget is not None
        ):
            raise ValueError("思考强度档位要求显式开启思考，且不能与思考预算同时设置")
        return self


class AnthropicConfig(ModelHTTPConfig):
    base_url: str = Field(default="https://api.anthropic.com", max_length=2048)
    api_key_env: str = Field(default="ANTHROPIC_API_KEY", pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")
    proxy_url: LoopbackProxyUrl = None
