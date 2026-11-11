"""注册Workspace只读Tool并协调有界并发、取消和Artifact发布，不提供写入能力。"""

from __future__ import annotations

import asyncio
import errno
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from pydantic import JsonValue, ValidationError

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import ToolCallContent, ToolResultContent
from harnessix.artifacts.contracts import ArtifactPage, ArtifactToolResult, ReadArtifactInput
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import ContractModel, ToolDescriptor
from harnessix.sandbox.contracts import container_workspace_path_problem
from harnessix.tools import files, search
from harnessix.tools.argument_feedback import invalid_argument_message
from harnessix.tools.argument_repair import ARTIFACT_ID, repair_read_arguments
from harnessix.tools.contracts import (
    MAX_RESULT_BYTES,
    READ_TIMEOUT_SECONDS,
    ListFilesInput,
    ReadContract,
    ReadFileInput,
    ReadFileViewOutput,
    ReadToolError,
)
from harnessix.tools.git_contracts import (
    GitDiffInput,
    GitReviewInput,
    GitStatusInput,
)
from harnessix.tools.grep_matcher import RegexSearchError
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
from harnessix.tools.workspace import ReadOperation, Workspace, run_read_operation

if TYPE_CHECKING:
    from harnessix.processes.owner_protocol import OutputRedactionSource


# 写给模型的失败说明（英文）：说明发生了什么与下一步，不回显路径或查询值；
# 界面按错误码另行展示用户文案（product_ui.tool_summary）。
_READ_ERROR_MESSAGES = {
    "anchor_not_found": (
        "No line of the file contains anchor. anchor is literal text inside one line (not a "
        "regex, no line breaks); copy it from a line you have seen in this file, or locate the "
        "line with grep on this path and read by start_line"
    ),
    "anchor_ambiguous": (
        "More than one line contains anchor. Use a longer piece of the line that occurs once, "
        "or grep this path for it and read by start_line"
    ),
    "anchor_outside_page": (
        "The page ended before the anchor line. Use a smaller lines_before or a larger max_lines"
    ),
    "definition_unavailable": (
        "view=definition works only for a real def/class header in a Python file within the "
        "size limit. Use view=text, or check that anchor is the def/class line"
    ),
    "limit_exceeded": (
        "The scan hit a tool limit (entries, bytes or depth) and returned nothing. Do not "
        "retry unchanged: set path to a more specific subdirectory, or find candidate files "
        "with glob first and grep those"
    ),
    # 只说明可操作的下一步，不区分“越界”与“敏感路径拒绝”，不回显路径。
    "path_denied": (
        "This path cannot be read. Read tools only read files inside the workspace, addressed "
        "relative to the workspace root (for example src/app.py). Use the command tool for "
        "paths outside it such as temporary directories; some sensitive paths are never readable"
    ),
    "not_found": (
        "Path not found. Paths are relative to the workspace root; confirm the path with glob "
        "or list_files before retrying"
    ),
    "wrong_file_type": (
        "The path is a directory where a file is needed, or a file where a directory is "
        "needed. Use list_files or glob for directories and read_file for files"
    ),
    "invalid_utf8": "The file is not valid UTF-8 text. Inspect it with the command tool",
    "binary_file": "The file contains binary data. Inspect it with the command tool",
    "offset_out_of_range": (
        "The start position is past the end. Read from line 1, or use the next_line or "
        "next_offset value of the previous page"
    ),
    "page_changed": (
        "The file or directory changed since expected_revision was issued. Read again without "
        "expected_revision"
    ),
    "workspace_changed": "The workspace changed during the read. Retry the call",
    "no_files_searched": (
        "No files were searched: no file under path matches include. include filters by file "
        "name (for example *.py) or by a path relative to path; to search one known file pass "
        "it as path, and check names with glob"
    ),
    "io_failed": "Reading failed with an I/O error. Retry, or use the command tool",
    "timeout": (
        "The read or search timed out and returned nothing. Do not retry unchanged: set path "
        "to a specific subdirectory, narrow include, or find candidate files with glob first"
    ),
}
# 上表覆盖全部ReadErrorCode；兜底只在出现表外错误码时使用，保持原说明（界面照旧显示）。
_READ_ERROR_FALLBACK = "工作区读取未完成"
# 模型常把命令输出存档编号当作read_file路径，或沿用read_artifact的分页参数。
_ARTIFACT_READ_HINT = (
    "read_file reads workspace files only and pages by start_line and max_lines; for stored "
    "command output use read_artifact(artifact_id, offset, limit)"
)
_ARTIFACT_ERROR_MESSAGES = {
    "artifact_not_found": (
        "No stored output has this artifact_id in this session. Copy the id from the artifact "
        "field of a tool result of this session, or rerun the command"
    ),
    "artifact_expired": "This stored output has expired. Rerun the command that produced it",
    "artifact_invalid_cursor": (
        "offset is past the end of the stored output. Use next_offset from the previous page, "
        "or start at offset 0"
    ),
}
# 存档损坏、发布器不匹配等不是模型能改正的调用错误：保持原说明（界面照旧显示）。
_ARTIFACT_ERROR_FALLBACK = "Artifact 读取未完成"


