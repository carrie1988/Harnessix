"""`harnessix code configure`：先选服务商和模型，再写入不含API Key值的产品配置。

国内用户不必自己查接口地址和模型名：交互模式先列出DeepSeek、阿里云百炼、智谱和“其他”，再列该家
推荐模型与人民币牌价；地址和Key变量名取自服务商预设，上下文窗口、思考方式与等待时间按已核对的
端点表填写。命令行给出的字段优先且不再询问，`--vendor`供脚本使用。成功后用中文说明下一步；
`--json`保留原有的JSON回执与错误格式。
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from pydantic import ValidationError

from harnessix.models.config import is_private_network_host
from harnessix.product_config.contracts import ProviderKind
from harnessix.product_config.errors import ProductConfigError
from harnessix.product_config.product_contracts import ConfigurationDraft
from harnessix.product_config.vendors import VENDOR_IDS, VENDORS, Vendor, VendorModel, vendor_by_id
from harnessix.product_config.wizard import (
    ConfigurationWriteRequest,
    auto_tool_choice_only,
    current_config_digest,
    effort_tiers_verified,
    known_context_window,
    thinking_preset,
    write_product_config,
)
from harnessix.product_ui.cli import _default_config, _emit_error
from harnessix.product_ui.cost_estimate import chain_model_name, list_price_for
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.startup_failure import api_key_setup_text

_MANUAL_API_KEY_ENV = "MODEL_API_KEY"
_OTHER_VENDOR = "其他兼容 OpenAI 接口的服务（手动填写地址和模型名）"
_OTHER_MODEL = "手动输入其他模型名"
_THINKING_WITHOUT_SWITCH = (
    "这个服务和模型没有已核对的思考开关，--thinking 的设置到不了模型。"
    "请去掉 --thinking；或用 --thinking-protocol 指定开关格式"
    "（dashscope_enable_thinking 或 deepseek_thinking_type）；"
    "确认模型本身不思考时，用 --thinking-protocol none"
)
_EFFORT_NOT_VERIFIED = (
    "这个服务和模型没有核实过思考强度档位或思考上限，"
    "--thinking-effort、--thinking-budget 的设置到不了模型"
    "（目前核实过的是阿里云百炼的 qwen3.8-max、qwen3.8-flash，且没有关闭思考）。请去掉这两个参数"
)
_EFFORT_WITH_BUDGET = (
    "--thinking-effort 和 --thinking-budget 只能设置一个：设置上限后不再发送强度档位"
)


@dataclass(frozen=True, slots=True)
class _Endpoint:
    """选定的接口；手动填写时没有服务商。"""

    vendor: Vendor | None
    provider_kind: ProviderKind
    base_url: str
    model: str
    api_key_env: str


@dataclass(frozen=True, slots=True)
class _Replacement:
    replace: bool
    expected_source_sha256: str | None


class _KeepExisting(Exception):
    """用户选择保留现有配置。"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harnessix code configure",
        description="选择模型服务商和模型，生成不含API Key值的Harnessix Code配置",
    )
    parser.add_argument("--config")
    parser.add_argument(
        "--vendor",
        choices=VENDOR_IDS,
        help="模型服务商：deepseek（DeepSeek 官方）、bailian（阿里云百炼）、zhipu（智谱）",
    )
    parser.add_argument("--provider-kind", choices=("openai_chat", "anthropic"))
    parser.add_argument("--provider-id", default="primary")
    parser.add_argument("--profile-id", default="primary")
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--secret-name", default="model-api-key")
    parser.add_argument("--secret-version", default="environment-v1")
    parser.add_argument(
        "--api-key-env",
        help="存放API Key的环境变量名；默认按服务商（如DASHSCOPE_API_KEY），否则MODEL_API_KEY",
    )
    parser.add_argument(
        "--output-token-parameter",
        choices=("max_completion_tokens", "max_tokens"),
    )
    # auto：已求证端点与模型族默认开启思考（只在工具步骤）；none：不发送任何思考参数。
    parser.add_argument(
        "--thinking-protocol",
        choices=("auto", "none", "dashscope_enable_thinking", "deepseek_thinking_type"),
        default="auto",
    )
    parser.add_argument("--thinking", choices=("tool_steps", "every_step", "off"))
    parser.add_argument(
        "--thinking-effort",
        choices=("low", "medium", "high"),
        help="思考强度：low、medium（已核实模型的默认值）、high；只对核实过强度档位的服务和模型有效",
    )
    parser.add_argument(
        "--thinking-budget",
        type=int,
        help="思考Token上限（高级选项，256起）：到达后模型停止思考并作答；设置后不再发送思考强度",
    )
    parser.add_argument("--tool-choice", choices=("auto", "send", "omit"), default="auto")
    parser.add_argument(
        "--context-window-tokens",
        type=int,
        help="模型的上下文窗口（Token）；已知云模型自动填写，自托管模型建议手动指定",
    )
    parser.add_argument(
        "--self-hosted",
        action="store_true",
        help="声明这是你自己部署在内网的模型端点：允许明文HTTP，并按本地模型配置",
    )
    parser.add_argument(
        "--proxy-url",
        help="经本机HTTP代理访问云端点，如http://127.0.0.1:7890；优先于HTTPS_PROXY等环境变量",
    )
    parser.add_argument(
        "--service-failure-retries",
        type=int,
        help=(
            "模型请求因网络中断、限流、服务端错误失败后最多自动重试的次数，一次请求最多尝试N+1次"
            "（最多6次）；不设置时沿用配置里的尝试次数，0为不重试"
        ),
    )
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--json", action="store_true", help="输出JSON回执与JSON错误，供脚本使用")
    return parser


