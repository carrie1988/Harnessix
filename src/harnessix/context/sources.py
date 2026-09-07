from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.context.contracts import (
    ContextBuildInput,
    ContextConsistencySnapshot,
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
from harnessix.tools.contracts import (
    ListFilesInput,
    ListFilesOutput,
    ReadFileInput,
    ReadToolError,
)
from harnessix.tools.git import GitReadRuntime
from harnessix.tools.git_contracts import GitStatusInput, GitStatusOutput
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
_WORKSPACE_SOURCE_ID = "workspace/layout"
_GIT_SOURCE_ID = "git/status"
_ENVIRONMENT_SOURCE_ID = "environment/runtime"
_PROJECT_CANDIDATES = ("AGENTS.override.md", "AGENTS.md")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_SECRET_ENVIRONMENT_NAME = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE|AUTH|PRIVATE|JWT|SESSION|"
    r"(?:^|_)KEY(?:_|$)|KEY$"
)
MAX_PROJECT_INSTRUCTION_BYTES = 64 * 1024
MAX_WORKSPACE_CONTEXT_BYTES = 12 * 1024
MAX_GIT_CONTEXT_BYTES = 16 * 1024
MAX_ENVIRONMENT_CONTEXT_BYTES = 4 * 1024
MAX_ENVIRONMENT_VALUE_BYTES = 1024


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
        observations = await self._observe_all(request, cancel)
        consistency = None
        if len(self._sources) > 1:
            workspace_scope = self._common_workspace_scope(observations)
            verified = await self._observe_all(request, cancel)
            if self._common_workspace_scope(verified) != workspace_scope:
                raise ContextSourceError(
                    "context_sources_changed",
                    "Context Source在组合观测期间发生变化",
                    retryable=True,
                )
            for previous, current in zip(observations, verified, strict=True):
                if previous.source_revision != current.source_revision:
                    raise ContextSourceError(
                        "context_sources_changed",
                        "Context Source在组合观测期间发生变化",
                        retryable=True,
                    )
                if previous != current:
                    raise ContextSourceError(
                        "context_source_invalid", "Context Source revision与观测正文不一致"
                    )
            observations = verified
            consistency = ContextConsistencySnapshot(
                source_count=len(self._sources), workspace_scope=workspace_scope
            )

        fragments: list[ContextFragment] = []
        snapshots: list[ContextSourceSnapshot] = []
        for source, observed in zip(self._sources, observations, strict=True):
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
            return self._engine.prepare_sourced(request, fragments, snapshots, consistency)
        except ValueError:
            raise ContextSourceError(
                "context_source_invalid", "Context Source 组合结果不符合规划契约"
            ) from None

    async def _observe_all(
        self, request: ContextBuildInput, cancel: CancelToken
    ) -> tuple[ContextSourceObservation, ...]:
        observations: list[ContextSourceObservation] = []
        for source in self._sources:
            cancel.checkpoint()
            observed = await source.observe(request, cancel)
            if not isinstance(observed, ContextSourceObservation):
                raise ContextSourceError("context_source_invalid", "Context Source 返回无效契约")
            observations.append(observed)
        return tuple(observations)

    @staticmethod
    def _common_workspace_scope(observations: Sequence[ContextSourceObservation]) -> str:
        scopes = {observation.workspace_scope for observation in observations}
        if len(scopes) != 1:
            raise ContextSourceError(
                "context_source_workspace_mismatch", "Context Source Workspace scope不一致"
            )
        return next(iter(scopes))


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


