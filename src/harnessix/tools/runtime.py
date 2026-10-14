"""注册Workspace只读Tool并协调有界并发、取消和Artifact发布，不提供写入能力。"""

from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Self

from pydantic import JsonValue, ValidationError

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import ToolCallContent, ToolResultContent
from harnessix.artifacts.contracts import ArtifactPage, ArtifactToolResult, ReadArtifactInput
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import ContractModel, ToolDescriptor
from harnessix.tools import files, search
from harnessix.tools.argument_feedback import invalid_argument_message
from harnessix.tools.contracts import (
    MAX_RESULT_BYTES,
    ListFilesInput,
    ReadContract,
    ReadFileInput,
    ReadToolError,
)
from harnessix.tools.git_contracts import (
    GitDiffInput,
    GitReviewInput,
    GitStatusInput,
)
from harnessix.tools.read_backend import build_read_backend
from harnessix.tools.read_catalog import (
    _GIT_BINDINGS,
    _build_definitions,
    _read_bindings,
    _ReadBinding,
)
from harnessix.tools.search_contracts import (
    GlobInput,
    GrepInput,
)
from harnessix.tools.workspace import ReadOperation, run_read_operation

if TYPE_CHECKING:
    from harnessix.processes.owner_protocol import OutputRedactionSource


async def _drain[T](task: asyncio.Task[T]) -> None:
    """取消已发生后仍回收资源任务；其错误不覆盖原始取消信号。"""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if not task.cancelled():
        task.exception()


def _invalid_arguments(
    input_model: type[ContractModel],
    arguments: dict[str, JsonValue],
    constraint_codes: tuple[str, ...] = (),
) -> tuple[str, str]:
    """只投影正式字段和白名单规则，不回显参数值或第三方校验异常。"""
    return "tool_invalid_arguments", invalid_argument_message(
        input_model.model_json_schema(), arguments, constraint_codes=constraint_codes
    )


def _argument_failure(
    binding: _ReadBinding, arguments: dict[str, JsonValue], error: ValidationError
) -> tuple[str, str]:
    codes = tuple(
        detail["type"]
        for detail in error.errors(include_url=False, include_input=False, include_context=False)
    )
    invalid = _invalid_arguments(binding.input_model, arguments, codes)
    if binding.input_model is ListFilesInput:
        position = arguments.get("offset", 0)
        first_position = 0
    else:
        return invalid
    if (
        type(position) is not int
        or position <= first_position
        or arguments.get("expected_revision") is not None
    ):
        return invalid
    try:
        binding.input_model.model_validate({**arguments, "expected_revision": "0" * 64})
    except ValidationError:
        return invalid
    return (
        "tool_expected_revision_required",
        f"{binding.name}后续页必须携带上一成功结果的revision作为expected_revision",
    )


def _read_semaphore(limit: int) -> asyncio.BoundedSemaphore:
    if type(limit) is not int or not 1 <= limit <= 16:
        raise KernelError("tool_concurrency_invalid", "只读工具并发上限必须在1到16之间")
    return asyncio.BoundedSemaphore(limit)


