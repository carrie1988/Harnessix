"""复用正式Suite Runner执行受控真实Provider Task Pack基线。"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from contextlib import AbstractAsyncContextManager, ExitStack
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
from harnessix.evals.task_pack_publication import (
    PROVIDER_SECRET,
    provider_publication_scope,
    require_provider_publication_scope,
)
from harnessix.evals.task_pack_trial import TaskPackProviderFactory
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ModelProvider
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.observability import Observability
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.tools.workspace import digest

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
    publication_scope: SecretPublicationScope | None = None

    def __call__(
        self,
        case: CodingEvalTaskPackCase,
        run_id: UUID,
    ) -> AbstractAsyncContextManager[ModelProvider]:
        del case, run_id
        require_provider_publication_scope(self.publication_scope)
        assert self.publication_scope is not None
        material = self.publication_scope.resolve(PROVIDER_SECRET.name)
        try:
            if material.version != PROVIDER_SECRET.version:
                raise KernelError("publication_scope_unavailable", "真实评测凭据引用版本不匹配")
            return OpenAIChatProvider(self.config, api_key=bytes(material.value).decode("utf-8"))
        finally:
            material.clear()


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
    *,
    provider_factory: TaskPackProviderFactory | None = None,
    provider_binding_sha256: str | None = None,
    publication_scope: SecretPublicationScope | None = None,
) -> TaskPackCaseExecutor:
    loaded = builtin_coding_eval_task_pack(config.pack_id, config.pack_version)
    if loaded.manifest.pack_sha256 != config.pack_sha256:
        raise KernelError("eval_provider_suite_pack_mismatch", "真实Provider Suite Task Pack漂移")
    git = _require_executable(Path(config.git_executable), "Git程序")
    container = _require_executable(Path(config.container_engine), "Container Engine")
    _require_source_revision(config, git)
    selected_factory = (
        TaskPackOpenAIChatProviderFactory(config.provider_config, publication_scope)
        if provider_factory is None
        else provider_factory
    )
    executor = TaskPackCaseExecutor(
        loaded,
        git,
        container,
        selected_factory,
        provider_binding_sha256=provider_binding_sha256 or config.fingerprint,
        observability=observability,
        publication_scope=publication_scope,
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
    provider_factory: TaskPackProviderFactory | None = None,
    provider_binding_sha256: str | None = None,
    publication_scope: SecretPublicationScope | None = None,
) -> CodingEvalSuiteRunReport:
    """执行固定Suite；受托Factory必须显式绑定身份，不能跨恢复偷换请求控制。"""

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
    if provider_factory is None and provider_binding_sha256 is None:
        # 默认路径保留原指纹，既有无注入运行与恢复合同不变。
        binding = checked.fingerprint
        executor = _require_scope(checked, observability)
        if publication_scope is not None:
            raise KernelError(
                "eval_provider_suite_binding_invalid", "外部凭据 Scope 必须与受托 Factory 一起提供"
            )
    else:
        if (
            not callable(provider_factory)
            or not isinstance(provider_binding_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", provider_binding_sha256) is None
        ):
            raise KernelError(
                "eval_provider_suite_binding_invalid", "受托Provider Factory缺少有效执行身份"
            )
        binding = digest(
            {
                "provider_suite_config": checked.fingerprint,
                "provider_binding": provider_binding_sha256,
            }
        )
        require_provider_publication_scope(publication_scope)
        executor = _require_scope(
            checked,
            observability,
            provider_factory=provider_factory,
            provider_binding_sha256=binding,
            publication_scope=publication_scope,
        )
    with ExitStack() as scopes:
        if provider_factory is None:
            # 在正式宿主/配置预检之后冻结指定来源；Provider 不再重新读取可漂移环境。
            env_name = checked.provider_config.api_key_env
            publication_scope = scopes.enter_context(
                provider_publication_scope(env_name, os.environ.get(env_name, ""))
            )
            executor.publication_scope = publication_scope
            executor.provider_factory = TaskPackOpenAIChatProviderFactory(
                checked.provider_config, publication_scope
            )
        return await run_coding_eval_suite(
            checked.suite,
            executor,
            cancel=cancel,
            resume=resume,
            fault=fault,
            execution_binding_sha256=binding,
        )