def configure_main(argv: Sequence[str]) -> None:
    arguments = _parser().parse_args(argv)
    path = Path(arguments.config).absolute() if arguments.config else _default_config()
    try:
        replacement = _replacement(arguments, path)
        endpoint = _endpoint(arguments)
        receipt = write_product_config(
            ConfigurationWriteRequest(
                path=path,
                draft=_draft(arguments, endpoint),
                expected_source_sha256=replacement.expected_source_sha256,
                replace=replacement.replace,
            )
        )
        if arguments.json:
            print(
                json.dumps(
                    receipt.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
            )
        else:
            print(_success_text(path, endpoint))
    except _KeepExisting:
        print("已保留原有配置，没有修改。")
    except ProductConfigError as error:
        message = error.message
        if error.code == "product_config_permissions" and not arguments.json:
            message = _config_directory_problem(path) or message
        _fail(arguments, ProductUIError(error.code, message, retryable=error.retryable))
    except ValidationError:
        _fail(arguments, ProductUIError("product_configure_invalid", "配置向导参数不符合契约"))
    except ProductUIError as error:
        _fail(arguments, error)
    except Exception:
        _fail(arguments, ProductUIError("product_internal_failure", "配置向导发生内部错误"))


def _config_directory_problem(path: Path) -> str | None:
    """配置目录不合规时指出是哪个目录、差在哪里、怎样改；判断不出时返回None，沿用通用提示。"""

    directory = path.parent
    try:
        info = directory.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return f"配置目录 {directory} 不是普通目录（可能是符号链接或文件），请换一个目录"
    if os.name != "posix":
        return None
    if info.st_uid != os.getuid():
        return f"配置目录 {directory} 不属于当前用户，请换一个你自己的目录"
    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o700:
        return (
            f"配置目录 {directory} 的权限是 {mode:o}，需要 700（只允许你自己访问）。"
            f"运行 chmod 700 {shlex.quote(str(directory))} 后重试"
        )
    return None


def _fail(arguments: argparse.Namespace, error: ProductUIError) -> None:
    if arguments.json:
        _emit_error(error)
    else:
        print(f"配置没有完成：{error.message}", file=sys.stderr)
    raise SystemExit(2)


def _replacement(arguments: argparse.Namespace, path: Path) -> _Replacement:
    """交互模式下已有配置时先问是否替换；替换以此刻的文件摘要为期望值，期间被改写则冲突。"""

    explicit = _Replacement(arguments.replace, arguments.expected_source_sha256)
    if arguments.replace or arguments.expected_source_sha256 or arguments.non_interactive:
        return explicit
    if not (path.exists() or path.is_symlink()):
        return explicit
    model = chain_model_name(path, None)
    current = f"（当前模型 {model}）" if model else ""
    answer = _ask(f"已有配置：{path}{current}。重新配置会替换它，继续吗？[y/N]: ")
    if answer.lower() not in {"y", "yes"}:
        raise _KeepExisting
    return _Replacement(True, current_config_digest(path))


def _endpoint(arguments: argparse.Namespace) -> _Endpoint:
    if arguments.vendor:
        return _vendor_endpoint(arguments, vendor_by_id(arguments.vendor))
    if arguments.base_url or arguments.provider_kind:
        return _manual_endpoint(arguments, arguments.provider_kind)
    if arguments.non_interactive:
        raise ProductUIError(
            "product_configure_arguments_required",
            "缺少服务商：用 --vendor 选择 deepseek、bailian 或 zhipu；"
            "其他服务用 --provider-kind、--base-url、--model 填写",
        )
    vendor = _choose_vendor()
    if vendor is None:
        return _manual_endpoint(arguments, "openai_chat")
    return _vendor_endpoint(arguments, vendor)


def _vendor_endpoint(arguments: argparse.Namespace, vendor: Vendor) -> _Endpoint:
    if arguments.provider_kind not in (None, "openai_chat"):
        raise ProductUIError(
            "product_configure_invalid", "--vendor 使用 OpenAI 兼容接口，不能与 Anthropic 接口同用"
        )
    base_url = arguments.base_url or vendor.base_url
    model = arguments.model or (
        vendor.default_model.model if arguments.non_interactive else _choose_model(vendor, base_url)
    )
    api_key_env = arguments.api_key_env or vendor.api_key_env
    return _Endpoint(vendor, "openai_chat", base_url, model, api_key_env)


def _manual_endpoint(arguments: argparse.Namespace, provider_kind: str | None) -> _Endpoint:
    quiet = arguments.non_interactive
    kind = provider_kind or _prompt(None, "Provider类型(openai_chat/anthropic)", quiet)
    base_url = _prompt(arguments.base_url, "接口地址（Base URL）", quiet)
    model = _prompt(arguments.model, "模型名", quiet)
    api_key_env = arguments.api_key_env or _MANUAL_API_KEY_ENV
    return _Endpoint(None, cast(ProviderKind, kind), base_url, model, api_key_env)


def _choose_vendor() -> Vendor | None:
    index = _menu("选择模型服务商：", [item.label for item in VENDORS] + [_OTHER_VENDOR])
    return VENDORS[index] if index < len(VENDORS) else None


def _choose_model(vendor: Vendor, base_url: str) -> str:
    options = [
        _model_option(base_url, item, recommended=index == 0)
        for index, item in enumerate(vendor.models)
    ]
    index = _menu(f"选择模型（{vendor.name}，价格为每百万 Token）：", [*options, _OTHER_MODEL])
    if index < len(vendor.models):
        return vendor.models[index].model
    return _prompt(None, "模型名", False)


def _model_option(base_url: str, item: VendorModel, *, recommended: bool) -> str:
    text = f"{item.model}  {item.note}{'（推荐）' if recommended else ''}"
    price = list_price_for(base_url, item.model)
    if price is None:
        return text
    return f"{text} · 输入 ¥{price.input_per_million} / 输出 ¥{price.output_per_million}"


def _menu(title: str, options: Sequence[str]) -> int:
    """列出带序号的选项；直接回车选第一项，输错时重新询问。"""

    print(title)
    for number, text in enumerate(options, start=1):
        print(f"  {number}) {text}")
    while True:
        answer = _ask("输入序号 [1]: ")
        if not answer:
            return 0
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        print(f"请输入 1～{len(options)} 之间的序号。")


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        raise ProductUIError("product_configure_interrupted", "配置向导已中断") from None


def _prompt(value: str | None, label: str, non_interactive: bool) -> str:
    if value is not None and value.strip():
        return value
    if non_interactive:
        raise ProductUIError("product_configure_arguments_required", f"缺少配置字段：{label}")
    entered = _ask(f"{label}: ")
    if not entered:
        raise ProductUIError("product_configure_arguments_required", f"缺少配置字段：{label}")
    return entered


def _self_hosted_choice(arguments: argparse.Namespace, base_url: str) -> bool:
    """内网端点是否按自托管处理：命令行声明优先，交互模式询问，非交互默认否。"""

    url = urlsplit(base_url)
    if arguments.self_hosted:
        return True
    if not is_private_network_host(url.hostname):
        return False
    if arguments.non_interactive:
        if url.scheme == "http":
            raise ProductUIError(
                "product_configure_self_hosted_required",
                "内网HTTP端点需要显式声明自托管：加 --self-hosted",
            )
        return False
    answer = _ask("这是你自己部署的模型吗？选是才允许明文HTTP并按本地模型配置 [y/N]: ")
    return answer.lower() in {"y", "yes"}


def _draft(arguments: argparse.Namespace, endpoint: _Endpoint) -> ConfigurationDraft:
    self_hosted = _self_hosted_choice(arguments, endpoint.base_url)
    openai_chat = endpoint.provider_kind == "openai_chat"
    return ConfigurationDraft.model_validate(
        {
            "provider_kind": endpoint.provider_kind,
            "provider_id": arguments.provider_id,
            "profile_id": arguments.profile_id,
            "base_url": endpoint.base_url,
            "model": endpoint.model,
            "secret_name": arguments.secret_name,
            "secret_version": arguments.secret_version,
            "environment_variable": endpoint.api_key_env,
            "output_token_parameter": arguments.output_token_parameter,
            "self_hosted": self_hosted,
            "proxy_url": arguments.proxy_url,
            "service_failure_retries": arguments.service_failure_retries,
            "context_window_tokens": arguments.context_window_tokens
            or (known_context_window(endpoint.base_url, endpoint.model) if openai_chat else None),
            **_thinking_options(arguments, endpoint, self_hosted=self_hosted),
        }
    )


def _thinking_options(
    arguments: argparse.Namespace, endpoint: _Endpoint, *, self_hosted: bool
) -> dict[str, object]:
    """把思考相关参数解析为草案字段：auto只对已求证端点预设，显式值优先。

    用户写了--thinking而没有任何协议能把它送达模型时拒绝配置，不生成一份设置不生效的文件。
    """

    openai_chat = endpoint.provider_kind == "openai_chat"
    preset = (
        thinking_preset(endpoint.base_url, endpoint.model, self_hosted=self_hosted)
        if openai_chat
        else None
    )
    protocol = arguments.thinking_protocol
    if protocol == "auto":
        protocol = preset[0] if preset is not None else None
        if protocol is None and arguments.thinking is not None:
            raise ProductUIError("product_configure_invalid", _THINKING_WITHOUT_SWITCH)
    elif protocol == "none":
        protocol = None
    endpoint_omits = (preset is not None and preset[1]) or auto_tool_choice_only(endpoint.base_url)
    omit = arguments.tool_choice == "omit" or (arguments.tool_choice == "auto" and endpoint_omits)
    return {
        "thinking_protocol": protocol,
        "omit_tool_choice": omit,
        "thinking": arguments.thinking,
        **_thinking_bounds(arguments, endpoint, protocol),
    }


def _thinking_bounds(
    arguments: argparse.Namespace, endpoint: _Endpoint, protocol: str | None
) -> dict[str, object]:
    """思考强度与思考Token上限只对核实过的服务和模型生效；到不了模型的设置直接拒绝。"""

    effort, budget = arguments.thinking_effort, arguments.thinking_budget
    if effort is None and budget is None:
        return {}
    if effort is not None and budget is not None:
        raise ProductUIError("product_configure_invalid", _EFFORT_WITH_BUDGET)
    verified = protocol == "dashscope_enable_thinking" and effort_tiers_verified(
        endpoint.base_url, endpoint.model
    )
    if not verified or arguments.thinking == "off":
        raise ProductUIError("product_configure_invalid", _EFFORT_NOT_VERIFIED)
    return {"thinking_effort": effort, "thinking_budget_tokens": budget}


def _success_text(path: Path, endpoint: _Endpoint) -> str:
    """配置成功后的下一步：未设置Key时给出设置命令，再说明怎样启动。"""

    vendor = f"（{endpoint.vendor.name}）" if endpoint.vendor else ""
    price = list_price_for(endpoint.base_url, endpoint.model)
    cost = (
        f" · 每百万 Token 输入 ¥{price.input_per_million} / 输出 ¥{price.output_per_million}"
        if price
        else ""
    )
    if os.environ.get(endpoint.api_key_env):
        key_step = f"已检测到环境变量 {endpoint.api_key_env}，可以直接使用。"
    else:
        key_step = api_key_setup_text((endpoint.api_key_env,))
    first, *rest = key_step.splitlines()
    return "\n".join(
        [
            f"配置已保存：{path}",
            f"模型：{endpoint.model}{vendor}{cost}",
            "",
            "下一步：",
            f"  1. {first}",
            *(f"     {line}" for line in rest),
            "  2. 进入项目目录，运行 harnessix code",
        ]
    )
