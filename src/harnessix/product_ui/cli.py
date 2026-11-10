"""解析`harnessix code`参数并装配TUI、客户端状态和stdio服务。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.models.config import is_private_network_host
from harnessix.product_config.codec import load_product_config
from harnessix.product_config.contracts import ProductConfigSnapshot, ProviderKind
from harnessix.product_config.endpoint_probe import EndpointProbe, probe_provider_endpoint
from harnessix.product_config.errors import ProductConfigError
from harnessix.product_config.mcp_status import mcp_doctor_lines
from harnessix.product_config.preflight import ProductPreflightRequest, run_product_preflight
from harnessix.product_config.product_contracts import (
    ConfigurationDraft,
    PreflightMode,
    ProductPreflightReport,
)
from harnessix.product_config.wizard import (
    ConfigurationWriteRequest,
    known_context_window,
    thinking_preset,
    write_product_config,
)
from harnessix.product_config.zero_config import zero_config_wanted
from harnessix.product_ui.contracts import workspace_identity_fingerprint
from harnessix.product_ui.controller import ProductController, StartRequest
from harnessix.product_ui.cost_estimate import chain_list_price
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.git_detection import detected_git_executable
from harnessix.product_ui.preflight_text import remediation_text, sandbox_status_line
from harnessix.product_ui.session import RecoverableAgentSession
from harnessix.product_ui.state_store import ClientStateStore
from harnessix.product_ui.workspace_trust import confirm_workspace_trust
from harnessix.sdk import AgentSDKError, SubprocessAgentTransport

if TYPE_CHECKING:
    from harnessix.product_ui.app import ProductApp


def _start_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harnessix code",
        description="启动Harnessix Code全屏终端产品",
    )
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--config")
    parser.add_argument("--action-config")
    parser.add_argument("--profile")
    parser.add_argument("--state-directory")
    parser.add_argument("--resume", type=UUID, metavar="THREAD_ID")
    parser.add_argument("--git-executable")
    parser.add_argument("--expected-active-sha256")
    parser.add_argument("--expected-active-profile")
    parser.add_argument("--expected-active-action-sha256")
    return parser


def _configure_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harnessix code configure",
        description="生成不含API Key值的Harnessix Code产品配置",
    )
    parser.add_argument("--config")
    parser.add_argument("--provider-kind", choices=("openai_chat", "anthropic"))
    parser.add_argument("--provider-id", default="primary")
    parser.add_argument("--profile-id", default="primary")
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--secret-name", default="model-api-key")
    parser.add_argument("--secret-version", default="environment-v1")
    parser.add_argument("--api-key-env", default="MODEL_API_KEY")
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
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--non-interactive", action="store_true")
    return parser


def _thinking_options(
    arguments: argparse.Namespace,
    provider_kind: str,
    base_url: str,
    model: str,
    *,
    self_hosted: bool,
) -> dict[str, object]:
    """把思考相关参数解析为草案字段：auto只对已求证端点预设，显式值优先。"""
    preset = (
        thinking_preset(base_url, model, self_hosted=self_hosted)
        if provider_kind == "openai_chat"
        else None
    )
    protocol = arguments.thinking_protocol
    if protocol == "auto":
        protocol = preset[0] if preset is not None else None
    elif protocol == "none":
        protocol = None
    omit = arguments.tool_choice == "omit" or (
        arguments.tool_choice == "auto" and preset is not None and preset[1]
    )
    return {"thinking_protocol": protocol, "omit_tool_choice": omit, "thinking": arguments.thinking}


def _doctor_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harnessix code doctor",
        description="离线检查Harnessix Code启动条件",
    )
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--config")
    parser.add_argument("--action-config")
    parser.add_argument("--profile")
    parser.add_argument("--state-directory")
    parser.add_argument("--git-executable")
    parser.add_argument("--no-tui", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--probe-model",
        action="store_true",
        help="额外在线探测活动Profile的端点：模型是否存在、工具调用参数能否解析",
    )
    return parser


def _workspace(value: str) -> Path:
    try:
        path = Path(value).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ProductUIError("product_workspace_invalid", "产品Workspace不可用") from None
    if not path.is_dir():
        raise ProductUIError("product_workspace_invalid", "产品Workspace不是目录")
    return path


def _default_config() -> Path:
    configured = os.getenv("HARNESSIX_PRODUCT_CONFIG")
    return Path(configured).absolute() if configured else Path.home() / ".harnessix" / "config.json"


def _default_state_directory(workspace: Path) -> Path:
    configured = os.getenv("HARNESSIX_PRODUCT_STATE_DIRECTORY")
    if configured:
        return Path(configured).absolute()
    fingerprint = workspace_identity_fingerprint(str(workspace))
    return Path.home() / ".harnessix" / "workspaces" / fingerprint


def _chain_pricing(config: Path, profile: str | None) -> Literal["free", "unknown"]:
    """读取产品配置判断活动Profile链是否全为免费本地端点；读不到时按费用未知处理。"""

    try:
        snapshot = load_product_config(config)
    except (OSError, ValueError, KernelError):
        return "unknown"
    if not isinstance(snapshot, ProductConfigSnapshot):
        return "unknown"
    try:
        return snapshot.config.chain_pricing(profile)
    except (KeyError, ValueError):
        return "unknown"


_PROBE_THINKING_OFF: dict[str, dict[str, object]] = {
    "vllm_chat_template": {"chat_template_kwargs": {"enable_thinking": False}},
    "dashscope_enable_thinking": {"enable_thinking": False},
    "deepseek_thinking_type": {"thinking": {"type": "disabled"}},
}


def _probe_model(arguments: argparse.Namespace) -> list[EndpointProbe]:
    """按产品配置探测活动Profile的OpenAI兼容端点；配置不可读时报告失败而不抛出。"""

    config_path = Path(arguments.config).absolute() if arguments.config else _default_config()
    try:
        snapshot = load_product_config(config_path)
        if not isinstance(snapshot, ProductConfigSnapshot):
            raise ValueError
        config = snapshot.config
        profile = config.profile_chain(arguments.profile)[0]
        provider = next(
            item for item in config.providers if item.provider_id == profile.provider_id
        )
        variable = next(
            source.environment_variable
            for source in config.secret_sources
            if source.secret == provider.credential
        )
    except (OSError, ValueError, KernelError, StopIteration, KeyError):
        return [EndpointProbe("config", "failed", "产品配置不可读或缺少活动Profile", 0.0)]
    if provider.kind != "openai_chat":
        return [EndpointProbe("endpoint", "warning", "仅支持探测OpenAI兼容端点", 0.0)]
    api_key = os.environ.get(variable)
    if not api_key:
        return [EndpointProbe("credential", "failed", f"环境变量{variable}未设置", 0.0)]
    protocol = provider.thinking_protocol
    return probe_provider_endpoint(
        provider,
        profile.model,
        api_key,
        os.environ,
        extra_body=_PROBE_THINKING_OFF.get(protocol) if protocol else None,
    )


def _action_config(value: str | None) -> Path | None:
    configured = value or os.getenv("HARNESSIX_PRODUCT_ACTION_CONFIG")
    return Path(configured).absolute() if configured else None


def _server_command(
    *,
    config: Path,
    action_config: Path | None = None,
    profile: str | None,
    workspace: Path,
    runtime_state: Path,
    git_executable: str | None,
    expected_active_sha256: str | None = None,
    expected_active_profile: str | None = None,
    expected_active_action_sha256: str | None = None,
    unattended: bool = False,
) -> tuple[str, ...]:
    command = [
        sys.executable,
        "-m",
        "harnessix",
        "agent-server",
        "--config",
        str(config),
        "--workspace",
        str(workspace),
        "--state-directory",
        str(runtime_state),
    ]
    if profile is not None:
        command.extend(("--profile", profile))
    if action_config is not None:
        command.extend(("--action-config", str(action_config)))
    elif zero_config_wanted(action_config, os.environ):
        # 与预检同一判定；App Server据此生成同一份零配置命令工具。
        command.append("--zero-config-actions")
    if git_executable is not None:
        command.extend(("--git-executable", git_executable))
    if unattended:
        command.append("--unattended")
    if expected_active_sha256 is not None:
        command.extend(("--expected-active-sha256", expected_active_sha256))
    if expected_active_profile is not None:
        command.extend(("--expected-active-profile", expected_active_profile))
    if expected_active_action_sha256 is not None:
        command.extend(("--expected-active-action-sha256", expected_active_action_sha256))
    return tuple(command)


def _product_app_type() -> type[ProductApp]:
    try:
        from harnessix.product_ui.app import ProductApp
    except ModuleNotFoundError as error:
        if error.name == "textual" or (error.name or "").startswith("textual."):
            raise ProductUIError(
                "tui_dependency_missing",
                "缺少终端依赖，请安装 harnessix[tui]",
            ) from None
        raise
    return ProductApp


def _emit_error(error: ProductUIError) -> None:
    print(
        json.dumps(
            {"code": error.code, "message": error.message, "retryable": error.retryable},
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ),
        file=sys.stderr,
    )


def _report_json(report: ProductPreflightReport) -> str:
    return json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )


def _render_preflight(report: ProductPreflightReport) -> str:
    labels = {"passed": "通过", "failed": "失败", "skipped": "跳过"}
    lines = [
        f"Harnessix Code检查结论：{'可启动' if report.ready else '不可启动'}",
        f"模式：{report.mode}；平台：{report.platform}；报告摘要：{report.report_sha256}",
    ]
    for check in report.checks:
        remediation = _remediation_suffix(check.remediation_id)
        lines.append(
            f"[{labels[check.status]}] {check.category}/{check.check_id}: {check.code}{remediation}"
        )
    return "\n".join(lines)


def _prompt(value: str | None, label: str, *, non_interactive: bool) -> str:
    if value is not None and value.strip():
        return value
    if non_interactive:
        raise ProductUIError("product_configure_arguments_required", f"缺少配置字段：{label}")
    try:
        entered = input(f"{label}: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise ProductUIError("product_configure_interrupted", "配置向导已中断") from None
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
    try:
        answer = input("这是你自己部署的模型吗？选是才允许明文HTTP并按本地模型配置 [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        raise ProductUIError("product_configure_interrupted", "配置向导已中断") from None
    return answer.strip().lower() in {"y", "yes"}


def _configure(argv: Sequence[str]) -> None:
    arguments = _configure_parser().parse_args(argv)
    try:
        provider_kind = cast(
            ProviderKind,
            _prompt(
                arguments.provider_kind,
                "Provider类型(openai_chat/anthropic)",
                non_interactive=arguments.non_interactive,
            ),
        )
        base_url = _prompt(
            arguments.base_url,
            "Provider Base URL",
            non_interactive=arguments.non_interactive,
        )
        model = _prompt(arguments.model, "模型标识", non_interactive=arguments.non_interactive)
        self_hosted = _self_hosted_choice(arguments, base_url)
        draft = ConfigurationDraft.model_validate(
            {
                "provider_kind": provider_kind,
                "provider_id": arguments.provider_id,
                "profile_id": arguments.profile_id,
                "base_url": base_url,
                "model": model,
                "secret_name": arguments.secret_name,
                "secret_version": arguments.secret_version,
                "environment_variable": arguments.api_key_env,
                "output_token_parameter": arguments.output_token_parameter,
                "self_hosted": self_hosted,
                "proxy_url": arguments.proxy_url,
                "context_window_tokens": arguments.context_window_tokens
                or (
                    known_context_window(base_url, model)
                    if provider_kind == "openai_chat"
                    else None
                ),
                **_thinking_options(
                    arguments, provider_kind, base_url, model, self_hosted=self_hosted
                ),
            }
        )
        receipt = write_product_config(
            ConfigurationWriteRequest(
                path=Path(arguments.config).absolute() if arguments.config else _default_config(),
                draft=draft,
                expected_source_sha256=arguments.expected_source_sha256,
                replace=arguments.replace,
            )
        )
        print(
            json.dumps(
                receipt.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
        )
    except ProductConfigError as error:
        _emit_error(ProductUIError(error.code, error.message, retryable=error.retryable))
        raise SystemExit(2) from None
    except ValidationError:
        _emit_error(ProductUIError("product_configure_invalid", "配置向导参数不符合契约"))
        raise SystemExit(2) from None
    except ProductUIError as error:
        _emit_error(error)
        raise SystemExit(2) from None
    except Exception:
        _emit_error(ProductUIError("product_internal_failure", "配置向导发生内部错误"))
        raise SystemExit(2) from None


def _preflight_request(
    arguments: argparse.Namespace,
    *,
    mode: PreflightMode,
    require_tui: bool,
) -> ProductPreflightRequest:
    workspace = Path(arguments.workspace).absolute()
    try:
        workspace_identity = workspace.resolve(strict=True)
    except (OSError, RuntimeError):
        workspace_identity = workspace
    state_root = (
        Path(arguments.state_directory).absolute()
        if arguments.state_directory
        else _default_state_directory(workspace_identity)
    )
    action_config = _action_config(arguments.action_config)
    return ProductPreflightRequest(
        mode=mode,
        config_path=Path(arguments.config).absolute() if arguments.config else _default_config(),
        action_config_path=action_config,
        zero_config_actions=zero_config_wanted(action_config, os.environ),
        profile_id=arguments.profile,
        workspace=workspace,
        state_directory=state_root,
        git_executable=(
            Path(arguments.git_executable).absolute() if arguments.git_executable else None
        ),
        require_tui=require_tui,
    )


def _doctor(argv: Sequence[str]) -> None:
    arguments = _doctor_parser().parse_args(argv)
    arguments.git_executable = detected_git_executable(
        arguments.git_executable, Path(arguments.workspace).absolute()
    )
    try:
        report = run_product_preflight(
            _preflight_request(
                arguments,
                mode="doctor",
                require_tui=not arguments.no_tui,
            )
        )
        probes = _probe_model(arguments) if arguments.probe_model else []
        if arguments.json:
            document = json.loads(_report_json(report))
            if arguments.probe_model:
                document["model_probe"] = [asdict(item) for item in probes]
            print(json.dumps(document, ensure_ascii=False, sort_keys=True))
        else:
            print(_render_preflight(report))
            for item in probes:
                print(
                    f"- 端点探测 {item.check}：{item.status}（{item.detail}，{item.seconds:.1f}s）"
                )
            sys.stdout.writelines(f"{line}\n" for line in mcp_doctor_lines(os.environ))
        healthy = all(item.status != "failed" for item in probes)
        raise SystemExit(0 if report.ready and healthy else 2)
    except SystemExit:
        raise
    except ProductConfigError as error:
        _emit_error(ProductUIError(error.code, error.message, retryable=error.retryable))
        raise SystemExit(2) from None
    except Exception:
        _emit_error(ProductUIError("product_internal_failure", "产品检查发生内部错误"))
        raise SystemExit(2) from None


def _remediation_suffix(remediation_id: str | None) -> str:
    if remediation_id is None:
        return ""
    text = remediation_text(remediation_id)
    return f"；修复动作：{remediation_id}" + (f"（{text}）" if text else "")


def code_main(argv: Sequence[str] | None = None) -> None:
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] == "configure":
        _configure(values[1:])
        return
    if values and values[0] == "doctor":
        _doctor(values[1:])
        return
    arguments = _start_parser().parse_args(values)
    if not confirm_workspace_trust(Path(arguments.workspace).absolute()):
        raise SystemExit(2)
    arguments.git_executable = detected_git_executable(
        arguments.git_executable, Path(arguments.workspace).absolute()
    )
    try:
        preflight = run_product_preflight(
            _preflight_request(
                arguments,
                mode="startup",
                require_tui=True,
            )
        )
        if not preflight.ready:
            print(_render_preflight(preflight), file=sys.stderr)
            raise SystemExit(2)
        app_type = _product_app_type()
        workspace = _workspace(arguments.workspace)
        config = Path(arguments.config).absolute() if arguments.config else _default_config()
        state_root = (
            Path(arguments.state_directory).absolute()
            if arguments.state_directory
            else _default_state_directory(workspace)
        )
        command = _server_command(
            config=config,
            action_config=_action_config(arguments.action_config),
            profile=arguments.profile,
            workspace=workspace,
            runtime_state=state_root / "runtime",
            git_executable=arguments.git_executable,
            expected_active_sha256=arguments.expected_active_sha256,
            expected_active_profile=arguments.expected_active_profile,
            expected_active_action_sha256=arguments.expected_active_action_sha256,
        )
        with ClientStateStore(state_root, workspace_identity=str(workspace)) as store:
            session = RecoverableAgentSession(
                store,
                lambda: SubprocessAgentTransport(command),
            )
            controller = ProductController(
                session,
                pricing=_chain_pricing(config, arguments.profile),
                list_price=chain_list_price(config, arguments.profile),
                sandbox=sandbox_status_line(preflight),
            )
            report = app_type(
                controller,
                StartRequest(
                    workspace=str(workspace),
                    resume_thread_id=arguments.resume,
                ),
            ).run()
        if report is not None and not report.clean:
            raise SystemExit(2)
    except SystemExit:
        raise
    except AgentSDKError as error:
        _emit_error(ProductUIError(error.code, error.message, retryable=error.retryable))
        raise SystemExit(2) from None
    except ProductUIError as error:
        _emit_error(error)
        raise SystemExit(2) from None
    except Exception:
        _emit_error(ProductUIError("product_internal_failure", "终端产品发生内部错误"))
        raise SystemExit(2) from None
