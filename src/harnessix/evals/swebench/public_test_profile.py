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
_PROFILE_VERSION = "swebench-public-tests-v1"
_PROFILE_DESCRIPTION = "Run focused public repository tests for this SWE-bench case"
_IMAGE_REPOSITORY_PREFIX = "swebench/sweb.eval."
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_LIST_TIMEOUT_SECONDS = 30.0
_MAX_STDOUT_BYTES = 4 * 1024 * 1024
_MAX_STDERR_BYTES = 16 * 1024
_PROGRAM = "/usr/bin/env"
_ARGUMENTS = (
    "HOME=/tmp",
    "TMPDIR=/tmp",
    "XDG_CACHE_HOME=/tmp",
    "MPLCONFIGDIR=/tmp",
    "HYPOTHESIS_STORAGE_DIRECTORY=/tmp",
    "PYTHONDONTWRITEBYTECODE=1",
    "/opt/miniconda3/envs/testbed/bin/python",
    "-B",
    "-m",
    "pytest",
    "-q",
    "-p",
    "no:cacheprovider",
    "--disable-warnings",
    "--tb=short",
    "--maxfail=1",
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
    profile: ProductProcessProfile

    @model_validator(mode="after")
    def fixed_public_test_profile(self) -> Self:
        profile = self.profile
        if (
            profile.profile_id != PUBLIC_TEST_PROFILE_ID
            or profile.version != _PROFILE_VERSION
            or profile.description != _PROFILE_DESCRIPTION
            or profile.program != _PROGRAM
            or profile.arguments != _ARGUMENTS
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


def build_swebench_case_process_profile(
    instance_id: str,
    *,
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
        arguments=_ARGUMENTS,
        selector_policy="bounded_test_selector",
        timeout_seconds=_TIMEOUT_SECONDS,
        max_output_bytes=_MAX_OUTPUT_BYTES,
        network_mode="none",
        cpu_limit=_CPU_LIMIT,
        memory_bytes=_MEMORY_BYTES,
        process_limit=_PROCESS_LIMIT,
    )
    return SweBenchCaseProcessProfile(instance_id=instance_id, profile=profile)


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
        suffix = f"_{case.instance_id}"
        matches = tuple(
            image_id
            for repository, tag, image_id in rows
            if tag == "latest"
            and repository.startswith(_IMAGE_REPOSITORY_PREFIX)
            and len(repository) > len(_IMAGE_REPOSITORY_PREFIX) + len(suffix)
            and repository.endswith(suffix)
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
                container_engine=engine,
                image_id=matches[0],
            )
        )
    if len(bindings) != SWE_BENCH_CASE_COUNT:
        raise KernelError("swebench_public_test_profile_invalid", "SWE-bench公开测试Profile不完整")
    return tuple(bindings)
