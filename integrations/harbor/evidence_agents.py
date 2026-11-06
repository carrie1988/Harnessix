"""Harbor内置智能体的证据增强版：名称与行为不变，只在智能体阶段结束时导出补丁。

- OracleWithPatch / NopWithPatch：环境与判分的正负对照；
- MiniSweAgentOffline：安装步骤与上游相同，但不从astral.sh或GitHub下载uv和Python，
  要求任务镜像已提供uv（派生镜像放在/root/.local/bin），包经镜像内配置的PyPI镜像安装，
  并把安装后的依赖清单写入/logs/agent以便复现。
"""

from __future__ import annotations

import shlex

from harbor.agents.installed.mini_swe_agent import MiniSweAgent
from harbor.agents.nop import NopAgent
from harbor.agents.oracle import OracleAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths

try:  # Harbor按import_path加载，仓库根目录在PYTHONPATH上
    from integrations.harbor.patch_export import head_command, parse_head, patch_command
except ImportError:  # pragma: no cover - 与本文件同目录直接运行
    from patch_export import head_command, parse_head, patch_command

_WORKSPACE = "/testbed"


class _PatchExport:
    """包装run：记录运行前HEAD，结束后（含失败）导出补丁；导出失败不改变试次结果。"""

    workspace: str = _WORKSPACE

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        head = await environment.exec(command=head_command(self.workspace), timeout_sec=30)
        base = parse_head(head.stdout) if head.return_code == 0 else None
        try:
            await super().run(instruction, environment, context)  # type: ignore[misc]
        finally:
            if base is not None:
                await environment.exec(
                    command=patch_command(self.workspace, base, EnvironmentPaths.agent_dir),
                    timeout_sec=120,
                )


class OracleWithPatch(_PatchExport, OracleAgent):
    pass


class NopWithPatch(_PatchExport, NopAgent):
    pass


class MiniSweAgentOffline(_PatchExport, MiniSweAgent):
    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(environment, ("curl", "bash", "build_tools", "git"))
        version = f"=={self._version}" if self._version else ""
        freeze = shlex.quote(str(EnvironmentPaths.agent_dir / "mini-swe-agent.freeze.txt"))
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                'export PATH="$HOME/.local/bin:$PATH"; '
                "command -v uv >/dev/null || { echo 'image must provide uv' >&2; exit 1; }; "
                'grep -q \'export PATH="$HOME/.local/bin:$PATH"\' "$HOME/.bashrc" 2>/dev/null || '
                'echo \'export PATH="$HOME/.local/bin:$PATH"\' >> "$HOME/.bashrc"; '
                "uv python install 3.12 && "
                f"uv tool install --python 3.12 mini-swe-agent{version} "
                "--with litellm --with orjson --with fastapi && "
                "mini-swe-agent --help >/dev/null && "
                f'uv pip freeze --python "$(uv tool dir)/mini-swe-agent/bin/python" > {freeze}'
            ),
        )
