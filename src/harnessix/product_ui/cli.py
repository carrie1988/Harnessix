"""解析`harnessix code`参数并装配TUI、客户端状态和stdio服务。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import UUID

from pydantic import ValidationError

from harnessix.product_config.contracts import ProviderKind
from harnessix.product_config.errors import ProductConfigError
from harnessix.product_config.preflight import ProductPreflightRequest, run_product_preflight
from harnessix.product_config.product_contracts import (
    ConfigurationDraft,
    PreflightMode,
    ProductPreflightReport,
)
from harnessix.product_config.wizard import ConfigurationWriteRequest, write_product_config
from harnessix.product_ui.contracts import workspace_identity_fingerprint
from harnessix.product_ui.controller import ProductController, StartRequest
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.session import RecoverableAgentSession
from harnessix.product_ui.state_store import ClientStateStore
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
    parser.add_argument("--profile")
    parser.add_argument("--state-directory")
    parser.add_argument("--resume", type=UUID, metavar="THREAD_ID")
    parser.add_argument("--git-executable")
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
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--non-interactive", action="store_true")
    return parser


def _doctor_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harnessix code doctor",
        description="离线检查Harnessix Code启动条件",
    )
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--config")
    parser.add_argument("--profile")
    parser.add_argument("--state-directory")
    parser.add_argument("--git-executable")
    parser.add_argument("--no-tui", action="store_true")
    parser.add_argument("--json", action="store_true")
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


def _server_command(
    *,
    config: Path,
    profile: str | None,
    workspace: Path,
    runtime_state: Path,
    git_executable: str | None,
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
    if git_executable is not None:
        command.extend(("--git-executable", git_executable))
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
        remediation = f"；修复动作：{check.remediation_id}" if check.remediation_id else ""
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


def _configure(argv: Sequence[str]) -> None:
    arguments = _configure_parser().parse_args(argv)
    try:
        draft = ConfigurationDraft(
            provider_kind=cast(
                ProviderKind,
                _prompt(
                    arguments.provider_kind,
                    "Provider类型(openai_chat/anthropic)",
                    non_interactive=arguments.non_interactive,
                ),
            ),
            provider_id=arguments.provider_id,
            profile_id=arguments.profile_id,
            base_url=_prompt(
                arguments.base_url,
                "Provider Base URL",
                non_interactive=arguments.non_interactive,
            ),
            model=_prompt(
                arguments.model,
                "模型标识",
                non_interactive=arguments.non_interactive,
            ),
            secret_name=arguments.secret_name,
            secret_version=arguments.secret_version,
            environment_variable=arguments.api_key_env,
            output_token_parameter=arguments.output_token_parameter,
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
    return ProductPreflightRequest(
        mode=mode,
        config_path=Path(arguments.config).absolute() if arguments.config else _default_config(),
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
    try:
        report = run_product_preflight(
            _preflight_request(
                arguments,
                mode="doctor",
                require_tui=not arguments.no_tui,
            )
        )
        print(_report_json(report) if arguments.json else _render_preflight(report))
        raise SystemExit(0 if report.ready else 2)
    except SystemExit:
        raise
    except ProductConfigError as error:
        _emit_error(ProductUIError(error.code, error.message, retryable=error.retryable))
        raise SystemExit(2) from None
    except Exception:
        _emit_error(ProductUIError("product_internal_failure", "产品检查发生内部错误"))
        raise SystemExit(2) from None


def code_main(argv: Sequence[str] | None = None) -> None:
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] == "configure":
        _configure(values[1:])
        return
    if values and values[0] == "doctor":
        _doctor(values[1:])
        return
    arguments = _start_parser().parse_args(values)
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
            profile=arguments.profile,
            workspace=workspace,
            runtime_state=state_root / "runtime",
            git_executable=arguments.git_executable,
        )
        with ClientStateStore(state_root, workspace_identity=str(workspace)) as store:
            session = RecoverableAgentSession(
                store,
                lambda: SubprocessAgentTransport(command),
            )
            controller = ProductController(session)
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
