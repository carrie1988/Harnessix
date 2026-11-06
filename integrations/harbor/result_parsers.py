"""Harbor内置智能体的结果解析器：只登记智能体阶段导出的补丁，状态与用量沿用通用规则。

Harnessix使用integrations.harnessix_eval.harbor_parser:HarnessixResultParser，它额外映射原生协议摘要。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evalkit.harbor import AgentResult


class _PatchArtifactParser:
    agent_name = ""
    unique_refs: tuple[str, ...] = ()

    def parse(self, trial_dir: Path, result: Mapping[str, Any]) -> AgentResult:
        del result
        patch = trial_dir / "agent" / "patch.diff"
        return AgentResult(artifact_files={"patch": patch} if patch.is_file() else {})


class OracleParser(_PatchArtifactParser):
    agent_name = "oracle"


class NopParser(_PatchArtifactParser):
    agent_name = "nop"


class MiniSweAgentParser(_PatchArtifactParser):
    agent_name = "mini-swe-agent"
