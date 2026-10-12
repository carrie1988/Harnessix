"""从 Docker 本地公开镜像清单冻结每个 SWE-bench Case 的测试 Profile。"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ContractModel
from harnessix.evals.swebench.contracts import (
    SWE_BENCH_CASE_COUNT,
    SweBenchPreRegistrationPlan,
)
from harnessix.product_config.action_contracts import (
    ProductProcessProfile,
    build_product_process_profile,
)

ImageListRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]

PUBLIC_TEST_PROFILE_ID = "public-tests"
_PROFILE_VERSION = "swebench-public-tests-v5"
_PROFILE_DESCRIPTION = "Run focused public repository tests for this SWE-bench case"
_IMAGE_REPOSITORY_PREFIX = "swebench/sweb.eval."
_IMAGE_REPOSITORY_PLATFORM = "x86_64"
_IMAGE_BUILD_NAMESPACE = "1776"
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_LIST_TIMEOUT_SECONDS = 30.0
_MAX_STDOUT_BYTES = 4 * 1024 * 1024
_MAX_STDERR_BYTES = 16 * 1024
_PROGRAM = "/usr/bin/env"
_COMMON_ENVIRONMENT_ARGUMENTS = (
    "HOME=/tmp",
    "TMPDIR=/tmp",
    "XDG_CACHE_HOME=/tmp",
    "MPLCONFIGDIR=/tmp",
    "HYPOTHESIS_STORAGE_DIRECTORY=/tmp",
    "PYTHONPATH=/workspace",
    "PYTHONDONTWRITEBYTECODE=1",
)

# 测试源码来自只读 /workspace，编译扩展与构建期生成模块只允许从同一
# SWE-bench 官方镜像的 /testbed 回退加载。测试进程在 /tmp 运行，避免
# pytest、图片比较等工具把临时结果写回待交付 Workspace。
_PYTEST_BOOTSTRAP_SOURCE = """\
import importlib.abc
import importlib.machinery
import os
from pathlib import Path
import sys

workspace = Path(sys.argv[1])
image_workspace = Path(sys.argv[2])
arguments = sys.argv[3:]
separator = arguments.index("--")
fixed_arguments = arguments[:separator]
selectors = arguments[separator + 1:]
source_pairs = tuple(
    (workspace / name, image_workspace / name)
    for name in ("src", "lib", "")
    if (workspace / name).is_dir() and (image_workspace / name).is_dir()
)


