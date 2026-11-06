"""Harbor 0.25适配器；只安装协议客户端并启动现有Harnessix App Server。"""

from __future__ import annotations

import json
import shlex
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from uuid import UUID

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

_RUNNER = "/installed-agent/harnessix_protocol_runner.py"


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
        super().__init__(*args, **kwargs)
        self.server_config = server_config
        self.action_config = action_config
        self.workspace = workspace
        self.state_directory = state_directory
        self.python_executable = python_executable
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds

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
        await self.exec_as_agent(
            environment,
            command=shlex.join([self.python_executable, "-c", "import harnessix.sdk.agent_client"]),
            timeout_sec=30,
        )
        await environment.upload_file(Path(__file__).with_name("protocol_runner.py"), _RUNNER)

    @with_prompt_template
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        trial_id = UUID(str(self.context_id))
        temporary_input = f"/tmp/harnessix-input-{trial_id}.txt"
        summary = str(self.environment_logs_dir / "harnessix-summary.json")
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
        ]
        if self.action_config is not None:
            command.extend(["--action-config", self.action_config])
        try:
            await self.exec_as_agent(
                environment,
                command=shlex.join(command),
                env=self._extra_env,
                cwd=self.workspace,
                timeout_sec=int(self.timeout_seconds) + 15,
            )
        finally:
            await self.exec_as_agent(
                environment, command=f"rm -f -- {shlex.quote(temporary_input)}", timeout_sec=5
            )

    def populate_context_post_run(self, context: AgentContext) -> None:
        """回填观测元数据；没有全量Attempt证据时保留Harbor总用量与费用未知。"""

        source = self.logs_dir / "harnessix-summary.json"
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 65_536:
            raise ValueError("Harbor协议摘要缺失或无效")
        result = json.loads(source.read_text(encoding="utf-8"))
        if result.get("spec_version") != "harnessix.harbor-protocol-result/v1" or result.get(
            "trial_id"
        ) != str(self.context_id):
            raise ValueError("Harbor协议摘要身份不匹配")
        for field in ("input_tokens", "output_tokens"):
            value = result.get(field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Harbor协议摘要用量无效")
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
                "observed_model_steps": result.get("model_steps"),
                "protocol_reason": result.get("reason"),
                "cost_completeness": "unknown",
                "usage_source": "agent_protocol_turn",
                "usage_completeness": "unknown",
                "observed_input_tokens": result.get("input_tokens"),
                "observed_output_tokens": result.get("output_tokens"),
                "turn_terminal_confirmed": result.get("turn_terminal_confirmed"),
                "protocol_evidence_complete": result.get("protocol_evidence_complete"),
                "tool_results_succeeded": result.get("tool_results_succeeded"),
                "final_assistant_sha256": result.get("final_assistant_sha256"),
            },
        }