class CodingToolRuntime:
    """固定只读绑定；宿主拥有能力选择，Kernel 拥有审批与调度。"""

    def __init__(
        self,
        root: Path,
        *,
        denied_paths: tuple[str, ...] = (),
        require_approval: bool = False,
        artifacts: SQLiteArtifactStore | None = None,
        git_executable: Path | None = None,
        git_state_directory: Path | None = None,
        git_output_redaction: OutputRedactionSource | None = None,
        max_concurrent_reads: int = 4,
        enable_grep_snapshots: bool = False,
    ) -> None:
        self._read_bindings = _read_bindings(enable_grep_snapshots)
        self._lock = _read_semaphore(max_concurrent_reads)
        backend = build_read_backend(
            root, denied_paths, git_executable, git_state_directory, git_output_redaction
        )
        self._workspace = backend.workspace
        self._windows = backend.windows
        self._max_concurrent_reads = max_concurrent_reads
        self._closed = False
        self._artifacts = artifacts
        self._git = backend.git_runtime
        self._definitions = _build_definitions(
            scope=self.workspace_scope,
            implementation=backend.implementation,
            max_concurrent_reads=max_concurrent_reads,
            require_approval=require_approval,
            artifacts=artifacts,
            git_runtime=self._git,
            read_bindings=self._read_bindings,
        )

    def definitions(self) -> tuple[ToolDescriptor, ...]:
        return tuple(d.model_copy(deep=True) for d in self._definitions.values())

    @property
    def workspace_root(self) -> Path:
        """宿主创建 Thread 时使用的规范根；访问能力仍由 Workspace 持有。"""
        if self._workspace is not None:
            return self._workspace.root
        assert self._windows is not None
        return self._windows.root

    @property
    def workspace_scope(self) -> str:
        if self._workspace is not None:
            return self._workspace.scope
        assert self._windows is not None
        return self._windows.scope

    async def artifact_workspace_scope(self, workspace: str, cancel: CancelToken) -> str:
        if workspace != str(self.workspace_root):
            raise KernelError("tool_workspace_mismatch", "Artifact访问与已绑定工作区不匹配")

        def inspect(operation: ReadOperation) -> str:
            if self._workspace is not None:
                with self._workspace.open(".", operation, directory=True):
                    return self.workspace_scope
            assert self._windows is not None
            return self._windows.inspect(operation)

        async def locked() -> str:
            async with self._lock:
                if self._closed:
                    raise KernelError("tool_runtime_closed", "工具运行时已关闭")
                return await run_read_operation(inspect)

        try:
            return await cancel.run(locked())
        except ReadToolError as error:
            raise KernelError(error.code, "Artifact工作区访问能力失效") from None

    async def execute_scoped(
        self, call: ToolCallContent, scope: ToolExecutionScope, cancel: CancelToken
    ) -> ToolResultContent | ArtifactToolResult:
        cancel.checkpoint()
        scope.validate_call(call)
        if scope.workspace != str(self.workspace_root):
            raise KernelError("tool_workspace_mismatch", "执行作用域与已绑定工作区不匹配")
        if self._closed:
            raise KernelError("tool_runtime_closed", "工具运行时已关闭")
        if self._artifacts is None:
            return await self.execute(call, cancel)
        if call.tool == "read_artifact":
            self._validate_definition(call)
            try:
                args = ReadArtifactInput.model_validate_json(json.dumps(call.arguments))
                page = await cancel.run(self._read_artifact(scope, args))
            except ValidationError:
                return self._failure(call, *_invalid_arguments(ReadArtifactInput, call.arguments))
            except KernelError as error:
                return self._failure(call, error.code, "Artifact 读取未完成")
            return ToolResultContent(
                call_id=call.call_id, outcome="succeeded", output=page.model_dump(mode="json")
            )
        capture = search.SearchCapture() if call.tool in {"glob", "grep"} else None
        result = await self._execute_call(call, cancel, capture=capture)
        if capture is not None and result.outcome == "succeeded":
            return ArtifactToolResult(
                result, bytes(capture.body), self.workspace_scope, capture.complete, self._artifacts
            )
        return result

    async def _read_artifact(
        self, scope: ToolExecutionScope, args: ReadArtifactInput
    ) -> ArtifactPage:
        async with self._lock:
            if self._closed:
                raise KernelError("tool_runtime_closed", "工具运行时已关闭")
            assert self._artifacts is not None
            return await self._artifacts.read(
                scope.thread_id,
                self.workspace_scope,
                args.artifact_id,
                offset=args.offset,
                limit=args.limit,
            )

    async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent:
        if self._artifacts is not None and call.tool in {"glob", "grep", "read_artifact"}:
            raise KernelError("artifact_scope_required", "Artifact 工具需要 Scoped 入口")
        return await self._execute_call(call, cancel)

    def _validate_definition(self, call: ToolCallContent) -> None:
        definition = self._definitions[call.tool]
        if (
            call.tool_version != definition.version
            or call.tool_fingerprint != tool_fingerprint(definition)
            or call.effect_class != definition.effect_class
            or call.requires_approval != definition.requires_approval
        ):
            raise KernelError("tool_contract_changed", "工具或工作区能力已变化")

    async def _execute_call(
        self,
        call: ToolCallContent,
        cancel: CancelToken,
        *,
        capture: search.SearchCapture | None = None,
    ) -> ToolResultContent:
        cancel.checkpoint()
        definition = self._definitions.get(call.tool)
        if definition is None:
            return self._failure(call, "unknown_tool", "工具未注册")
        self._validate_definition(call)
        binding = next(
            b
            for b in (*self._read_bindings, *(_GIT_BINDINGS if self._git is not None else ()))
            if b.name == call.tool
        )
        try:
            args = binding.input_model.model_validate(call.arguments)
        except ValidationError as error:
            code, message = _argument_failure(binding, call.arguments, error)
            return self._failure(call, code, message)
        try:
            if isinstance(args, GitStatusInput | GitDiffInput | GitReviewInput):
                output = await cancel.run(self._execute_git(args, cancel))
            else:
                output = await cancel.run(self._execute_read(args, capture=capture))
        except ReadToolError as error:
            message = {
                "anchor_not_found": "文件中没有该字面锚点；核对文件或改用窄范围grep定位",
                "anchor_ambiguous": "字面锚点不唯一；使用更具体的锚点或grep返回的行号",
                "anchor_outside_page": "文本页未覆盖锚点；减小lines_before或增大页容量",
            }.get(error.code, "工作区读取未完成")
            return self._failure(call, f"tool_{error.code}", message)
        except OSError as error:
            code = {
                errno.ENOENT: "not_found",
                errno.EACCES: "path_denied",
                errno.EPERM: "path_denied",
                errno.ENOTDIR: "wrong_file_type",
            }.get(error.errno or 0, "io_failed")
            return self._failure(call, f"tool_{code}", "工作区读取未完成")
        cancel.checkpoint()
        try:
            checked = binding.output_model.model_validate_json(output.model_dump_json())
        except (ValueError, TypeError, AttributeError):
            raise KernelError("tool_output_invalid", "只读工具输出不符合绑定契约") from None
        if len(checked.model_dump_json().encode()) > MAX_RESULT_BYTES:
            return self._failure(call, "tool_limit_exceeded", "工具结果超过输出上限")
        payload: JsonValue = checked.model_dump(mode="json")
        return ToolResultContent(call_id=call.call_id, outcome="succeeded", output=payload)

    async def _execute_read(
        self,
        args: ListFilesInput | ReadFileInput | GlobInput | GrepInput,
        *,
        capture: search.SearchCapture | None = None,
    ) -> ReadContract:
        async with self._lock:
            if self._closed:
                raise KernelError("tool_runtime_closed", "工具运行时已关闭")

            def read(operation: ReadOperation) -> ReadContract:
                if self._windows is not None:
                    return self._windows.execute(args, operation, capture=capture)
                assert self._workspace is not None
                if isinstance(args, ListFilesInput):
                    return files.list_files(self._workspace, args, operation)
                if isinstance(args, GlobInput):
                    return search.glob(self._workspace, args, operation, capture=capture)
                if isinstance(args, GrepInput):
                    return search.grep(self._workspace, args, operation, capture=capture)
                return files.read_file_snapshot(self._workspace, args, operation)

            return await run_read_operation(read)

    async def _execute_git(
        self, args: GitStatusInput | GitDiffInput | GitReviewInput, cancel: CancelToken
    ) -> ReadContract:
        async with self._lock:
            if self._closed:
                raise KernelError("tool_runtime_closed", "工具运行时已关闭")
            if self._git is None:
                raise KernelError("tool_contract_changed", "Git只读能力未绑定")
            return await self._git.execute(args, cancel)

    @staticmethod
    def _failure(call: ToolCallContent, code: str, message: str) -> ToolResultContent:
        return ToolResultContent(
            call_id=call.call_id, outcome="failed", error=AgentFailure(code=code, message=message)
        )

    async def aclose(self) -> None:
        self._closed = True

        async def close_scope() -> None:
            acquired = 0
            try:
                for _ in range(self._max_concurrent_reads):
                    await self._lock.acquire()
                    acquired += 1
                if self._workspace is not None:
                    self._workspace.close()
                if self._windows is not None:
                    self._windows.close()
            finally:
                for _ in range(acquired):
                    self._lock.release()

        closing = asyncio.create_task(close_scope())
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            await _drain(closing)
            raise

    async def __aenter__(self) -> Self:
        if self._closed:
            raise KernelError("tool_runtime_closed", "工具运行时已关闭")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
