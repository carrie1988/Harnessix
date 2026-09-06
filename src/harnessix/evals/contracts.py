"""Coding Eval v1 的严格、可持久化契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from harnessix.agent.models import Budget
from harnessix.domain.models import ContractModel
from harnessix.tools.workspace import digest

CODING_EVAL_SPEC_VERSION: Literal["harnessix.coding-eval/v1"] = "harnessix.coding-eval/v1"
CODING_EVAL_GRADER_VERSION: Literal["coding-eval-grader/v1"] = "coding-eval-grader/v1"
CODING_EVAL_MATERIALIZER_VERSION: Literal["coding-eval-materializer/v1"] = (
    "coding-eval-materializer/v1"
)

Revision = str
EvalOutcome = Literal["passed", "failed", "invalid"]
EvalRunStatus = Literal["ready", "running", "completed"]
EvalFailureCategory = Literal[
    "eval_infrastructure",
    "runtime",
    "correctness",
    "regression",
    "forbidden_edit",
    "final_answer",
    "budget",
]
EvalCheckCode = Literal[
    "task_repository_matched",
    "baseline_checks_failed",
    "final_check_set_matched",
    "turn_completed",
    "behavior_checks_passed",
    "regression_checks_passed",
    "head_unchanged",
    "allowed_changes",
    "change_count",
    "clean_index",
    "test_feedback_order",
    "git_feedback_order",
    "final_answer_consistent",
    "budget_respected",
]
EVAL_CHECK_CODES: tuple[EvalCheckCode, ...] = (
    "task_repository_matched",
    "baseline_checks_failed",
    "final_check_set_matched",
    "turn_completed",
    "behavior_checks_passed",
    "regression_checks_passed",
    "head_unchanged",
    "allowed_changes",
    "change_count",
    "clean_index",
    "test_feedback_order",
    "git_feedback_order",
    "final_answer_consistent",
    "budget_respected",
)
EVAL_CHECK_CATEGORIES: dict[EvalCheckCode, EvalFailureCategory] = {
    "task_repository_matched": "eval_infrastructure",
    "baseline_checks_failed": "eval_infrastructure",
    "final_check_set_matched": "eval_infrastructure",
    "turn_completed": "runtime",
    "behavior_checks_passed": "correctness",
    "regression_checks_passed": "regression",
    "head_unchanged": "forbidden_edit",
    "allowed_changes": "forbidden_edit",
    "change_count": "forbidden_edit",
    "clean_index": "forbidden_edit",
    "test_feedback_order": "correctness",
    "git_feedback_order": "correctness",
    "final_answer_consistent": "final_answer",
    "budget_respected": "budget",
}


class EvalContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


def _relative_path(value: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise ValueError("Eval 路径必须是合法 UTF-8") from None
    parts = value.split("/")
    if (
        not encoded
        or len(encoded) > 1024
        or value.startswith("/")
        or "\\" in value
        or len(parts) > 64
        or any(
            not part
            or part in {".", ".."}
            or len(part.encode("utf-8")) > 255
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
            for part in parts
        )
    ):
        raise ValueError("Eval 路径必须是受限 POSIX 相对路径")
    return value


def _sorted_unique(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    if not values or list(values) != sorted(set(values)):
        raise ValueError(f"{name}必须非空、唯一并按字节序排序")
    return values


class EvalRepository(EvalContract):
    """任务来源身份；不包含访问凭据或可变分支名。"""

    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    origin: str = Field(min_length=1, max_length=2048)
    source_revision: Revision = Field(pattern=r"^[0-9a-f]{40,64}$")
    baseline_tree_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("origin")
    @classmethod
    def origin_has_no_http_credentials(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("仓库来源包含控制字符")
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and (
            parsed.username is not None or parsed.password is not None
        ):
            raise ValueError("仓库来源不能内嵌 HTTP 凭据")
        return value


class CodingEvalTask(EvalContract):
    spec_version: Literal["harnessix.coding-eval/v1"] = CODING_EVAL_SPEC_VERSION
    task_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    task_version: int = Field(ge=1)
    repository: EvalRepository
    prompt: str = Field(min_length=1, max_length=16_384)
    allowed_changed_paths: tuple[str, ...] = Field(min_length=1, max_length=64)
    required_test_profiles: tuple[str, ...] = Field(min_length=1, max_length=32)
    baseline_checks: tuple[str, ...] = Field(min_length=1, max_length=32)
    behavior_checks: tuple[str, ...] = Field(min_length=1, max_length=32)
    regression_checks: tuple[str, ...] = Field(default=(), max_length=32)
    max_changed_files: int = Field(default=8, ge=1, le=64)
    budget: Budget = Field(default_factory=Budget)
    grader_version: Literal["coding-eval-grader/v1"] = CODING_EVAL_GRADER_VERSION

    @field_validator("budget", mode="before")
    @classmethod
    def strict_budget(cls, value: object) -> Budget:
        if isinstance(value, Budget):
            return value
        if not isinstance(value, dict):
            raise ValueError("Eval 预算必须是严格对象")
        integer_fields = {"max_steps", "max_tokens", "max_output_chars", "max_tool_calls_per_step"}
        if any(name in value and (type(value[name]) is not int) for name in integer_fields) or (
            "timeout_seconds" in value
            and (
                type(value["timeout_seconds"]) not in {int, float}
                or type(value["timeout_seconds"]) is bool
            )
        ):
            raise ValueError("Eval 预算字段不能进行类型强转")
        return Budget.model_validate(value, strict=True)

    @field_validator("allowed_changed_paths")
    @classmethod
    def valid_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        checked = tuple(_relative_path(path) for path in value)
        return _sorted_unique(checked, "允许修改路径")

    @field_validator(
        "required_test_profiles",
        "baseline_checks",
        "behavior_checks",
    )
    @classmethod
    def unique_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not name or len(name) > 128 for name in value):
            raise ValueError("Eval 名称为空或超过长度上限")
        return _sorted_unique(value, "Eval 名称")

    @field_validator("regression_checks")
    @classmethod
    def unique_optional_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not name or len(name) > 128 for name in value) or list(value) != sorted(set(value)):
            raise ValueError("回归检查名称必须唯一并按字节序排序")
        return value

    @model_validator(mode="after")
    def changed_file_limit(self) -> Self:
        if self.max_changed_files > len(self.allowed_changed_paths):
            raise ValueError("最大修改文件数不能超过允许路径数量")
        groups = (set(self.baseline_checks), set(self.behavior_checks), set(self.regression_checks))
        if groups[0] != groups[1] or groups[0] & groups[2]:
            raise ValueError("基线缺陷检查必须等于行为检查，且不能与回归检查重叠")
        return self

    @property
    def fingerprint(self) -> Revision:
        return digest(self.model_dump(mode="json"))


class EvalTestObservation(EvalContract):
    check_id: str = Field(min_length=1, max_length=128)
    phase: Literal["baseline", "final"]
    passed: bool
    returncode: int = Field(ge=-128, le=255)
    output_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")
    elapsed_seconds: float = Field(ge=0, le=86_400)

    @model_validator(mode="after")
    def result_matches_returncode(self) -> Self:
        if self.passed != (self.returncode == 0):
            raise ValueError("检查通过结论必须与退出码零一致")
        return self


class EvalGitEvidence(EvalContract):
    baseline_revision: Revision = Field(pattern=r"^[0-9a-f]{40,64}$")
    baseline_tree_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")
    head_revision: Revision = Field(pattern=r"^[0-9a-f]{40,64}$")
    changed_paths: tuple[str, ...] = Field(max_length=200)
    staged_paths: tuple[str, ...] = Field(max_length=200)
    untracked_paths: tuple[str, ...] = Field(max_length=200)
    unsupported_change_paths: tuple[str, ...] = Field(max_length=200)
    status_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")
    diff_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")
    diff_observed_bytes: int = Field(ge=0)

    @field_validator(
        "changed_paths",
        "staged_paths",
        "untracked_paths",
        "unsupported_change_paths",
    )
    @classmethod
    def valid_evidence_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        checked = tuple(_relative_path(path) for path in value)
        if list(checked) != sorted(set(checked)):
            raise ValueError("Git 证据路径必须唯一并排序")
        return checked

    @model_validator(mode="after")
    def evidence_subsets(self) -> Self:
        changed = set(self.changed_paths)
        if not (
            set(self.staged_paths) <= changed
            and set(self.untracked_paths) <= changed
            and set(self.unsupported_change_paths) <= changed
        ):
            raise ValueError("Git 分类路径必须属于完整变更路径")
        return self


class EvalFinalTest(EvalContract):
    profile: str = Field(min_length=1, max_length=128)
    passed: bool


class EvalFinalAnswer(EvalContract):
    summary: str = Field(min_length=1, max_length=4096)
    changed_paths: tuple[str, ...] = Field(max_length=64)
    tests: tuple[EvalFinalTest, ...] = Field(max_length=32)

    @field_validator("changed_paths")
    @classmethod
    def valid_changed_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        checked = tuple(_relative_path(path) for path in value)
        if list(checked) != sorted(set(checked)):
            raise ValueError("最终回答的修改路径必须唯一并排序")
        return checked

    @model_validator(mode="after")
    def unique_sorted_tests(self) -> Self:
        names = [test.profile for test in self.tests]
        if names != sorted(set(names)):
            raise ValueError("最终回答的测试必须按名称唯一排序")
        return self


class EvalFinalAnswerEvidence(EvalContract):
    """最终回答的脱敏证据；不把模型语义摘要写入报告。"""

    parsed: bool
    response_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")
    response_utf8_bytes: int = Field(ge=0, le=1_000_000)
    changed_paths: tuple[str, ...] = Field(max_length=64)
    tests: tuple[EvalFinalTest, ...] = Field(max_length=32)

    @field_validator("changed_paths")
    @classmethod
    def valid_changed_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        checked = tuple(_relative_path(path) for path in value)
        if list(checked) != sorted(set(checked)):
            raise ValueError("回答证据的修改路径必须唯一并排序")
        return checked

    @model_validator(mode="after")
    def parsed_fields(self) -> Self:
        if not self.parsed and (self.changed_paths or self.tests):
            raise ValueError("未解析回答不能携带已解释声明")
        names = [test.profile for test in self.tests]
        if names != sorted(set(names)):
            raise ValueError("回答证据的测试必须按名称唯一排序")
        return self


class CodingEvalEnvironment(EvalContract):
    harnessix_revision: Revision = Field(pattern=r"^[0-9a-f]{40,64}$")
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    platform: str = Field(min_length=1, max_length=256)
    isolation: str = Field(min_length=1, max_length=256)


class CodingEvalMaterialization(EvalContract):
    """发布在运行目录外层的历史任务工作区身份，不包含宿主绝对路径。"""

    spec_version: Literal["harnessix.coding-eval-materialization/v1"] = (
        "harnessix.coding-eval-materialization/v1"
    )
    materializer_version: Literal["coding-eval-materializer/v1"] = CODING_EVAL_MATERIALIZER_VERSION
    run_id: UUID
    task_id: str = Field(min_length=1, max_length=128)
    task_version: int = Field(ge=1)
    task_fingerprint: Revision = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: Revision = Field(pattern=r"^[0-9a-f]{40,64}$")
    source_tree_oid: Revision = Field(pattern=r"^[0-9a-f]{40,64}$")
    source_archive_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_revision: Revision = Field(pattern=r"^[0-9a-f]{40,64}$")
    baseline_tree_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")
    tracked_files: int = Field(ge=1, le=100_000)
    archive_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    git_version: str = Field(min_length=1, max_length=128)
    workspace_directory: Literal["workspace"] = "workspace"
    status: Literal["ready"] = "ready"
    created_at: AwareDatetime


class CodingEvalRunState(EvalContract):
    """历史任务编排的恢复锚点；路径由固定运行目录决定，不进入契约。"""

    spec_version: Literal["harnessix.coding-eval-run-state/v1"] = (
        "harnessix.coding-eval-run-state/v1"
    )
    run_id: UUID
    task_id: str = Field(min_length=1, max_length=128)
    task_version: int = Field(ge=1)
    task_fingerprint: Revision = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_revision: Revision = Field(pattern=r"^[0-9a-f]{40,64}$")
    baseline_tree_sha256: Revision = Field(pattern=r"^[0-9a-f]{64}$")
    execution_workspace_id: UUID
    status: EvalRunStatus
    thread_id: UUID | None = None
    turn_id: UUID | None = None
    baseline_observations: tuple[EvalTestObservation, ...] = Field(min_length=1, max_length=32)
    environment: CodingEvalEnvironment
    report_file: Literal["report.json"] = "report.json"
    report_sha256: Revision | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    started_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def lifecycle_is_consistent(self) -> Self:
        names = [item.check_id for item in self.baseline_observations]
        if (
            names != sorted(set(names))
            or any(item.phase != "baseline" for item in self.baseline_observations)
            or self.updated_at < self.started_at
        ):
            raise ValueError("Eval运行状态中的基线观察或时间无效")
        if self.status == "ready":
            if self.thread_id is not None or self.turn_id is not None or self.report_sha256:
                raise ValueError("ready运行不能提前绑定Session或报告")
        elif self.status == "running":
            if self.thread_id is None or self.report_sha256 is not None:
                raise ValueError("running运行必须绑定Thread且尚未发布报告")
        elif self.thread_id is None or self.turn_id is None or self.report_sha256 is None:
            raise ValueError("completed运行必须绑定Turn和报告摘要")
        if self.turn_id is not None and self.thread_id is None:
            raise ValueError("Turn必须归属于已绑定Thread")
        return self


class EvalCheck(EvalContract):
    code: EvalCheckCode
    passed: bool
    category: EvalFailureCategory
    message: str = Field(min_length=1, max_length=512)


class EvalMetrics(EvalContract):
    elapsed_seconds: float = Field(ge=0, le=86_400)
    model_steps: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    approvals: int = Field(ge=0)
    changed_files: int = Field(ge=0)


class CodingEvalReport(EvalContract):
    spec_version: Literal["harnessix.coding-eval-report/v1"] = "harnessix.coding-eval-report/v1"
    run_id: UUID
    task_id: str = Field(min_length=1, max_length=128)
    task_version: int = Field(ge=1)
    task_fingerprint: Revision = Field(pattern=r"^[0-9a-f]{64}$")
    grader_version: Literal["coding-eval-grader/v1"] = CODING_EVAL_GRADER_VERSION
    outcome: EvalOutcome
    failure_categories: tuple[EvalFailureCategory, ...]
    environment: CodingEvalEnvironment
    started_at: AwareDatetime
    completed_at: AwareDatetime
    checks: tuple[EvalCheck, ...]
    baseline_observations: tuple[EvalTestObservation, ...]
    final_observations: tuple[EvalTestObservation, ...]
    git: EvalGitEvidence
    final_answer: EvalFinalAnswerEvidence | None = None
    metrics: EvalMetrics

    @model_validator(mode="after")
    def consistent_report(self) -> Self:
        if tuple(check.code for check in self.checks) != EVAL_CHECK_CODES:
            raise ValueError("报告检查项缺失、重复或顺序错误")
        if any(check.category != EVAL_CHECK_CATEGORIES[check.code] for check in self.checks):
            raise ValueError("报告检查项分类与评分器版本不一致")
        failed = tuple(sorted({check.category for check in self.checks if not check.passed}))
        if self.failure_categories != failed:
            raise ValueError("报告失败分类与检查结果不一致")
        if (self.outcome == "passed") != (not failed):
            raise ValueError("报告结论与检查结果不一致")
        if (self.outcome == "invalid") != ("eval_infrastructure" in failed):
            raise ValueError("invalid 只表示评测基础设施或任务基线无效")
        if self.completed_at < self.started_at:
            raise ValueError("报告完成时间不能早于开始时间")
        baseline_names = [observation.check_id for observation in self.baseline_observations]
        final_names = [observation.check_id for observation in self.final_observations]
        if (
            baseline_names != sorted(set(baseline_names))
            or final_names != sorted(set(final_names))
            or any(observation.phase != "baseline" for observation in self.baseline_observations)
            or any(observation.phase != "final" for observation in self.final_observations)
        ):
            raise ValueError("报告检查观察必须按阶段唯一排序")
        if self.metrics.changed_files != len(self.git.changed_paths):
            raise ValueError("报告修改文件计数与Git证据不一致")
        answer_check = next(
            check for check in self.checks if check.code == "final_answer_consistent"
        )
        if answer_check.passed and (self.final_answer is None or not self.final_answer.parsed):
            raise ValueError("回答一致检查通过时必须存在已解析证据")
        return self


def elapsed_seconds(started_at: datetime, completed_at: datetime) -> float:
    return max(0.0, (completed_at - started_at).total_seconds())
