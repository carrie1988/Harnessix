"""解析`harnessix code`参数并装配TUI、客户端状态和stdio服务。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from harnessix.product_ui.contracts import workspace_identity_fingerprint
from harnessix.product_ui.controller import ProductController, StartRequest
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.session import RecoverableAgentSession
from harnessix.product_ui.state_store import ClientStateStore
from harnessix.sdk import AgentSDKError, SubprocessAgentTransport

if TYPE_CHECKING:
    from harnessix.product_ui.app import ProductApp


def _parser() -> argparse.ArgumentParser:
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


def code_main(argv: Sequence[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    try:
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
