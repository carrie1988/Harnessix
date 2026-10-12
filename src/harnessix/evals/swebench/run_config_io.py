"""SWE-bench 私有 Agent 执行配置的单次发布与严格读取。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.evals.swebench.files import write_atomic
from harnessix.evals.swebench.run_config import SweBenchAgentRunConfig

_MAX_CONFIG_BYTES = 1024 * 1024


def write_swebench_agent_run_config(path: Path, config: SweBenchAgentRunConfig) -> None:
    if path.exists() or path.is_symlink():
        raise KernelError("swebench_run_config_exists", "SWE-bench执行配置已经发布")
    body = (config.model_dump_json(indent=2) + "\n").encode("utf-8")
    write_atomic(
        path,
        body,
        error_code="swebench_run_config_write_failed",
        label="SWE-bench执行配置",
    )


def read_swebench_agent_run_config(path: Path) -> SweBenchAgentRunConfig:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or not 1 <= info.st_size <= _MAX_CONFIG_BYTES
        ):
            raise OSError
        body = os.read(descriptor, _MAX_CONFIG_BYTES + 1)
        if len(body) != info.st_size:
            raise OSError
    except OSError:
        raise KernelError(
            "swebench_run_config_invalid",
            "SWE-bench执行配置缺失、损坏或权限错误",
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        return SweBenchAgentRunConfig.model_validate_json(body, strict=True)
    except ValueError:
        raise KernelError("swebench_run_config_invalid", "SWE-bench执行配置无效") from None