class ImageBuildFallback(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        parts = fullname.split(".")
        if len(parts) < 2:
            return None
        for workspace_root, image_root in source_pairs:
            workspace_package = workspace_root / parts[0]
            image_package = image_root / parts[0]
            if not (workspace_package / "__init__.py").is_file() or not image_package.is_dir():
                continue
            relative = Path(*parts[1:])
            workspace_module = workspace_package / relative
            if (
                workspace_module.with_suffix(".py").is_file()
                or (workspace_module / "__init__.py").is_file()
                or tuple(workspace_module.parent.glob(workspace_module.name + ".*.so"))
            ):
                return None
            image_parent = image_package / relative.parent
            spec = importlib.machinery.PathFinder.find_spec(fullname, [str(image_parent)])
            if spec is not None:
                return spec
        return None


def changed_test_files():
    def source_changed(candidate):
        relative = candidate.relative_to(workspace)
        if candidate.is_symlink() or any(
            part.startswith(".") or part in {"build", "dist"} for part in relative.parts
        ):
            return False
        original = image_workspace / relative
        return (
            not original.is_file()
            or original.is_symlink()
            or candidate.read_bytes() != original.read_bytes()
        )

    stems = {
        candidate.stem
        for candidate in workspace.rglob("*.py")
        if not candidate.name.startswith("test_") and source_changed(candidate)
    }
    found = {
        candidate
        for stem in stems
        for candidate in workspace.rglob("test_" + stem + ".py")
        if candidate.is_file()
        and not candidate.is_symlink()
        and not any(
            part.startswith(".") or part in {"build", "dist"}
            for part in candidate.relative_to(workspace).parts
        )
    }
    return [str(candidate) for candidate in sorted(found)[:32]]


def existing_test_files(values):
    found = []
    for value in values:
        path = workspace / value.partition("::")[0]
        if path.exists():
            found.append(str(path))
    return found


sys.meta_path.insert(0, ImageBuildFallback())
sys.path[:0] = [str(workspace_root) for workspace_root, _ in source_pairs]
os.chdir("/tmp")
if not selectors:
    selectors = changed_test_files()
else:
    selectors = existing_test_files(selectors) or changed_test_files()
if not selectors:
    raise SystemExit("Harnessix found no existing public test target for the changed Python files")
import pytest
raise SystemExit(pytest.main([*fixed_arguments, *selectors]))
"""
_PYTEST_BOOTSTRAP_ARGUMENT = f"exec({_PYTEST_BOOTSTRAP_SOURCE!r})"
_PYTEST_ARGUMENTS = (
    *_COMMON_ENVIRONMENT_ARGUMENTS,
    "/opt/miniconda3/envs/testbed/bin/python",
    "-B",
    "-c",
    _PYTEST_BOOTSTRAP_ARGUMENT,
    "/workspace",
    "/testbed",
    "-q",
    "-p",
    "no:cacheprovider",
    "--disable-warnings",
    "--tb=short",
    "--maxfail=1",
    "--",
)
_DJANGO_TEST_ARGUMENTS = (
    *_COMMON_ENVIRONMENT_ARGUMENTS,
    "/opt/miniconda3/envs/testbed/bin/python",
    "-B",
    "tests/runtests.py",
)
_SYMPY_TEST_ARGUMENTS = (
    *_COMMON_ENVIRONMENT_ARGUMENTS,
    "/opt/miniconda3/envs/testbed/bin/python",
    "-B",
    "bin/test",
)
_TIMEOUT_SECONDS = 300
_MAX_OUTPUT_BYTES = 1024 * 1024
_CPU_LIMIT = 2.0
_MEMORY_BYTES = 3 * 1024 * 1024 * 1024
_PROCESS_LIMIT = 256


class SweBenchCaseProcessProfile(ContractModel):
    """把一个公开 Case 身份绑定到其完整、不可变的 Product Process Profile。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    spec_version: Literal["harnessix.swebench-case-process-profile/v1"] = (
        "harnessix.swebench-case-process-profile/v1"
    )
    instance_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+$",
    )
    repo: str = Field(
        min_length=3,
        max_length=256,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )
    profile: ProductProcessProfile

    @model_validator(mode="after")
    def fixed_public_test_profile(self) -> Self:
        profile = self.profile
        if (
            profile.profile_id != PUBLIC_TEST_PROFILE_ID
            or profile.version != _PROFILE_VERSION
            or profile.description != _PROFILE_DESCRIPTION
            or profile.program != _PROGRAM
            or profile.arguments != _profile_arguments(self.repo)
            or profile.selector_policy != "bounded_test_selector"
            or profile.timeout_seconds != _TIMEOUT_SECONDS
            or profile.max_output_bytes != _MAX_OUTPUT_BYTES
            or profile.network_mode != "none"
            or profile.cpu_limit != _CPU_LIMIT
            or profile.memory_bytes != _MEMORY_BYTES
            or profile.process_limit != _PROCESS_LIMIT
            or profile.secret_refs
            or _IMAGE_ID.fullmatch(profile.image) is None
        ):
            raise ValueError("SWE-bench公开测试Profile不符合固定执行边界")
        return self


def _profile_arguments(repo: str) -> tuple[str, ...]:
    """按公开仓库家族选择固定测试入口，不按单个 Case 定制。"""

    native_runners = {
        "django/django": _DJANGO_TEST_ARGUMENTS,
        "sympy/sympy": _SYMPY_TEST_ARGUMENTS,
    }
    return native_runners.get(repo, _PYTEST_ARGUMENTS)


def _run_image_list(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
        env={"PATH": os.defpath},
    )


def _docker_engine(container_engine: Path) -> Path:
    try:
        engine = container_engine.resolve(strict=True)
        info = engine.stat()
    except (OSError, RuntimeError):
        raise KernelError(
            "swebench_container_engine_invalid",
            "SWE-bench Docker CLI不可用",
        ) from None
    name = engine.name.casefold()
    if name.endswith(".exe"):
        name = name[:-4]
    if name != "docker" or not stat.S_ISREG(info.st_mode) or not os.access(engine, os.X_OK):
        raise KernelError("swebench_container_engine_invalid", "SWE-bench只接受可执行Docker CLI")
    return engine


