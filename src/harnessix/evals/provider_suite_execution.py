"""复用正式Suite Runner执行受控真实Provider Task Pack基线。"""

from __future__ import annotations

import os
import stat
import subprocess
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.evals.provider_suite_contracts import CodingEvalProviderSuiteRunConfig
from harnessix.evals.suite_execution import Fault, run_coding_eval_suite
from harnessix.evals.suite_execution_contracts import CodingEvalSuiteRunReport
from harnessix.evals.task_pack import builtin_coding_eval_task_pack
from harnessix.evals.task_pack_contracts import CodingEvalTaskPackCase
from harnessix.evals.task_pack_execution import TaskPackCaseExecutor
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ModelProvider
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.observability import Observability

_GIT_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}


@dataclass(frozen=True, slots=True)
class TaskPackOpenAIChatProviderFactory:
    """每个Trial创建独立HTTP Client，不跨Session共享Provider状态。"""

    config: OpenAIChatConfig

    def __call__(
        self,
        case: CodingEvalTaskPackCase,
        run_id: UUID,
    ) -> AbstractAsyncContextManager[ModelProvider]:
        del case, run_id
        return OpenAIChatProvider(self.config)


def _require_executable(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
            raise OSError
        return resolved
    except OSError:
        raise KernelError(
            "eval_provider_suite_host_binding_invalid",
            f"真实Provider Suite{label}绑定无效",
        ) from None


def _require_source_revision(config: CodingEvalProviderSuiteRunConfig, git: Path) -> None:
    try:
        completed = subprocess.run(
            (
                str(git),
                "--no-pager",
                "--no-optional-locks",
                "-c",
                "core.hooksPath=/dev/null",
                "rev-parse",
                "HEAD",
            ),
            cwd=config.source_root,
            env=_GIT_ENVIRONMENT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
        revision = completed.stdout.decode("ascii", errors="strict").strip()
        if (
            completed.returncode != 0
            or revision != config.suite.plan.environment.harnessix_revision
        ):
            raise ValueError
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        raise KernelError(
            "eval_provider_suite_source_revision_mismatch",
            "真实Provider Suite执行代码Revision与计划不一致",
        ) from None


def _require_scope(
    config: CodingEvalProviderSuiteRunConfig,
    observability: Observability | None,
) -> TaskPackCaseExecutor:
    loaded = builtin_coding_eval_task_pack(config.pack_id, config.pack_version)
    if loaded.manifest.pack_sha256 != config.pack_sha256:
        raise KernelError("eval_provider_suite_pack_mismatch", "真实Provider Suite Task Pack漂移")
    git = _require_executable(Path(config.git_executable), "Git程序")
    container = _require_executable(Path(config.container_engine), "Container Engine")
    _require_source_revision(config, git)
    provider_factory = TaskPackOpenAIChatProviderFactory(config.provider_config)
    executor = TaskPackCaseExecutor(
        loaded,
        git,
        container,
        provider_factory,
        provider_binding_sha256=config.fingerprint,
        observability=observability,
    )
    return executor


async def run_task_pack_provider_suite(
    config: CodingEvalProviderSuiteRunConfig,
    *,
    allow_network: bool = False,
    resume: bool = False,
    cancel: CancelToken | None = None,
    observability: Observability | None = None,
    fault: Fault | None = None,
) -> CodingEvalSuiteRunReport:
    """显式启网后执行固定Suite；配置摘要同时绑定Suite和Case恢复状态。"""

    if allow_network is not True:
        raise KernelError("eval_provider_suite_network_disabled", "真实Provider Suite默认禁止网络")
    try:
        checked = CodingEvalProviderSuiteRunConfig.model_validate_json(
            config.model_dump_json(), strict=True
        )
    except ValueError:
        raise KernelError(
            "eval_provider_suite_config_invalid", "真实Provider Suite执行配置无效"
        ) from None
    executor = _require_scope(checked, observability)
    return await run_coding_eval_suite(
        checked.suite,
        executor,
        cancel=cancel,
        resume=resume,
        fault=fault,
        execution_binding_sha256=checked.fingerprint,
    )
