from __future__ import annotations

import asyncio
import errno
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import JsonValue, ValidationError

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import ToolCallContent, ToolResultContent
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.tools import files
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
from harnessix.tools.workspace import ReadOperation, Workspace, digest


@dataclass(frozen=True)
class _ReadBinding:
    name: str
    description: str
    input_model: type[ListFilesInput] | type[ReadFileInput]
    output_model: type[ListFilesOutput] | type[ReadFileOutput]


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
)


class CodingToolRuntime:
    """固定只读绑定；宿主拥有能力选择，Kernel 拥有审批与调度。"""

    def __init__(
        self, root: Path, *, denied_paths: tuple[str, ...] = (), require_approval: bool = False
    ) -> None:
        self._workspace = Workspace(root, denied_paths=denied_paths)
        self._lock = asyncio.Lock()
        self._closed = False
        self._definitions: dict[str, ToolDescriptor] = {}
        for binding in _BINDINGS:
            contract = digest(
                {
                    "implementation": "coding-read/v1",
                    "scope": self._workspace.scope,
                    "input": binding.input_model.model_json_schema(),
                    "output": binding.output_model.model_json_schema(),
                    "concurrency": "serial",
                    "max_text_bytes": MAX_TEXT_BYTES,
                    "max_line_bytes": MAX_LINE_BYTES,
                    "max_scan_bytes": MAX_SCAN_BYTES,
                    "max_directory_entries": MAX_DIRECTORY_ENTRIES,
                    "max_result_bytes": MAX_RESULT_BYTES,
                    "timeout": READ_TIMEOUT_SECONDS,
                }
            )
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
            )

    def definitions(self) -> tuple[ToolDescriptor, ...]:
        return tuple(d.model_copy(deep=True) for d in self._definitions.values())

    async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent:
        cancel.checkpoint()
        definition = self._definitions.get(call.tool)
        if definition is None:
            return self._failure(call, "unknown_tool", "工具未注册")
        if (
            call.tool_version != definition.version
            or call.tool_fingerprint != tool_fingerprint(definition)
            or call.effect_class != definition.effect_class
            or call.requires_approval != definition.requires_approval
        ):
            raise KernelError("tool_contract_changed", "工具或工作区能力已变化")
        binding = next(b for b in _BINDINGS if b.name == call.tool)
        try:
            args = binding.input_model.model_validate(call.arguments)
        except ValidationError:
            return self._failure(call, "tool_invalid_arguments", "工具参数不符合契约")
        try:
            output = await cancel.run(self._execute_read(args))
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

    async def _execute_read(self, args: ListFilesInput | ReadFileInput) -> ReadContract:
        async with self._lock:
            if self._closed:
                raise KernelError("tool_runtime_closed", "工具运行时已关闭")
            operation = ReadOperation()

            def read() -> ReadContract:
                if isinstance(args, ListFilesInput):
                    return files.list_files(self._workspace, args, operation)
                return files.read_file(self._workspace, args, operation)

            worker = asyncio.create_task(asyncio.to_thread(read))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                operation.stopped.set()
                # 即便父任务再次取消，也先等待线程释放 FD，不能把清理变成后台工作。
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not worker.cancelled():
                    worker.exception()
                raise

    @staticmethod
    def _failure(call: ToolCallContent, code: str, message: str) -> ToolResultContent:
        return ToolResultContent(
            call_id=call.call_id, outcome="failed", error=AgentFailure(code=code, message=message)
        )

    async def aclose(self) -> None:
        self._closed = True
        async with self._lock:
            self._workspace.close()

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