def _misrouted_artifact_read(binding: _ReadBinding, arguments: Mapping[str, Any]) -> bool:
    if not issubclass(binding.input_model, ReadFileInput):
        return False
    path = arguments.get("path")
    return bool({"artifact_id", "offset", "limit"} & arguments.keys()) or (
        isinstance(path, str) and ARTIFACT_ID.match(path) is not None
    )


_OUTPUT_LIMIT_MESSAGE = (
    "The result exceeds the output limit and was not returned. Do not retry unchanged: narrow "
    "path, lower context_lines or max_results, or page a file with start_line and max_lines"
)


def _read_output_payload(
    binding: _ReadBinding, output: ReadContract
) -> dict[str, JsonValue] | None:
    """严格核验并序列化绑定输出；text视图不增加空元数据，超限不发布。"""
    try:
        checked = binding.output_model.model_validate_json(output.model_dump_json())
    except (ValueError, TypeError, AttributeError):
        raise KernelError("tool_output_invalid", "只读工具输出不符合绑定契约") from None
    if len(checked.model_dump_json().encode()) > MAX_RESULT_BYTES:
        return None
    payload: JsonValue = checked.model_dump(mode="json")
    assert isinstance(payload, dict)
    if isinstance(checked, ReadFileViewOutput) and checked.definition is None:
        payload.pop("definition", None)
    return payload


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
    validation_errors: Sequence[Mapping[str, Any]] = (),
) -> tuple[str, str]:
    """只投影正式字段和白名单规则，不回显参数值或第三方校验异常。"""
    return "tool_invalid_arguments", invalid_argument_message(
        input_model.model_json_schema(),
        arguments,
        constraint_codes=constraint_codes,
        validation_errors=validation_errors,
    )


def _workspace_relative(
    arguments: dict[str, JsonValue], container_paths: tuple[str, ...]
) -> dict[str, JsonValue]:
    """容器挂载点前缀换算为相对路径；之后仍走原有相对路径校验，不扩大可读范围。"""
    path = arguments.get("path")
    if not isinstance(path, str):
        return arguments
    for mount in container_paths:
        if path.rstrip("/") == mount:
            return {**arguments, "path": "."}
        if path.startswith(mount + "/"):
            return {**arguments, "path": path[len(mount) + 1 :].lstrip("/") or "."}
    return arguments


def _read_error_message(binding: _ReadBinding, code: str, call: ToolCallContent) -> str:
    message = _READ_ERROR_MESSAGES.get(code, _READ_ERROR_FALLBACK)
    if code in {"not_found", "path_denied"} and _misrouted_artifact_read(binding, call.arguments):
        return f"{_ARTIFACT_READ_HINT}. {message}"
    return message


def _argument_failure(
    binding: _ReadBinding, arguments: dict[str, JsonValue], error: ValidationError
) -> tuple[str, str]:
    details = error.errors(include_url=False, include_input=False, include_context=False)
    codes = tuple(detail["type"] for detail in details)
    invalid = _invalid_arguments(binding.input_model, arguments, codes, details)
    if _misrouted_artifact_read(binding, arguments):
        return invalid[0], f"{_ARTIFACT_READ_HINT}. {invalid[1]}"
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
        f"Later pages of {binding.name} need expected_revision set to the revision of the "
        "previous successful page",
    )


