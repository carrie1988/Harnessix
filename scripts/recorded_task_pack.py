"""仅供测试/CI使用的Task Pack Recorded Provider与Golden解析。"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid5

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.ids import new_id
from harnessix.agent.models import Usage
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
from harnessix.evals.task_pack import LoadedCodingEvalTaskPack
from harnessix.evals.task_pack_contracts import CodingEvalTaskPackCase
from harnessix.evals.task_pack_materializer import materialize_task_pack_case
from harnessix.models.contracts import (
    ModelProvider,
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextStarted,
    ToolCallCompleted,
)

RECORDED_MODEL = "harnessix-recorded-v1"
_ORACLE_NAMESPACE = UUID("89bf0828-997d-5a08-9b85-3a8ddaf44b57")


@dataclass(frozen=True, slots=True)
class RecordedSolution:
    case: CodingEvalTaskPackCase
    before: bytes | None
    content: str
    mode: int


def prepare_recorded_solution(
    loaded: LoadedCodingEvalTaskPack,
    case: CodingEvalTaskPackCase,
    *,
    git_executable: Path,
    oracle_root: Path,
    solutions_root: Path,
) -> RecordedSolution:
    """在隔离Workspace应用Golden，并只返回正式Patch调用所需的目标文件事实。"""

    oracle_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    materialized = materialize_task_pack_case(
        loaded,
        oracle_root,
        git_executable,
        case.case_id,
        uuid5(_ORACLE_NAMESPACE, case.case_id),
    )
    target = materialized.workspace / case.task.allowed_changed_paths[0]
    before = target.read_bytes() if target.exists() else None
    completed = subprocess.run(
        (
            str(git_executable),
            "apply",
            "--unidiff-zero",
            "--whitespace=nowarn",
            str(solutions_root / f"{case.case_id}.patch"),
        ),
        cwd=materialized.workspace,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Recorded Golden无法应用：{case.case_id}")
    return RecordedSolution(
        case=case,
        before=before,
        content=target.read_text(encoding="utf-8"),
        mode=target.stat().st_mode & 0o777,
    )


class RecordedSolutionProvider:
    """按固定六步事件驱动正式Agent工具链，不直接生成Eval报告。"""

    def __init__(self, solution: RecordedSolution, run_id: UUID) -> None:
        self._solution = solution
        self._run_id = run_id
        self.requests: list[ModelRequest] = []

    def _patch(self) -> dict[str, object]:
        path = self._solution.case.task.allowed_changed_paths[0]
        file: dict[str, object] = {
            "operation": "create" if self._solution.before is None else "replace",
            "path": path,
            "content": self._solution.content,
            "mode": self._solution.mode,
        }
        if self._solution.before is not None:
            file["expected_sha256"] = hashlib.sha256(self._solution.before).hexdigest()
        return {"files": [file]}

    def _answer(self) -> str:
        case = self._solution.case
        findings = (
            ()
            if case.review_oracle is None
            else tuple(item.finding_id for item in case.review_oracle.required_findings)
        )
        summary = "Recorded Provider完成固定离线任务"
        if findings:
            summary = f"{summary} {' '.join(findings)}"
        return json.dumps(
            {
                "summary": summary,
                "changed_paths": list(case.task.allowed_changed_paths),
                "tests": [{"profile": case.profile_id, "passed": True}],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _step_event(self, response_id: str, step: int) -> tuple[ProviderEvent | None, str]:
        profile = self._solution.case.profile_id
        if step == 1:
            return (
                ToolCallCompleted(
                    call_id=f"{response_id}-profile-baseline",
                    tool=f"run_profile.{profile}",
                    arguments={"profile": profile, "selectors": []},
                ),
                "tool_calls",
            )
        if step == 2:
            return (
                ToolCallCompleted(
                    call_id=f"{response_id}-patch",
                    tool="apply_patch_batch",
                    arguments=self._patch(),
                ),
                "tool_calls",
            )
        if step == 3:
            return (
                ToolCallCompleted(
                    call_id=f"{response_id}-profile-final",
                    tool=f"run_profile.{profile}",
                    arguments={"profile": profile, "selectors": []},
                ),
                "tool_calls",
            )
        if step == 4:
            return (
                ToolCallCompleted(call_id=f"{response_id}-status", tool="git_status", arguments={}),
                "tool_calls",
            )
        if step == 5:
            return (
                ToolCallCompleted(
                    call_id=f"{response_id}-diff",
                    tool="git_diff",
                    arguments={"target": "worktree", "context_lines": 1},
                ),
                "tool_calls",
            )
        if step != 6:
            raise AssertionError(f"Recorded Provider收到意外步骤：{step}")
        return None, "completed"

    async def stream(
        self,
        request: ModelRequest,
        cancel: CancelToken,
    ) -> AsyncGenerator[ProviderEvent, None]:
        self.requests.append(request.model_copy(deep=True))
        cancel.checkpoint()
        attempt_id = new_id()
        response_id = f"recorded-{self._run_id}-{request.step}"
        usage = Usage(input_tokens=10, output_tokens=5)
        yield ModelAttemptStarted(
            attempt_id=attempt_id,
            step=request.step,
            index=1,
            provider="recorded",
            requested_model=RECORDED_MODEL,
        )
        yield ResponseStarted(response_id=response_id)
        event, finish = self._step_event(response_id, request.step)
        if event is None:
            yield TextStarted(content_id=f"{response_id}-answer")
            yield TextCompleted(content_id=f"{response_id}-answer", text=self._answer())
        else:
            yield event
        yield ModelUsageObserved(
            attempt_id=attempt_id,
            actual_model=RECORDED_MODEL,
            response_id=response_id,
            usage=UsageObservation(
                completeness="complete",
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            ),
        )
        yield ModelAttemptFinished(attempt_id=attempt_id, outcome="completed")
        yield ResponseCompleted(finish_reason=finish, usage=usage)


class RecordedProviderFactory:
    """为固定Case准备Golden结果，并拒绝同一Run重复打开Provider。"""

    def __init__(
        self,
        loaded: LoadedCodingEvalTaskPack,
        *,
        git_executable: Path,
        oracle_root: Path,
        solutions_root: Path,
    ) -> None:
        self._loaded = loaded
        self._git = git_executable
        self._oracle_root = oracle_root
        self._solutions_root = solutions_root
        self._solutions: dict[str, RecordedSolution] = {}
        self.providers: list[RecordedSolutionProvider] = []
        self.opened_run_ids: set[UUID] = set()

    def prepare(self, case_ids: Sequence[str] | None = None) -> None:
        selected = (
            tuple(case_ids)
            if case_ids is not None
            else tuple(case.case_id for case in self._loaded.manifest.cases)
        )
        for case_id in selected:
            if case_id in self._solutions:
                continue
            case = self._loaded.manifest.case(case_id)
            self._solutions[case_id] = prepare_recorded_solution(
                self._loaded,
                case,
                git_executable=self._git,
                oracle_root=self._oracle_root,
                solutions_root=self._solutions_root,
            )

    def __call__(
        self,
        case: CodingEvalTaskPackCase,
        run_id: UUID,
    ) -> AbstractAsyncContextManager[ModelProvider]:
        if case.case_id not in self._solutions:
            raise AssertionError(f"Recorded Case尚未准备：{case.case_id}")
        if run_id in self.opened_run_ids:
            raise AssertionError(f"完成Run不得重新打开Provider：{run_id}")
        self.opened_run_ids.add(run_id)

        @asynccontextmanager
        async def context() -> AsyncGenerator[ModelProvider, None]:
            provider = RecordedSolutionProvider(self._solutions[case.case_id], run_id)
            self.providers.append(provider)
            yield provider

        return context()
