from __future__ import annotations

import errno
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from harnessix.agent.cancellation import CancelToken
from harnessix.context.contracts import (
    ContextBuildInput,
    ContextFragment,
    ContextFragmentKind,
    ContextSourceDocument,
    ContextSourceDocumentSnapshot,
    ContextSourceObservation,
    ContextSourceSnapshot,
    PreparedContext,
)
from harnessix.context.engine import ContextEngine
from harnessix.tools import files
from harnessix.tools.contracts import ReadFileInput, ReadToolError
from harnessix.tools.workspace import (
    ReadOperation,
    Workspace,
    digest,
    revision_state,
    run_read_operation,
)

_SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._/-]*$")
_DYNAMIC_KINDS = frozenset(
    {
        ContextFragmentKind.PROJECT_INSTRUCTION,
        ContextFragmentKind.WORKSPACE,
        ContextFragmentKind.GIT,
        ContextFragmentKind.ENVIRONMENT,
    }
)
_PROJECT_SOURCE_ID = "project/instructions"
_PROJECT_CANDIDATES = ("AGENTS.override.md", "AGENTS.md")
MAX_PROJECT_INSTRUCTION_BYTES = 64 * 1024


class ContextSourceError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class ContextSource(Protocol):
    @property
    def source_id(self) -> str: ...

    @property
    def kind(self) -> ContextFragmentKind: ...

    async def observe(
        self, request: ContextBuildInput, cancel: CancelToken
    ) -> ContextSourceObservation: ...


class SourcedContextEngine:
    """先刷新宿主准入的动态来源，再委托纯 Context Engine 规划模型视图。"""

    def __init__(self, engine: ContextEngine, sources: Sequence[ContextSource]) -> None:
        copied = tuple(sources)
        if not copied or len(copied) > 16:
            raise ValueError("动态 Context Source 数量必须为 1 到 16")
        identities = [source.source_id for source in copied]
        if len(set(identities)) != len(identities):
            raise ValueError("Context Source 身份重复")
        if any(
            len(identity) > 128 or _SOURCE_ID.fullmatch(identity) is None for identity in identities
        ):
            raise ValueError("Context Source 身份不符合命名空间契约")
        if any(source.kind not in _DYNAMIC_KINDS for source in copied):
            raise ValueError("动态 Context Source 不得声明 Runtime 或 User 信任级别")
        self._engine = engine
        self._sources = copied

    async def prepare(self, request: ContextBuildInput, cancel: CancelToken) -> PreparedContext:
        fragments: list[ContextFragment] = []
        snapshots: list[ContextSourceSnapshot] = []
        for source in self._sources:
            cancel.checkpoint()
            observed = await source.observe(request, cancel)
            if not isinstance(observed, ContextSourceObservation):
                raise ContextSourceError("context_source_invalid", "Context Source 返回无效契约")
            document_snapshots: list[ContextSourceDocumentSnapshot] = []
            source_fragments: list[ContextFragment] = []
            for document in observed.documents:
                fragment = None
                if document.content.strip():
                    fragment = ContextFragment(
                        kind=source.kind,
                        source=document.source,
                        content=document.content,
                    )
                    fragments.append(fragment)
                    source_fragments.append(fragment)
                document_snapshots.append(
                    ContextSourceDocumentSnapshot(
                        source=document.source,
                        revision=document.revision,
                        utf8_bytes=len(document.content.encode()),
                        fragment_id=fragment.fragment_id if fragment is not None else None,
                    )
                )
            snapshots.append(
                ContextSourceSnapshot(
                    source_id=source.source_id,
                    kind=source.kind,
                    status="available" if source_fragments else "empty",
                    workspace_scope=observed.workspace_scope,
                    source_revision=observed.source_revision,
                    documents=tuple(document_snapshots),
                )
            )
        cancel.checkpoint()
        try:
            return self._engine.prepare_sourced(request, fragments, snapshots)
        except ValueError:
            raise ContextSourceError(
                "context_source_invalid", "Context Source 组合结果不符合规划契约"
            ) from None