async def _observe_for_repair[T](
    tools: CodingToolRuntime, observe: Callable[[Workspace, str, ReadOperation], T], path: str
) -> T | None:
    """提案修复的观察与模型读取同边界、同并发上限；任何失败只表示“无法补全”，不成为结果。"""
    workspace = tools._workspace
    if workspace is None:
        return None
    async with tools._lock:
        if tools._closed:
            return None
        try:
            return await run_read_operation(
                lambda operation: observe(workspace, path, operation),
                timeout_seconds=tools._read_timeout_seconds,
            )
        except (ReadToolError, OSError, ValueError):
            return None


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
        enable_regex_search: bool = False,
        enable_definition_views: bool = False,
        read_timeout_seconds: int = READ_TIMEOUT_SECONDS,
        container_workspace_paths: tuple[str, ...] = (),
    ) -> None:
        if type(read_timeout_seconds) is not int or not 1 <= read_timeout_seconds <= 60:
            raise KernelError("tool_timeout_invalid", "读工具时限必须是1到60秒的整数")
        if any(container_workspace_path_problem(path) for path in container_workspace_paths):
            raise KernelError("tool_path_alias_invalid", "容器工作区路径必须是规范绝对路径")
        self._read_timeout_seconds = read_timeout_seconds
        # 命令容器内的工作区挂载点；读工具把以它开头的绝对路径换算为相对工作区根的路径。
        self._container_workspace_paths = container_workspace_paths
        self._read_bindings = _read_bindings(
            enable_grep_snapshots, enable_regex_search, enable_definition_views
        )
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
            read_timeout_seconds=read_timeout_seconds,
        )

    def definitions(self) -> tuple[ToolDescriptor, ...]:
        return tuple(d.model_copy(deep=True) for d in self._definitions.values())

    def _absolute_workspace_paths(self) -> tuple[str, ...]:
        """可换算为相对路径的绝对前缀：命令容器挂载点与POSIX工作区根本身（命令工具里的绝对路径）。"""
        if self._workspace is None:
            return self._container_workspace_paths
        return (*self._container_workspace_paths, str(self._workspace.root))

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

    async def read_workspace_file(self, path: str) -> bytes | None:
        """完整读取一个不超过扫描上限的工作区文件，供提案修复核对；不可读或不支持时返回None。"""
        return await _observe_for_repair(self, files.read_complete_bytes, path)

    async def workspace_file_mode(self, path: str) -> int | None:
        """返回工作区普通文件的权限位，供提案修复沿用现有权限；不可读或不支持时返回None。"""
        return await _observe_for_repair(self, files.file_permission_mode, path)

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
                return await run_read_operation(inspect, timeout_seconds=self._read_timeout_seconds)

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
            except ValidationError as error:
                details = error.errors(include_url=False, include_input=False)
                return self._failure(
                    call, *_invalid_arguments(ReadArtifactInput, call.arguments, (), details)
                )
            except KernelError as error:
                message = _ARTIFACT_ERROR_MESSAGES.get(error.code, _ARTIFACT_ERROR_FALLBACK)
                return self._failure(call, error.code, message)
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
        arguments = repair_read_arguments(
            binding.name,
            _workspace_relative(call.arguments, self._absolute_workspace_paths()),
            binding.input_model,
        )
        try:
            args = binding.input_model.model_validate(arguments)
        except ValidationError as error:
            code, message = _argument_failure(binding, arguments, error)
            return self._failure(call, code, message)
        try:
            if isinstance(args, GitStatusInput | GitDiffInput | GitReviewInput):
                output = await cancel.run(self._execute_git(args, cancel))
            else:
                output = await cancel.run(self._execute_read(args, capture=capture))
        except RegexSearchError as error:
            return self._failure(call, error.code, error.message)
        except ReadToolError as error:
            return self._failure(
                call, f"tool_{error.code}", _read_error_message(binding, error.code, call)
            )
        except OSError as error:
            code = {
                errno.ENOENT: "not_found",
                errno.EACCES: "path_denied",
                errno.EPERM: "path_denied",
                errno.ENOTDIR: "wrong_file_type",
            }.get(error.errno or 0, "io_failed")
            return self._failure(call, f"tool_{code}", _read_error_message(binding, code, call))
        cancel.checkpoint()
        payload = _read_output_payload(binding, output)
        if payload is None:
            return self._failure(call, "tool_limit_exceeded", _OUTPUT_LIMIT_MESSAGE)
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

            return await run_read_operation(read, timeout_seconds=self._read_timeout_seconds)

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