class WorkspaceContextSource:
    """提供受控Workspace根与工作目录的有界一级概览。"""

    source_id = _WORKSPACE_SOURCE_ID
    kind = ContextFragmentKind.WORKSPACE

    def __init__(
        self,
        root: Path,
        *,
        working_directory: str = ".",
        denied_paths: tuple[str, ...] = (),
        max_entries_per_directory: int = 64,
        max_content_bytes: int = MAX_WORKSPACE_CONTEXT_BYTES,
    ) -> None:
        if type(max_entries_per_directory) is not int or not 1 <= max_entries_per_directory <= 200:
            raise ValueError("Workspace Context每目录条目上限必须为1到200")
        if type(max_content_bytes) is not int or not 512 <= max_content_bytes <= 32_768:
            raise ValueError("Workspace Context正文上限必须为512到32768字节")
        self._root = root.resolve(strict=True)
        self._working_directory = working_directory
        self._denied_paths = denied_paths
        self._max_entries = max_entries_per_directory
        self._max_content_bytes = max_content_bytes

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
            raise _mapped_read_error(error, "Workspace Context") from None
        except OSError as error:
            raise _mapped_os_error(error, "Workspace Context") from None

    def _observe_sync(
        self, operation: ReadOperation, requested_workspace: str
    ) -> ContextSourceObservation:
        _require_bound_root(self._root, requested_workspace)
        with Workspace(self._root, denied_paths=self._denied_paths) as workspace:
            parts = workspace.parts(self._working_directory)
            working_directory = "." if not parts else "/".join(parts)
            paths = tuple(dict.fromkeys((".", working_directory)))
            first = tuple(
                files.list_files(
                    workspace,
                    ListFilesInput(path=path, limit=self._max_entries),
                    operation,
                )
                for path in paths
            )
            verified = tuple(
                files.list_files(
                    workspace,
                    ListFilesInput(
                        path=page.path,
                        limit=self._max_entries,
                        expected_revision=page.revision,
                    ),
                    operation,
                )
                for page in first
            )
            content = _workspace_content(working_directory, verified, self._max_content_bytes)
            revision = _content_revision(content)
            return ContextSourceObservation(
                workspace_scope=workspace.scope,
                source_revision=digest(
                    {
                        "contract": "workspace-context/v1",
                        "working_directory": working_directory,
                        "max_entries": self._max_entries,
                        "max_content_bytes": self._max_content_bytes,
                        "directories": [(page.path, page.revision) for page in verified],
                        "document_revision": revision,
                    }
                ),
                documents=(
                    ContextSourceDocument(
                        source="workspace/layout.json", content=content, revision=revision
                    ),
                ),
            )


class GitContextSource:
    """通过既有固定Git只读运行时提供每模型步骤刷新状态。"""

    source_id = _GIT_SOURCE_ID
    kind = ContextFragmentKind.GIT

    def __init__(
        self,
        root: Path,
        executable: Path,
        *,
        working_directory: str = ".",
        denied_paths: tuple[str, ...] = (),
        status_limit: int = 100,
        max_content_bytes: int = MAX_GIT_CONTEXT_BYTES,
    ) -> None:
        if type(status_limit) is not int or not 1 <= status_limit <= 200:
            raise ValueError("Git Context状态条目上限必须为1到200")
        if type(max_content_bytes) is not int or not 1024 <= max_content_bytes <= 32_768:
            raise ValueError("Git Context正文上限必须为1024到32768字节")
        self._root = root.resolve(strict=True)
        self._working_directory = working_directory
        self._denied_paths = denied_paths
        self._status_limit = status_limit
        self._max_content_bytes = max_content_bytes
        self._runtime = GitReadRuntime(self._root, executable)

    async def observe(
        self, request: ContextBuildInput, cancel: CancelToken
    ) -> ContextSourceObservation:
        try:
            before = await _workspace_binding(
                self._root,
                request.workspace,
                self._working_directory,
                self._denied_paths,
                cancel,
            )
            try:
                observed = await self._runtime.execute(
                    GitStatusInput(limit=self._status_limit), cancel
                )
            except ReadToolError as error:
                if error.code != "not_found":
                    raise
                observed = None
            after = await _workspace_binding(
                self._root,
                request.workspace,
                self._working_directory,
                self._denied_paths,
                cancel,
            )
            if before != after:
                raise ContextSourceError(
                    "context_source_unavailable",
                    "Git Context工作区在观测期间发生变化",
                    retryable=True,
                )
            workspace_scope, working_directory_revision = after
            if observed is not None and not isinstance(observed, GitStatusOutput):
                raise ContextSourceError("context_source_invalid", "Git Context运行时返回无效契约")
            if observed is not None:
                observed = await cancel.run(
                    run_read_operation(
                        lambda operation: _visible_git_status(
                            self._root, self._denied_paths, observed, operation
                        )
                    )
                )
            content = _git_content(observed, self._max_content_bytes)
            revision = _content_revision(content)
            return ContextSourceObservation(
                workspace_scope=workspace_scope,
                source_revision=digest(
                    {
                        "contract": "git-context/v1",
                        "runtime": self._runtime.contract(),
                        "working_directory": self._working_directory,
                        "working_directory_revision": working_directory_revision,
                        "status_revision": observed.revision if observed is not None else None,
                        "document_revision": revision,
                    }
                ),
                documents=(
                    ContextSourceDocument(
                        source="git/status.json", content=content, revision=revision
                    ),
                ),
            )
        except ReadToolError as error:
            raise _mapped_read_error(error, "Git Context") from None
        except KernelError as error:
            retryable = error.code in {
                "process_timeout",
                "process_launch_failed",
                "process_binding_changed",
            }
            raise ContextSourceError(
                "context_source_unavailable" if retryable else "context_source_invalid",
                "Git Context运行时不可用" if retryable else "Git Context运行时绑定无效",
                retryable=retryable,
            ) from None
        except OSError as error:
            raise _mapped_os_error(error, "Git Context") from None