class ProjectInstructionSource:
    """在宿主绑定工作区内按根到工作目录顺序发现项目指令。"""

    source_id = _PROJECT_SOURCE_ID
    kind = ContextFragmentKind.PROJECT_INSTRUCTION

    def __init__(
        self,
        root: Path,
        *,
        working_directory: str = ".",
        denied_paths: tuple[str, ...] = (),
        max_total_bytes: int = MAX_PROJECT_INSTRUCTION_BYTES,
    ) -> None:
        if type(max_total_bytes) is not int or not 1 <= max_total_bytes <= 131_072:
            raise ValueError("项目指令总量上限必须为 1 到 131072 字节")
        self._root = root.resolve(strict=True)
        self._working_directory = working_directory
        self._denied_paths = denied_paths
        self._max_total_bytes = max_total_bytes

    async def observe(
        self, request: ContextBuildInput, cancel: CancelToken
    ) -> ContextSourceObservation:
        try:
            return await cancel.run(
                run_read_operation(
                    lambda operation: self._observe_sync(operation, request.workspace)
                )
            )
        except ReadToolError as error:
            if error.code in {"limit_exceeded", "offset_out_of_range"}:
                raise ContextSourceError(
                    "context_source_too_large", "项目指令超过受控读取上限"
                ) from None
            if error.code in {"timeout", "workspace_changed", "page_changed", "io_failed"}:
                raise ContextSourceError(
                    "context_source_unavailable",
                    "项目指令来源暂时不可用",
                    retryable=True,
                ) from None
            raise ContextSourceError(
                "context_source_invalid", "项目指令文件不符合安全读取契约"
            ) from None
        except OSError as error:
            retryable = (error.errno or 0) not in {errno.EACCES, errno.EPERM}
            raise ContextSourceError(
                "context_source_unavailable",
                "项目指令来源暂时不可用",
                retryable=retryable,
            ) from None

    def _observe_sync(
        self, operation: ReadOperation, requested_workspace: str
    ) -> ContextSourceObservation:
        operation.checkpoint()
        try:
            requested_root = Path(requested_workspace).resolve(strict=True)
        except (OSError, ValueError):
            requested_root = None
        if requested_root != self._root:
            raise ContextSourceError(
                "context_source_workspace_mismatch", "Context Source 与 Thread 工作区不匹配"
            )
        operation.checkpoint()
        with Workspace(self._root, denied_paths=self._denied_paths) as workspace:
            parts = workspace.parts(self._working_directory)
            directories = tuple(
                "." if index == 0 else "/".join(parts[:index]) for index in range(len(parts) + 1)
            )
            before = tuple(
                (directory, self._directory_revision(workspace, directory, operation))
                for directory in directories
            )
            documents: list[ContextSourceDocument] = []
            total = 0
            for directory in directories:
                for filename in _PROJECT_CANDIDATES:
                    path = filename if directory == "." else f"{directory}/{filename}"
                    try:
                        document = self._read_document(
                            workspace, path, operation, self._max_total_bytes - total
                        )
                    except FileNotFoundError:
                        continue
                    total += len(document.content.encode())
                    if total > self._max_total_bytes:
                        raise ReadToolError("limit_exceeded")
                    documents.append(document)
                    break
            after = tuple(
                (directory, self._directory_revision(workspace, directory, operation))
                for directory in directories
            )
            if before != after:
                raise ReadToolError("workspace_changed")
            return ContextSourceObservation(
                workspace_scope=workspace.scope,
                source_revision=digest(
                    {
                        "contract": "project-instructions/v1",
                        "working_directory": self._working_directory,
                        "candidates": _PROJECT_CANDIDATES,
                        "directories": after,
                        "documents": [
                            (document.source, document.revision) for document in documents
                        ],
                    }
                ),
                documents=tuple(documents),
            )

    @staticmethod
    def _directory_revision(workspace: Workspace, path: str, operation: ReadOperation) -> str:
        with workspace.open(path, operation, directory=True) as descriptor:
            return digest((workspace.scope, path, revision_state(os.fstat(descriptor))))

    @staticmethod
    def _read_document(
        workspace: Workspace, path: str, operation: ReadOperation, remaining_bytes: int
    ) -> ContextSourceDocument:
        chunks: list[str] = []
        total = 0
        start_line = 1
        expected_revision = None
        while True:
            page = files.read_file(
                workspace,
                ReadFileInput(
                    path=path,
                    start_line=start_line,
                    max_lines=2000,
                    expected_revision=expected_revision,
                ),
                operation,
            )
            chunks.append(page.text)
            total += page.utf8_bytes
            if total > remaining_bytes:
                raise ReadToolError("limit_exceeded")
            expected_revision = page.revision
            if not page.truncated:
                return ContextSourceDocument(
                    source=path,
                    content="".join(chunks),
                    revision=page.revision,
                )
            assert page.next_line is not None
            start_line = page.next_line
