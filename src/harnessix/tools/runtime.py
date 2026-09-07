from __future__ import annotations

import asyncio
import errno
import json
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import JsonValue, ValidationError

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import ToolCallContent, ToolResultContent
from harnessix.artifacts.contracts import ArtifactPage, ArtifactToolResult, ReadArtifactInput
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.tools import files, git, search
from harnessix.tools.contracts import (
    MAX_DIRECTORY_ENTRIES,
    MAX_LINE_BYTES,
    MAX_RESULT_BYTES,
    MAX_SCAN_BYTES,
    MAX_TEXT_BYTES,
    READ_TIMEOUT_SECONDS,
    ListFilesInput,
    ListFilesOutput,
    ReadContract,
    ReadFileInput,
    ReadFileOutput,
    ReadToolError,
)
from harnessix.tools.git_contracts import (
    GitDiffInput,
    GitDiffOutput,
    GitStatusInput,
    GitStatusOutput,
)
from harnessix.tools.search_contracts import (
    ArchivedGlobOutput,
    ArchivedGrepOutput,
    GlobInput,
    GlobOutput,
    GrepInput,
    GrepOutput,
    SearchInput,
)
from harnessix.tools.workspace import ReadOperation, Workspace, digest, run_read_operation


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


@dataclass(frozen=True)
class _ReadBinding:
    name: str
    description: str
    input_model: (
        type[ListFilesInput]
        | type[ReadFileInput]
        | type[GlobInput]
        | type[GrepInput]
        | type[GitStatusInput]
        | type[GitDiffInput]
    )
    output_model: (
        type[ListFilesOutput]
        | type[ReadFileOutput]
        | type[GlobOutput]
        | type[GrepOutput]
        | type[GitStatusOutput]
        | type[GitDiffOutput]
    )


_BINDINGS = (
    _ReadBinding(
        "list_files",
        "分页列出工作区目录；隐藏拒绝路径，不递归、不跟随链接",
        ListFilesInput,
        ListFilesOutput,
    ),
    _ReadBinding(
        "read_file",
        "有界读取工作区 UTF-8 文件；后续页须携带 revision，不跟随链接",
        ReadFileInput,
        ReadFileOutput,
    ),
    _ReadBinding(
        "glob",
        "在工作区目录内有界定位文件；支持 * ? [] 和 ** 路径段，显式报告截断",
        GlobInput,
        GlobOutput,
    ),
    _ReadBinding(
        "grep",
        "工作区目录内按大小写敏感字面量查找命中行，不支持正则；返回片段和 revision",
        GrepInput,
        GrepOutput,
    ),
)

_GIT_BINDINGS = (
    _ReadBinding(
        "git_status",
        "读取当前仓库的分支和结构化变更状态；不刷新索引、不执行Hook",
        GitStatusInput,
        GitStatusOutput,
    ),
    _ReadBinding(
        "git_diff",
        "读取当前仓库有界工作区或暂存区差异；禁用外部Diff和textconv",
        GitDiffInput,
        GitDiffOutput,
    ),
)


