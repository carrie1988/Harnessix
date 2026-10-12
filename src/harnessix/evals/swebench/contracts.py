"""SWE-bench 公开 Case 与不可变预注册合同。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_validator, model_validator

from harnessix.domain.models import ContractModel

SWE_BENCH_CASE_COUNT = 25
SweBenchSelectionPolicy = Literal["repo_round_robin_sha256_v1"]
SWE_BENCH_SELECTION_POLICY: SweBenchSelectionPolicy = "repo_round_robin_sha256_v1"
ModelParameterValue = str | int | float | bool | None

_GIT_REVISION = r"^[0-9a-f]{40,64}$"
_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$"


class _SweBenchContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class SweBenchPublicCase(_SweBenchContract):
    """允许进入计划或 Agent 的 SWE-bench 字段白名单。"""

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
    base_commit: str = Field(pattern=_GIT_REVISION)
    problem_statement: str = Field(min_length=1, max_length=256 * 1024)
    version: str = Field(min_length=1, max_length=128)
    issue_id: int | None = Field(default=None, ge=1)
    issue_url: str | None = Field(default=None, min_length=1, max_length=2048)
    pr_url: str | None = Field(default=None, min_length=1, max_length=2048)
    created_at: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("problem_statement", "version", "created_at")
    @classmethod
    def no_nul(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("SWE-bench public text contains NUL")
        return value

    @field_validator("issue_url", "pr_url")
    @classmethod
    def public_https_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        try:
            _ = parsed.port
        except ValueError:
            raise ValueError("SWE-bench public URL is invalid") from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(character.isspace() for character in value)
        ):
            raise ValueError("SWE-bench public URL must be credential-free HTTPS")
        return value


class SweBenchModelParameter(_SweBenchContract):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
    value: ModelParameterValue


class SweBenchModelConfig(_SweBenchContract):
    """Provider 身份与不可变、规范排序的模型标量参数。"""

    provider: str = Field(pattern=_IDENTIFIER)
    parameters: tuple[SweBenchModelParameter, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def parameters_are_canonical(self) -> Self:
        names = [parameter.name for parameter in self.parameters]
        if names != sorted(set(names)):
            raise ValueError("SWE-bench model parameters must be unique and sorted")
        return self


class SweBenchTaskBudget(_SweBenchContract):
    max_steps: int = Field(ge=1, le=1000)
    max_tokens: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0, le=86_400)
    max_output_chars: int = Field(ge=1, le=1_000_000)
    max_tool_calls_per_step: int = Field(ge=1, le=128)


class SweBenchPreRegistrationPlan(_SweBenchContract):
    """任何 SWE-bench 任务执行前必须冻结的完整范围。"""

    spec_version: Literal["harnessix.swebench-preregistration/v1"] = (
        "harnessix.swebench-preregistration/v1"
    )
    dataset_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    dataset_revision: str = Field(pattern=_GIT_REVISION)
    dataset_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_count: int = Field(ge=SWE_BENCH_CASE_COUNT, le=100_000)
    candidate_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    split: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
    harnessix_revision: str = Field(pattern=_GIT_REVISION)
    swebench_revision: str = Field(pattern=_GIT_REVISION)
    swebench_package_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    task_repo_revision: str = Field(pattern=_GIT_REVISION)
    model_name: str = Field(pattern=_IDENTIFIER)
    model_settings: SweBenchModelConfig
    task_budget: SweBenchTaskBudget
    trials_per_case: Literal[1] = 1
    agent_network_mode: Literal["none"] = "none"
    model_retry_policy: Literal["none"] = "none"
    selection_policy: SweBenchSelectionPolicy
    salt: str = Field(min_length=16, max_length=256)
    cases: tuple[SweBenchPublicCase, ...] = Field(
        min_length=SWE_BENCH_CASE_COUNT,
        max_length=SWE_BENCH_CASE_COUNT,
    )
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("salt")
    @classmethod
    def valid_salt(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("SWE-bench selection salt contains control characters")
        return value

    @model_validator(mode="after")
    def scope_is_canonical_and_bound(self) -> Self:
        from harnessix.evals.swebench.selection import canonical_swebench_case_order

        instance_ids = [case.instance_id for case in self.cases]
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("SWE-bench cases must have unique instance_id values")
        if self.cases != canonical_swebench_case_order(
            self.cases,
            salt=self.salt,
            selection_policy=self.selection_policy,
        ):
            raise ValueError("SWE-bench cases are not in canonical selection order")
        if self.plan_sha256 != swebench_preregistration_plan_digest(self):
            raise ValueError("SWE-bench pre-registration plan digest does not match")
        return self


def swebench_preregistration_plan_digest(plan: SweBenchPreRegistrationPlan) -> str:
    payload = plan.model_dump(
        mode="json",
        exclude={"plan_sha256"},
        warnings="error",
    )
    body = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def swebench_candidate_manifest_digest(cases: Iterable[SweBenchPublicCase]) -> str:
    """只用公开字段计算候选总体摘要，防止按运行结果换题。"""

    payload = [
        case.model_dump(mode="json", warnings="error")
        for case in sorted(cases, key=lambda item: item.instance_id)
    ]
    body = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def build_swebench_preregistration_plan(
    rows: Iterable[Mapping[str, object] | SweBenchPublicCase],
    *,
    dataset_name: str,
    dataset_revision: str,
    dataset_artifact_sha256: str,
    split: str,
    harnessix_revision: str,
    swebench_revision: str,
    swebench_package_version: str,
    task_repo_revision: str,
    model_name: str,
    model_config: SweBenchModelConfig,
    task_budget: SweBenchTaskBudget,
    salt: str,
    selection_policy: SweBenchSelectionPolicy = SWE_BENCH_SELECTION_POLICY,
) -> SweBenchPreRegistrationPlan:
    """Select exactly 25 public cases and seal their complete pre-registration scope."""

    from harnessix.evals.swebench.selection import (
        project_swebench_public_cases,
        select_swebench_cases,
    )

    candidates = project_swebench_public_cases(rows)
    cases = select_swebench_cases(
        candidates,
        salt=salt,
        selection_policy=selection_policy,
    )
    candidate = SweBenchPreRegistrationPlan.model_construct(
        spec_version="harnessix.swebench-preregistration/v1",
        dataset_name=dataset_name,
        dataset_revision=dataset_revision,
        dataset_artifact_sha256=dataset_artifact_sha256,
        candidate_count=len(candidates),
        candidate_manifest_sha256=swebench_candidate_manifest_digest(candidates),
        split=split,
        harnessix_revision=harnessix_revision,
        swebench_revision=swebench_revision,
        swebench_package_version=swebench_package_version,
        task_repo_revision=task_repo_revision,
        model_name=model_name,
        model_settings=model_config,
        task_budget=task_budget,
        trials_per_case=1,
        agent_network_mode="none",
        model_retry_policy="none",
        selection_policy=selection_policy,
        salt=salt,
        cases=cases,
        plan_sha256="0" * 64,
    )
    return SweBenchPreRegistrationPlan.model_validate(
        {
            **candidate.model_dump(mode="python", exclude={"plan_sha256"}, warnings="error"),
            "plan_sha256": swebench_preregistration_plan_digest(candidate),
        },
        strict=True,
    )