def _local_image_rows(
    container_engine: Path,
    *,
    runner: ImageListRunner,
) -> tuple[tuple[str, str, str], ...]:
    try:
        completed = runner(
            (
                str(container_engine),
                "image",
                "ls",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ),
            _LIST_TIMEOUT_SECONDS,
        )
        if (
            type(completed.stdout) is not str
            or type(completed.stderr) is not str
            or completed.returncode != 0
            or len(completed.stdout.encode("utf-8")) > _MAX_STDOUT_BYTES
            or len(completed.stderr.encode("utf-8")) > _MAX_STDERR_BYTES
        ):
            raise ValueError
        rows: list[tuple[str, str, str]] = []
        for line in completed.stdout.splitlines():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError
            repository = value.get("Repository")
            tag = value.get("Tag")
            image_id = value.get("ID")
            if (
                not isinstance(repository, str)
                or not isinstance(tag, str)
                or not isinstance(image_id, str)
            ):
                raise ValueError
            rows.append((repository, tag, image_id))
        return tuple(rows)
    except (OSError, UnicodeError, ValueError, TypeError, subprocess.SubprocessError):
        raise KernelError(
            "swebench_public_test_images_unavailable",
            "SWE-bench本地公开测试镜像清单不可用",
        ) from None


def _expected_image_repository(instance_id: str) -> str:
    """按 SWE-bench 公共镜像命名规则派生唯一仓库名。"""

    owner, separator, repository_case = instance_id.partition("__")
    if not separator or not owner or not repository_case:
        raise KernelError(
            "swebench_public_test_profile_invalid",
            "SWE-bench Case身份无法派生公开测试镜像",
        )
    return (
        f"{_IMAGE_REPOSITORY_PREFIX}{_IMAGE_REPOSITORY_PLATFORM}."
        f"{owner}_{_IMAGE_BUILD_NAMESPACE}_{repository_case}"
    )


def build_swebench_case_process_profile(
    instance_id: str,
    *,
    repo: str,
    container_engine: Path,
    image_id: str,
) -> SweBenchCaseProcessProfile:
    """从已唯一发现的本地 image ID 构造固定公开测试 Profile。"""

    engine = _docker_engine(container_engine)
    if _IMAGE_ID.fullmatch(image_id) is None:
        raise KernelError(
            "swebench_public_test_image_invalid",
            "SWE-bench公开测试镜像没有完整本地SHA-256 ID",
        )
    profile = build_product_process_profile(
        profile_id=PUBLIC_TEST_PROFILE_ID,
        version=_PROFILE_VERSION,
        description=_PROFILE_DESCRIPTION,
        container_engine=str(engine),
        image=image_id,
        program=_PROGRAM,
        arguments=_profile_arguments(repo),
        selector_policy="bounded_test_selector",
        timeout_seconds=_TIMEOUT_SECONDS,
        max_output_bytes=_MAX_OUTPUT_BYTES,
        network_mode="none",
        cpu_limit=_CPU_LIMIT,
        memory_bytes=_MEMORY_BYTES,
        process_limit=_PROCESS_LIMIT,
    )
    return SweBenchCaseProcessProfile(instance_id=instance_id, repo=repo, profile=profile)


def discover_swebench_case_process_profiles(
    plan: SweBenchPreRegistrationPlan,
    container_engine: Path,
    *,
    runner: ImageListRunner = _run_image_list,
) -> tuple[SweBenchCaseProcessProfile, ...]:
    """只读一次 Docker 镜像清单，并为计划内每个 Case 严格唯一冻结 Profile。"""

    engine = _docker_engine(container_engine)
    rows = _local_image_rows(engine, runner=runner)
    bindings: list[SweBenchCaseProcessProfile] = []
    for case in sorted(plan.cases, key=lambda item: item.instance_id):
        expected_repository = _expected_image_repository(case.instance_id)
        matches = tuple(
            image_id
            for repository, tag, image_id in rows
            if tag == "latest" and repository == expected_repository
        )
        if not matches:
            raise KernelError(
                "swebench_public_test_image_missing",
                f"SWE-bench Case缺少本地公开测试镜像：{case.instance_id}",
            )
        if len(matches) != 1:
            raise KernelError(
                "swebench_public_test_image_ambiguous",
                f"SWE-bench Case匹配到多个本地公开测试镜像：{case.instance_id}",
            )
        bindings.append(
            build_swebench_case_process_profile(
                case.instance_id,
                repo=case.repo,
                container_engine=engine,
                image_id=matches[0],
            )
        )
    if len(bindings) != SWE_BENCH_CASE_COUNT:
        raise KernelError("swebench_public_test_profile_invalid", "SWE-bench公开测试Profile不完整")
    return tuple(bindings)
