"""Harbor 0.25适配器；只安装协议客户端并启动现有Harnessix App Server。

镜像可预装Harnessix，也可在安装阶段上传离线安装包（自带Python的目录归档）和原生配置模板；
运行结束后把补丁与产品状态库导出到/logs/agent，供官方判分与逐步记录使用。
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from uuid import UUID

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

try:  # Harbor按import_path加载，仓库根目录在PYTHONPATH上
    from integrations.harbor.patch_export import (
        parse_object_id,
        patch_command,
        workspace_tree_command,
    )
    from integrations.harbor.workspace_modes import normalize_modes_command
except ImportError:  # pragma: no cover - 与本文件同目录直接运行
    from patch_export import parse_object_id, patch_command, workspace_tree_command
    from workspace_modes import normalize_modes_command

_RUNNER = "/installed-agent/harnessix_protocol_runner.py"
_RELAY = "/installed-agent/model_relay.py"
_BUNDLE = "/installed-agent/harnessix-bundle.tar.gz"
_APPROVAL_MODES = {"manual", "auto-edit", "auto"}
_EVENTS = "harnessix-events.jsonl"
_EXTERNAL_BOUNDARY = "HARNESSIX_EXTERNAL_BOUNDARY"


def _relay_specs(value: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    specs = tuple(value.split(",")) if isinstance(value, str) else tuple(value)
    return tuple(spec.strip() for spec in specs if spec.strip())


class HarnessixAgent(BaseInstalledAgent):
    """使用镜像内已安装的Harnessix；不安装运行依赖、不读取宿主产品凭据。"""

    def __init__(
        self,
        *args,
        server_config: str,
        action_config: str | None = None,
        workspace: str = "/app",
        state_directory: str = "/tmp/harnessix-state",
        python_executable: str = "/opt/harnessix/bin/python",
        max_steps: int = 16,
        max_tokens: int = 50_000,
        timeout_seconds: float = 300.0,
        approval_mode: str = "manual",
        model_relays: str | list[str] | None = None,
        install_bundle: str | None = None,
        bundle_root: str = "/opt",
        server_config_template: str | None = None,
        action_config_template: str | None = None,
        external_boundary: bool = False,
        normalize_workspace_modes: bool = False,
        **kwargs,
    ):
        if kwargs.get("model_name") is not None or kwargs.get("config") is not None:
            raise ValueError("模型与Provider必须由显式Harnessix原生配置选择")
        if kwargs.get("version") is not None:
            raise ValueError("版本必须由镜像内已安装的Harnessix确定")
        paths = [
            PurePosixPath(value)
            for value in (server_config, workspace, state_directory, python_executable)
        ]
        if any(not path.is_absolute() or ".." in path.parts for path in paths):
            raise ValueError("评测路径必须是无父目录跳转的绝对路径")
        if paths[0].is_relative_to(paths[1]) or paths[2].is_relative_to(paths[1]):
            raise ValueError("配置和状态目录必须位于任务工作区外")
        if action_config is not None:
            action_path = PurePosixPath(action_config)
            if (
                not action_path.is_absolute()
                or ".." in action_path.parts
                or action_path.is_relative_to(paths[1])
            ):
                raise ValueError("Action配置必须位于任务工作区外的固定绝对路径")
        if not (1 <= max_steps <= 1000 and max_tokens > 0 and 0 < timeout_seconds <= 86_400):
            raise ValueError("评测预算无效")
        if approval_mode not in _APPROVAL_MODES:
            raise ValueError("审批模式必须是manual、auto-edit或auto")
        for template in (install_bundle, server_config_template, action_config_template):
            if template is not None and not Path(template).is_file():
                raise ValueError("安装包或配置模板必须是宿主机上已存在的文件")
        if action_config_template is not None and action_config is None:
            raise ValueError("Action配置模板需要同时指定容器内的action_config路径")
        if not PurePosixPath(bundle_root).is_absolute():
            raise ValueError("安装包解压目录必须是绝对路径")
        if type(external_boundary) is not bool:
            raise ValueError("external_boundary必须是布尔值")
        if type(normalize_workspace_modes) is not bool:
            raise ValueError("normalize_workspace_modes必须是布尔值")
        super().__init__(*args, **kwargs)
        self.server_config = server_config
        self.action_config = action_config
        self.workspace = workspace
        self.state_directory = state_directory
        self.python_executable = python_executable
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.approval_mode = approval_mode
        self.model_relays = _relay_specs(model_relays)
        self.install_bundle = install_bundle
        self.bundle_root = bundle_root
        self.server_config_template = server_config_template
        self.action_config_template = action_config_template
        # 任务容器即隔离边界（ADR 0121）：向App Server断言边界，原生命令Profile才会装配。
        self.external_boundary = external_boundary
        # 显式开启时，启动前把工作区文件权限规整为Git风格（见workspace_modes）。
        self.normalize_workspace_modes = normalize_workspace_modes

    @staticmethod
    def name() -> str:
        return "harnessix"

    def get_version_command(self) -> str:
        return shlex.join(
            [
                self.python_executable,
                "-c",
                "from importlib.metadata import version; print(version('harnessix'))",
            ]
        )

    async def install(self, environment: BaseEnvironment) -> None:
        if self.install_bundle is not None:
            await environment.upload_file(Path(self.install_bundle), _BUNDLE)
            root = shlex.quote(self.bundle_root)
            await self.exec_as_root(
                environment,
                command=f"mkdir -p {root} && tar -xzf {_BUNDLE} -C {root} && rm -f {_BUNDLE}",
                timeout_sec=300,
            )
        for template, target in (
            (self.server_config_template, self.server_config),
            (self.action_config_template, self.action_config),
        ):
            if template is not None and target is not None:
                parent = shlex.quote(str(PurePosixPath(target).parent))
                await self.exec_as_root(environment, command=f"mkdir -p {parent}", timeout_sec=30)
                await environment.upload_file(Path(template), target)
        if self.normalize_workspace_modes:
            await self.exec_as_root(
                environment, command=normalize_modes_command(self.workspace), timeout_sec=300
            )
        await self.exec_as_agent(
            environment,
            command=shlex.join([self.python_executable, "-c", "import harnessix.sdk.agent_client"]),
            timeout_sec=30,
        )
        await environment.upload_file(Path(__file__).with_name("protocol_runner.py"), _RUNNER)
        await environment.upload_file(Path(__file__).with_name("model_relay.py"), _RELAY)

    @with_prompt_template
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        trial_id = UUID(str(self.context_id))
        temporary_input = f"/tmp/harnessix-input-{trial_id}.txt"
        summary = str(self.environment_logs_dir / "harnessix-summary.json")
        events = str(self.environment_logs_dir / _EVENTS)
        with TemporaryDirectory(prefix="harnessix-harbor-") as temporary:
            source = Path(temporary) / "instruction.txt"
            source.write_text(instruction, encoding="utf-8")
            await environment.upload_file(source, temporary_input)
        command = [
            self.python_executable,
            _RUNNER,
            "--instruction-file",
            temporary_input,
            "--server-config",
            self.server_config,
            "--workspace",
            self.workspace,
            "--state-directory",
            f"{self.state_directory}/{trial_id}",
            "--output",
            summary,
            "--trial-id",
            str(trial_id),
            "--max-steps",
            str(self.max_steps),
            "--max-tokens",
            str(self.max_tokens),
            "--timeout-seconds",
            str(self.timeout_seconds),
            "--events-output",
            events,
        ]
        if self.action_config is not None:
            command.extend(["--action-config", self.action_config])
        command.extend(["--approval-mode", self.approval_mode])
        for spec in self.model_relays:
            command.extend(["--model-relay", spec])
        base = await self._head(environment)
        env = dict(self._extra_env or {})
        if self.external_boundary:
            env[_EXTERNAL_BOUNDARY] = "1"
        try:
            await self.exec_as_agent(
                environment,
                command=shlex.join(command),
                env=env or None,
                cwd=self.workspace,
                timeout_sec=int(self.timeout_seconds) + 15,
            )
        finally:
            await self.exec_as_agent(
                environment, command=f"rm -f -- {shlex.quote(temporary_input)}", timeout_sec=5
            )
            await self._export_evidence(environment, base, trial_id)

    async def _head(self, environment: BaseEnvironment) -> str | None:
        """记录运行前的工作区快照（树对象）；工作区不是Git仓库时不导出补丁。"""
        try:
            result = await self.exec_as_agent(
                environment, command=workspace_tree_command(self.workspace), timeout_sec=120
            )
        except RuntimeError:
            return None
        return parse_object_id(getattr(result, "stdout", None))

    async def _export_evidence(
        self, environment: BaseEnvironment, base: str | None, trial_id: UUID
    ) -> None:
        """导出相对基线的完整补丁并归档本次产品状态；失败不改变结果。"""
        if base is not None:
            try:
                await self.exec_as_agent(
                    environment,
                    command=patch_command(self.workspace, base, self.environment_logs_dir),
                    timeout_sec=120,
                )
            except RuntimeError:
                pass
        logs = shlex.quote(str(self.environment_logs_dir))
        state = shlex.quote(self.state_directory)
        try:
            await self.exec_as_agent(
                environment,
                command=f"tar -czf {logs}/harnessix-state.tar.gz -C {state} {trial_id}",
                timeout_sec=120,
            )
        except RuntimeError:
            pass

    def populate_context_post_run(self, context: AgentContext) -> None:
        """回填观测元数据；没有全量Attempt证据时保留Harbor总用量与费用未知。"""

        source = self.logs_dir / "harnessix-summary.json"
        try:
            if source.is_symlink() or not source.is_file() or source.stat().st_size > 65_536:
                raise ValueError("Harbor协议摘要缺失或过大")
            result = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise ValueError("Harbor协议摘要不是对象")
        except (OSError, ValueError):
            # 摘要缺失或无效时不写原生元数据：解析器判为协议证据缺失（基础设施问题），
            # 不在此抛错，否则Harbor会中断整个Job、其余试次无法完成。
            return
        if result.get("spec_version") != "harnessix.harbor-protocol-result/v1" or result.get(
            "trial_id"
        ) != str(self.context_id):
            raise ValueError("Harbor协议摘要身份不匹配")
        for field in ("input_tokens", "output_tokens", "transient_read_errors"):
            value = result.get(field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Harbor协议摘要计数无效")
        for field in ("thread_id", "turn_id"):
            if result.get(field) is not None:
                UUID(result[field])
        context.metadata = {
            **(context.metadata or {}),
            "harnessix": {
                "trial_id": str(self.context_id),
                "thread_id": result.get("thread_id"),
                "turn_id": result.get("turn_id"),
                "turn_status": result.get("turn_status"),
                "native_failure_category": result.get("failure_category"),
                "native_failure_code": result.get("failure_code"),
                "observed_model_steps": result.get("model_steps"),
                "protocol_reason": result.get("reason"),
                "runner_error": result.get("runner_error"),
                "transient_read_errors": result.get("transient_read_errors"),
                "cost_completeness": "unknown",
                "usage_source": "agent_protocol_turn",
                "usage_completeness": "unknown",
                "observed_input_tokens": result.get("input_tokens"),
                "observed_output_tokens": result.get("output_tokens"),
                "turn_terminal_confirmed": result.get("turn_terminal_confirmed"),
                "protocol_evidence_complete": result.get("protocol_evidence_complete"),
                "tool_results_succeeded": result.get("tool_results_succeeded"),
                "final_assistant_sha256": result.get("final_assistant_sha256"),
                "approval_mode": result.get("approval_mode"),
                "auto_approvals": result.get("auto_approvals"),
                "patch_exported": (self.logs_dir / "patch.diff").is_file(),
                "state_exported": (self.logs_dir / "harnessix-state.tar.gz").is_file(),
                "events_exported": (self.logs_dir / _EVENTS).is_file(),
                "external_boundary": self.external_boundary,
                "workspace_modes_normalized": self.normalize_workspace_modes,
            },
        }