class EnvironmentContextSource:
    """只投影宿主显式准入的非Secret环境事实。"""

    source_id = _ENVIRONMENT_SOURCE_ID
    kind = ContextFragmentKind.ENVIRONMENT

    def __init__(
        self,
        root: Path,
        *,
        values: Mapping[str, str] | None = None,
        allowlist: Sequence[str] = (),
        working_directory: str = ".",
        denied_paths: tuple[str, ...] = (),
        max_content_bytes: int = MAX_ENVIRONMENT_CONTEXT_BYTES,
    ) -> None:
        if values is not None and not isinstance(values, Mapping):
            raise ValueError("环境values必须是名称到文本的映射")
        if isinstance(allowlist, str):
            raise ValueError("环境allowlist必须是名称序列")
        names = tuple(allowlist)
        if len(names) > 32 or len(set(names)) != len(names):
            raise ValueError("环境allowlist必须唯一且不超过32项")
        if any(
            type(name) is not str
            or _ENVIRONMENT_NAME.fullmatch(name) is None
            or _SECRET_ENVIRONMENT_NAME.search(name) is not None
            for name in names
        ):
            raise ValueError("环境allowlist包含非法或Secret类名称")
        if type(max_content_bytes) is not int or not 512 <= max_content_bytes <= 4096:
            raise ValueError("环境Context正文上限必须为512到4096字节")
        self._root = root.resolve(strict=True)
        self._values = values if values is not None else {}
        self._allowlist = tuple(sorted(names))
        self._working_directory = working_directory
        self._denied_paths = denied_paths
        self._max_content_bytes = max_content_bytes

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
            raise _mapped_read_error(error, "环境Context") from None
        except OSError as error:
            raise _mapped_os_error(error, "环境Context") from None

    def _observe_sync(
        self, operation: ReadOperation, requested_workspace: str
    ) -> ContextSourceObservation:
        before = _workspace_binding_sync(
            self._root,
            requested_workspace,
            self._working_directory,
            self._denied_paths,
            operation,
        )
        variables: dict[str, str] = {}
        for name in self._allowlist:
            operation.checkpoint()
            try:
                value = self._values[name]
            except KeyError:
                continue
            except Exception:
                raise ContextSourceError(
                    "context_source_invalid", "环境Context映射读取失败"
                ) from None
            variables[name] = _validated_environment_value(value)
        after = _workspace_binding_sync(
            self._root,
            requested_workspace,
            self._working_directory,
            self._denied_paths,
            operation,
        )
        if before != after:
            raise ReadToolError("workspace_changed")
        workspace_scope, working_directory_revision = after
        content = _canonical_json(
            {
                "schema": "harnessix.environment-context/v1",
                "os": os.name,
                "platform": sys.platform,
                "working_directory": self._working_directory,
                "variables": variables,
            }
        )
        if len(content.encode()) > self._max_content_bytes:
            raise ReadToolError("limit_exceeded")
        revision = _content_revision(content)
        return ContextSourceObservation(
            workspace_scope=workspace_scope,
            source_revision=digest(
                {
                    "contract": "environment-context/v1",
                    "allowlist": self._allowlist,
                    "working_directory_revision": working_directory_revision,
                    "document_revision": revision,
                }
            ),
            documents=(
                ContextSourceDocument(
                    source="environment/runtime.json", content=content, revision=revision
                ),
            ),
        )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _content_revision(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _require_bound_root(root: Path, requested_workspace: str) -> None:
    try:
        requested_root = Path(requested_workspace).resolve(strict=True)
    except (OSError, ValueError):
        requested_root = None
    if requested_root != root:
        raise ContextSourceError(
            "context_source_workspace_mismatch", "Context Source与Thread工作区不匹配"
        )


async def _workspace_binding(
    root: Path,
    requested_workspace: str,
    working_directory: str,
    denied_paths: tuple[str, ...],
    cancel: CancelToken,
) -> tuple[str, str]:
    return await cancel.run(
        run_read_operation(
            lambda operation: _workspace_binding_sync(
                root,
                requested_workspace,
                working_directory,
                denied_paths,
                operation,
            )
        )
    )


def _workspace_binding_sync(
    root: Path,
    requested_workspace: str,
    working_directory: str,
    denied_paths: tuple[str, ...],
    operation: ReadOperation,
) -> tuple[str, str]:
    _require_bound_root(root, requested_workspace)
    with Workspace(root, denied_paths=denied_paths) as workspace:
        parts = workspace.parts(working_directory)
        normalized = "." if not parts else "/".join(parts)
        with workspace.open(normalized, operation, directory=True) as descriptor:
            revision = digest((workspace.scope, normalized, revision_state(os.fstat(descriptor))))
        return workspace.scope, revision


def _workspace_content(
    working_directory: str,
    pages: Sequence[ListFilesOutput],
    max_content_bytes: int,
) -> str:
    entries = [[entry.model_dump(mode="json") for entry in page.entries] for page in pages]
    truncated = [page.truncated for page in pages]
    while True:
        content = _canonical_json(
            {
                "schema": "harnessix.workspace-context/v1",
                "working_directory": working_directory,
                "directories": [
                    {
                        "path": page.path,
                        "entries": visible,
                        "truncated": truncated[index],
                    }
                    for index, (page, visible) in enumerate(zip(pages, entries, strict=True))
                ],
            }
        )
        if len(content.encode()) <= max_content_bytes:
            return content
        for index in range(len(entries) - 1, -1, -1):
            if entries[index]:
                entries[index].pop()
                truncated[index] = True
                break
        else:
            raise ReadToolError("limit_exceeded")


def _git_content(status: GitStatusOutput | None, max_content_bytes: int) -> str:
    if status is None:
        content = _canonical_json({"schema": "harnessix.git-context/v1", "repository": False})
        if len(content.encode()) > max_content_bytes:
            raise ReadToolError("limit_exceeded")
        return content
    entries = [entry.model_dump(mode="json") for entry in status.entries]
    while True:
        content = _canonical_json(
            {
                "schema": "harnessix.git-context/v1",
                "repository": True,
                "branch": status.branch,
                "head_oid": status.head_oid,
                "upstream": status.upstream,
                "ahead": status.ahead,
                "behind": status.behind,
                "entries": entries,
                "total_entries": status.total_entries,
                "truncated": status.truncated or len(entries) < len(status.entries),
            }
        )
        if len(content.encode()) <= max_content_bytes:
            return content
        if not entries:
            raise ReadToolError("limit_exceeded")
        entries.pop()


def _visible_git_status(
    root: Path,
    denied_paths: tuple[str, ...],
    status: GitStatusOutput,
    operation: ReadOperation,
) -> GitStatusOutput:
    visible = []
    with Workspace(root, denied_paths=denied_paths) as workspace:
        for entry in status.entries:
            operation.checkpoint()
            try:
                workspace.parts(entry.path)
                if entry.original_path is not None:
                    workspace.parts(entry.original_path)
            except ReadToolError:
                continue
            visible.append(entry)
    return status.model_copy(
        update={
            "entries": tuple(visible),
            "truncated": status.truncated or len(visible) < len(status.entries),
        }
    )


def _validated_environment_value(value: object) -> str:
    if (
        type(value) is not str
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ContextSourceError("context_source_invalid", "环境Context值不符合文本契约")
    try:
        encoded = value.encode()
    except UnicodeEncodeError:
        raise ContextSourceError("context_source_invalid", "环境Context值不是有效UTF-8") from None
    if len(encoded) > MAX_ENVIRONMENT_VALUE_BYTES:
        raise ContextSourceError("context_source_too_large", "环境Context单值超过读取上限")
    return value


def _mapped_read_error(error: ReadToolError, label: str) -> ContextSourceError:
    if error.code in {"limit_exceeded", "offset_out_of_range"}:
        return ContextSourceError("context_source_too_large", f"{label}超过受控读取上限")
    if error.code in {"timeout", "workspace_changed", "page_changed", "io_failed"}:
        return ContextSourceError(
            "context_source_unavailable", f"{label}暂时不可用", retryable=True
        )
    return ContextSourceError("context_source_invalid", f"{label}不符合安全读取契约")


def _mapped_os_error(error: OSError, label: str) -> ContextSourceError:
    retryable = (error.errno or 0) not in {errno.EACCES, errno.EPERM}
    return ContextSourceError(
        "context_source_unavailable",
        f"{label}暂时不可用",
        retryable=retryable,
    )