def _argument_failure(binding: _ReadBinding, arguments: dict[str, JsonValue]) -> tuple[str, str]:
    if binding.input_model is ReadFileInput:
        position = arguments.get("start_line", 1)
        first_position = 1
    elif binding.input_model is ListFilesInput:
        position = arguments.get("offset", 0)
        first_position = 0
    else:
        return "tool_invalid_arguments", "工具参数不符合契约"
    if (
        type(position) is not int
        or position <= first_position
        or arguments.get("expected_revision") is not None
    ):
        return "tool_invalid_arguments", "工具参数不符合契约"
    try:
        binding.input_model.model_validate({**arguments, "expected_revision": "0" * 64})
    except ValidationError:
        return "tool_invalid_arguments", "工具参数不符合契约"
    return (
        "tool_expected_revision_required",
        f"{binding.name}后续页必须携带上一成功结果的revision作为expected_revision",
    )


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
        max_concurrent_reads: int = 4,
    ) -> None:
        if type(max_concurrent_reads) is not int or not 1 <= max_concurrent_reads <= 16:
            raise KernelError("tool_concurrency_invalid", "只读工具并发上限必须在1到16之间")
        self._workspace = Workspace(root, denied_paths=denied_paths)
        self._max_concurrent_reads = max_concurrent_reads
        self._lock = asyncio.BoundedSemaphore(max_concurrent_reads)
        self._closed = False
        self._artifacts = artifacts
        self._git = (
            git.GitReadRuntime(self._workspace.root, git_executable) if git_executable else None
        )
        self._definitions: dict[str, ToolDescriptor] = {}
        bindings = (*_BINDINGS, *(_GIT_BINDINGS if self._git is not None else ()))
        for binding in bindings:
            rules: dict[str, object] = {
                "implementation": "coding-read/v1",
                "scope": self._workspace.scope,
                "input": binding.input_model.model_json_schema(),
                "output": binding.output_model.model_json_schema(),
                "concurrency": "parallel_read",
                "max_concurrent_reads": max_concurrent_reads,
                "max_text_bytes": MAX_TEXT_BYTES,
                "max_line_bytes": MAX_LINE_BYTES,
                "max_scan_bytes": MAX_SCAN_BYTES,
                "max_directory_entries": MAX_DIRECTORY_ENTRIES,
                "max_result_bytes": MAX_RESULT_BYTES,
                "timeout": READ_TIMEOUT_SECONDS,
            }
            if issubclass(binding.input_model, SearchInput):
                rules["search"] = search.execution_contract()
                if artifacts is not None:
                    rules["artifacts"] = artifacts.contract()
                    output_model = (
                        ArchivedGlobOutput if binding.name == "glob" else ArchivedGrepOutput
                    )
                    rules["output"] = output_model.model_json_schema()
            if binding in _GIT_BINDINGS:
                assert self._git is not None
                rules = {
                    **rules,
                    "implementation": "git-read/v1",
                    "git": self._git.contract(),
                }
            contract = digest(rules)
            self._definitions[binding.name] = ToolDescriptor(
                name=binding.name,
                version=f"1.{contract}",
                description=binding.description,
                input_schema=binding.input_model.model_json_schema(),
                effect_class=EffectClass.READ_ONLY,
                risk_level=RiskLevel.LOW,
                requires_idempotency=False,
                requires_approval=require_approval,
                supports_reconciliation=False,
                supports_parallel_calls=True,
            )
        if artifacts is not None:
            contract = digest(
                {
                    "scope": self._workspace.scope,
                    "artifacts": artifacts.contract(),
                    "input": ReadArtifactInput.model_json_schema(),
                    "output": ArtifactPage.model_json_schema(),
                    "concurrency": "parallel_read",
                    "max_concurrent_reads": max_concurrent_reads,
                }
            )
            self._definitions["read_artifact"] = ToolDescriptor(
                name="read_artifact",
                version=f"1.{contract}",
                description="分页读取当前会话/工作区的 JSONL Artifact；过期或损坏明确失败",
                input_schema=ReadArtifactInput.model_json_schema(),
                effect_class=EffectClass.READ_ONLY,
                risk_level=RiskLevel.LOW,
                requires_idempotency=False,
                requires_approval=require_approval,
                supports_reconciliation=False,
                supports_parallel_calls=True,
            )

    def definitions(self) -> tuple[ToolDescriptor, ...]:
        return tuple(d.model_copy(deep=True) for d in self._definitions.values())

    @property
    def workspace_root(self) -> Path:
        """宿主创建 Thread 时使用的规范根；访问能力仍由 Workspace 持有。"""
        return self._workspace.root

    @property
    def workspace_scope(self) -> str:
        return self._workspace.scope

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
                return self._failure(call, "tool_invalid_arguments", "工具参数不符合契约")
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
            for b in (*_BINDINGS, *(_GIT_BINDINGS if self._git is not None else ()))
            if b.name == call.tool
        )
        try:
            args = binding.input_model.model_validate(call.arguments)
        except ValidationError:
            code, message = _argument_failure(binding, call.arguments)
            return self._failure(call, code, message)
        try:
            if isinstance(args, GitStatusInput | GitDiffInput):
                output = await cancel.run(self._execute_git(args, cancel))
            else:
                output = await cancel.run(self._execute_read(args, capture=capture))
        except ReadToolError as error:
            return self._failure(call, f"tool_{error.code}", "工作区读取未完成")
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
                if isinstance(args, ListFilesInput):
                    return files.list_files(self._workspace, args, operation)
                if isinstance(args, GlobInput):
                    return search.glob(self._workspace, args, operation, capture=capture)
                if isinstance(args, GrepInput):
                    return search.grep(self._workspace, args, operation, capture=capture)
                return files.read_file(self._workspace, args, operation)

            return await run_read_operation(read)

    async def _execute_git(
        self, args: GitStatusInput | GitDiffInput, cancel: CancelToken
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
                self._workspace.close()
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
