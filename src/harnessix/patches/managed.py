"""宿主专用、一次性批准的私有工作副本写执行；尚不注册模型工具。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalDecision
from harnessix.patches import batch_ledger, ledger, ledger_migrations
from harnessix.patches.contracts import PreparedPatch
from harnessix.patches.managed_contracts import (
    MAX_COPY_BYTES,
    MAX_COPY_FILES,
    CopyFile,
    CopyManifest,
    PatchRecord,
    PatchState,
)
from harnessix.patches.managed_io import (
    FILE_FLAGS,
    create_file,
    fail,
    import_file,
    plain_metadata,
    private,
    snapshot,
    writable_target,
    write_all,
    write_parent,
)
from harnessix.patches.planner import validate_prepared, verify_prepared
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.workspace import ReadOperation, Workspace, digest, identity


def _fault(point: str) -> None:
    """仅用于测试的持久性切点；生产不注入行为。"""


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except ReadToolError as error:
        raise fail(error.code) from None
    except (OSError, sqlite3.Error):
        raise fail("storage_unavailable") from None


def _metadata(db: sqlite3.Connection, value: dict[str, object]) -> None:
    payload = json.dumps({**value, "checksum": digest(value)})
    db.execute("UPDATE metadata SET payload=? WHERE id=1", (payload,))


class PatchWorkspaces:
    """工厂只创建/重开自身登记的副本，管理根必须位于源工作区之外。"""

    def __init__(self, private_root: Path) -> None:
        with _errors():
            private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.root = private_root.resolve(strict=True)
            with Workspace(self.root) as root:
                fd = root._current_root()
                try:
                    private(os.fstat(fd), directory=True)
                    plain_metadata(fd)
                finally:
                    os.close(fd)

    def create(
        self, source: Workspace, paths: Sequence[str], operation: ReadOperation
    ) -> ManagedPatchWorkspace:
        with _errors(), ExitStack() as stack:
            operation.checkpoint()
            if self.root.is_relative_to(source.root) or source.root.is_relative_to(self.root):
                raise fail("source_overlap")
            if not 1 <= len(paths) <= MAX_COPY_FILES or len(set(paths)) != len(paths):
                raise fail("limit_exceeded")
            for path in paths:
                if not source.parts(path):
                    raise fail("path_denied")
            root = stack.enter_context(Workspace(self.root))
            root_fd = root._current_root()
            stack.callback(os.close, root_fd)
            private(os.fstat(root_fd), directory=True)
            plain_metadata(root_fd)
            workspace_id = uuid4()
            os.mkdir(str(workspace_id), 0o700, dir_fd=root_fd)
            os.fsync(root_fd)
            bundle = stack.enter_context(Workspace(self.root / str(workspace_id)))
            bundle_fd = bundle._current_root()
            stack.callback(os.close, bundle_fd)
            for name in ("owner.lock", "ledger.sqlite"):
                file_fd = create_file(bundle_fd, name)
                try:
                    os.fsync(file_fd)
                finally:
                    os.close(file_fd)
            os.mkdir("workspace", 0o700, dir_fd=bundle_fd)
            os.fsync(bundle_fd)
            copy = stack.enter_context(Workspace(bundle.root / "workspace"))
            db = sqlite3.connect(bundle.root / "ledger.sqlite", isolation_level=None)
            stack.callback(db.close)
            db.execute("PRAGMA synchronous=FULL")
            metadata: dict[str, object] = {
                "state": "building",
                "workspace_id": str(workspace_id),
                "root": identity(os.fstat(root_fd)),
                "bundle": identity(os.fstat(bundle_fd)),
                "workspace_scope": copy.scope,
                "lock": identity(os.stat("owner.lock", dir_fd=bundle_fd, follow_symlinks=False)),
                "database": identity(
                    os.stat("ledger.sqlite", dir_fd=bundle_fd, follow_symlinks=False)
                ),
                "manifest": None,
            }
            ledger.initialize(db, metadata)
            _metadata(db, metadata)
            _fault("copy_building")
            files: list[CopyFile] = []
            size = 0
            for path in sorted(paths):
                operation.checkpoint()
                body, revision, mode = snapshot(source, path, operation)
                size += len(body)
                if size > MAX_COPY_BYTES:
                    raise fail("limit_exceeded")
                import_file(copy, path, body, mode, operation)
                db.execute("INSERT INTO baseline VALUES(?,?)", (path, body))
                files.append(
                    CopyFile(
                        path=path,
                        source_revision=revision,
                        sha256=hashlib.sha256(body).hexdigest(),
                        size_bytes=len(body),
                        mode=mode,
                    )
                )
            manifest = CopyManifest(
                workspace_id=workspace_id,
                source_scope=source.scope,
                workspace_scope=copy.scope,
                files=tuple(files),
            )
            operation.checkpoint()
            _fault("copy_before_ready")
            _metadata(
                db, {**metadata, "state": "ready", "manifest": manifest.model_dump(mode="json")}
            )
            os.fsync(bundle_fd)
        return self.open(workspace_id)

    def open(self, workspace_id: UUID) -> ManagedPatchWorkspace:
        if type(workspace_id) is not UUID:
            raise fail("invalid_workspace")
        with _errors():
            return ManagedPatchWorkspace(self.root, workspace_id)


class ManagedPatchWorkspace:
    def __init__(self, root: Path, workspace_id: UUID) -> None:
        self._mutex = RLock()
        self._closed = False
        self.workspace_id = workspace_id
        with ExitStack() as stack:
            self._root = stack.enter_context(Workspace(root))
            self._bundle = stack.enter_context(Workspace(root / str(workspace_id)))
            self._bundle_fd = self._bundle._current_root()
            stack.callback(os.close, self._bundle_fd)
            private(os.fstat(self._bundle_fd), directory=True)
            plain_metadata(self._bundle_fd)
            self._lock_fd = os.open("owner.lock", FILE_FLAGS, dir_fd=self._bundle_fd)
            stack.callback(os.close, self._lock_fd)
            private(os.fstat(self._lock_fd), directory=False)
            plain_metadata(self._lock_fd)
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise fail("workspace_busy") from None
            self._db_fd = os.open("ledger.sqlite", FILE_FLAGS, dir_fd=self._bundle_fd)
            stack.callback(os.close, self._db_fd)
            private(os.fstat(self._db_fd), directory=False)
            plain_metadata(self._db_fd)
            self.workspace = stack.enter_context(Workspace(self._bundle.root / "workspace"))
            self._db = sqlite3.connect(
                self._bundle.root / "ledger.sqlite",
                isolation_level=None,
                check_same_thread=False,
                timeout=0,
            )
            stack.callback(self._db.close)
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            application_id = self._db.execute("PRAGMA application_id").fetchone()[0]
            if application_id != ledger.APPLICATION_ID or version not in {1, ledger.SCHEMA_VERSION}:
                raise fail("wrong_database")
            try:
                rows = self._db.execute("SELECT payload FROM metadata LIMIT 2").fetchall()
                if len(rows) != 1 or len(rows[0][0]) > 256 * 1024:
                    raise ValueError
                metadata = json.loads(rows[0][0])
                if metadata.pop("checksum") != digest(metadata):
                    raise ValueError
                self._identity_metadata = metadata
                if metadata["state"] != "ready":
                    raise fail("copy_not_ready")
                self.manifest = CopyManifest.model_validate_json(json.dumps(metadata["manifest"]))
                if (
                    self.manifest.workspace_id != workspace_id
                    or self.manifest.workspace_scope != self.workspace.scope
                ):
                    raise ValueError
                self._validate()
                baseline = self._db.execute(
                    "SELECT path,body FROM baseline ORDER BY path LIMIT 257"
                ).fetchall()
                if len(baseline) != len(self.manifest.files):
                    raise ValueError
                for (path, body), entry in zip(baseline, self.manifest.files, strict=True):
                    if (
                        path != entry.path
                        or len(body) != entry.size_bytes
                        or hashlib.sha256(body).hexdigest() != entry.sha256
                    ):
                        raise ValueError
            except (KeyError, ValueError, TypeError):
                raise fail("ledger_corrupt") from None
            if version == 1:
                with ledger.transaction(self._db):
                    ledger.capacity(self._db, 0, 0)
                    plans = self._db.execute("SELECT id FROM plans LIMIT 65").fetchall()
                    if len(plans) > 64:
                        raise fail("ledger_corrupt")
                    operation = ReadOperation()
                    for (plan_id,) in plans:
                        try:
                            parsed = UUID(plan_id)
                        except (ValueError, TypeError, AttributeError):
                            raise fail("ledger_corrupt") from None
                        self._load(parsed, operation)
                    ledger_migrations.add_batches(self._db)
                ledger_migrations._fault("migration_committed")
            self._resources = stack.pop_all()

    def _validate(self) -> None:
        if self._closed:
            raise fail("workspace_closed")
        metadata = self._identity_metadata
        for workspace, key in ((self._root, "root"), (self._bundle, "bundle")):
            fd = workspace._current_root()
            try:
                private(os.fstat(fd), directory=True)
                plain_metadata(fd)
                if list(identity(os.fstat(fd))) != metadata[key]:
                    raise fail("workspace_changed")
            finally:
                os.close(fd)
        fd = self.workspace._current_root()
        try:
            private(os.fstat(fd), directory=True)
            plain_metadata(fd)
        finally:
            os.close(fd)
        if (
            metadata["workspace_id"] != str(self.workspace_id)
            or metadata["workspace_scope"] != self.workspace.scope
        ):
            raise fail("workspace_changed")
        for name, fd, key in (
            ("owner.lock", self._lock_fd, "lock"),
            ("ledger.sqlite", self._db_fd, "database"),
        ):
            info = os.stat(name, dir_fd=self._bundle_fd, follow_symlinks=False)
            private(info, directory=False)
            plain_metadata(fd)
            if identity(info) != identity(os.fstat(fd)) or list(identity(info)) != metadata[key]:
                raise fail("workspace_changed")

    @contextmanager
    def _guard(self) -> Iterator[None]:
        with self._mutex, _errors():
            self._validate()
            yield

    def _load(
        self, plan_id: UUID, operation: ReadOperation
    ) -> tuple[PatchRecord, PreparedPatch, tuple[int, int] | None]:
        result = ledger.load(self._db, self.workspace, self.workspace_id, plan_id, operation)
        if result[0].manifest.path not in {entry.path for entry in self.manifest.files}:
            raise fail("path_denied")
        return result

    def save(
        self, prepared: PreparedPatch, request_id: str, operation: ReadOperation
    ) -> PatchRecord:
        with self._guard():
            # 载荷复核先于幂等命中；已应用计划重试保存不要求旧前镜像仍存在。
            validate_prepared(self.workspace, prepared, operation)
            row = self._db.execute(
                "SELECT id FROM plans WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is not None:
                batch_ledger.require_single(self._db, UUID(row[0]))
                record, existing, _ = self._load(UUID(row[0]), operation)
                if existing != prepared:
                    raise fail("request_conflict")
                return record
            if prepared.manifest.path not in {entry.path for entry in self.manifest.files}:
                raise fail("path_denied")
            verify_prepared(self.workspace, prepared, operation)
            record = PatchRecord(
                plan_id=uuid4(),
                workspace_id=self.workspace_id,
                request_id=request_id,
                manifest=prepared.manifest,
                approval_fingerprint="0" * 64,
                state="pending",
            )
            record = record.model_copy(update={"approval_fingerprint": ledger.fingerprint(record)})
            ledger.save(self._db, record, prepared)
            return record

    def get(self, plan_id: UUID) -> PatchRecord:
        with self._guard():
            return self._load(plan_id, ReadOperation())[0]

    def lookup(self, request_id: str, operation: ReadOperation) -> PatchRecord | None:
        """按稳定请求加载已有计划；缺失不隐式准备或创建。"""
        with self._guard():
            operation.checkpoint()
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                raise fail("invalid_request")
            row = self._db.execute(
                "SELECT id FROM plans WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                return None
            try:
                plan_id = UUID(row[0])
            except (ValueError, TypeError, AttributeError):
                raise fail("ledger_corrupt") from None
            return self._load(plan_id, operation)[0]

    def verify(self, plan_id: UUID, operation: ReadOperation) -> PatchRecord:
        """复核保存的完整计划和当前前镜像，不改变审批或执行状态。"""
        with self._guard():
            record, prepared, _ = self._load(plan_id, operation)
            verify_prepared(self.workspace, prepared, operation)
            return record

    def reply(
        self, plan_id: UUID, approval_fingerprint: str, decision: ApprovalDecision
    ) -> PatchRecord:
        with self._guard():
            batch_ledger.require_single(self._db, plan_id)
            record, _, temporary = self._load(plan_id, ReadOperation())
            self._authorize(record, approval_fingerprint)
            decision = ApprovalDecision.model_validate_json(decision.model_dump_json())
            if record.decision is not None:
                if record.decision != decision:
                    raise fail("approval_conflict")
                return record
            return self._append(record, decision.outcome.value, temporary, decision=decision)

    @staticmethod
    def _authorize(record: PatchRecord, approval_fingerprint: str) -> None:
        if record.approval_fingerprint != approval_fingerprint:
            raise fail("approval_mismatch")

    def _append(
        self,
        record: PatchRecord,
        state: str,
        temporary: tuple[int, int] | None,
        *,
        decision: ApprovalDecision | None = None,
        error_code: str | None = None,
    ) -> PatchRecord:
        result = PatchRecord.model_validate_json(
            record.model_copy(
                update={
                    "state": state,
                    "decision": decision or record.decision,
                    "error_code": error_code,
                }
            ).model_dump_json()
        )
        with ledger.transaction(self._db):
            ledger.append(self._db, result, temporary)
        return result

    def execute(
        self, plan_id: UUID, approval_fingerprint: str, operation: ReadOperation
    ) -> PatchRecord:
        with self._guard():
            batch_ledger.require_single(self._db, plan_id)
            record, prepared, _ = self._load(plan_id, operation)
            self._authorize(record, approval_fingerprint)
            if record.state != "approved":
                raise fail("not_executable")
            verify_prepared(self.workspace, prepared, operation)
            writable_target(self.workspace, record.manifest.path, operation)
            record = self._append(record, "started", None)
            attempted = False
            temporary: tuple[int, int] | None = None
            temp_name = f"{plan_id}.patch"
            try:
                _fault("started")
                with write_parent(self.workspace, record.manifest.path) as parent:
                    if os.fstat(parent.fd).st_dev != os.fstat(self._bundle_fd).st_dev:
                        raise fail("cross_device")
                    fd = create_file(self._bundle_fd, temp_name)
                    try:
                        temporary = identity(os.fstat(fd))
                        _fault("temp_created")
                        write_all(fd, prepared.after, operation)
                        os.fchmod(fd, record.manifest.source_mode)
                        plain_metadata(fd)
                        os.fsync(fd)
                        _fault("temp_synced")
                        # 临时 inode 归因证据先于 rename 持久化。
                        record = self._append(record, "started", temporary)
                        _fault("temp_recorded")
                        _fault("before_replace")
                        operation.checkpoint()
                        # 最终核对持有的目录链、当前前镜像与写准入条件。
                        parent.verify()
                        verify_prepared(self.workspace, prepared, operation)
                        writable_target(self.workspace, record.manifest.path, operation)
                        self._validate()
                        attempted = True
                        os.replace(
                            temp_name,
                            self.workspace.parts(record.manifest.path)[-1],
                            src_dir_fd=self._bundle_fd,
                            dst_dir_fd=parent.fd,
                        )
                        _fault("after_replace")
                        # 此后不响应调用方取消，先完成已发起效果的落盘与记账。
                        os.fsync(fd)
                        os.fsync(parent.fd)
                        os.fsync(self._bundle_fd)
                        _fault("directories_synced")
                        parent.verify()
                        self._validate()
                        if self._observe(prepared, temporary, ReadOperation()) != "observed_after":
                            raise fail("postimage_unverified")
                    finally:
                        os.close(fd)
                _fault("before_result")
                result = self._append(record, "applied", temporary)
            except BaseException as error:
                code = (
                    error.code
                    if isinstance(error, (KernelError, ReadToolError))
                    else "cancelled"
                    if isinstance(error, TurnCancelled)
                    else "patch_execution_failed"
                )
                # 未持久化临时证据时不能在结果中凭空增加证据；恢复仅依赖已落库事实。
                persisted, _, persisted_temporary = self._load(plan_id, ReadOperation())
                if persisted.state == "applied":
                    if not isinstance(error, (KernelError, OSError, sqlite3.Error)):
                        raise
                    return persisted
                result = self._append(
                    persisted,
                    "uncertain" if attempted else "failed",
                    persisted_temporary,
                    error_code=code,
                )
                if not isinstance(
                    error, (KernelError, ReadToolError, OSError, sqlite3.Error, TurnCancelled)
                ):
                    raise
            finally:
                if temporary is not None:
                    self._cleanup(temp_name, temporary)
            _fault("result_recorded")
            return result

    def _cleanup(self, name: str, temporary: tuple[int, int]) -> None:
        try:
            if identity(os.stat(name, dir_fd=self._bundle_fd, follow_symlinks=False)) == temporary:
                os.unlink(name, dir_fd=self._bundle_fd)
                os.fsync(self._bundle_fd)
        except OSError:
            pass  # 私有临时残留可检查；不能把它当作目标文件回滚。

    def _observe(
        self, prepared: PreparedPatch, temporary: tuple[int, int] | None, operation: ReadOperation
    ) -> PatchState:
        path, manifest = prepared.manifest.path, prepared.manifest
        try:
            with write_parent(self.workspace, path):
                body, revision, mode = snapshot(self.workspace, path, operation)
                target_identity = writable_target(self.workspace, path, operation)
            if (
                body == prepared.before
                and revision == manifest.source_revision
                and mode == manifest.source_mode
            ):
                return "observed_before"
            if body == prepared.after and mode == manifest.source_mode:
                return "observed_after" if target_identity == temporary else "uncertain"
            return "diverged"
        except (KernelError, ReadToolError, OSError) as error:
            if isinstance(error, FileNotFoundError) or (
                isinstance(error, KernelError) and error.code == "patch_not_found"
            ):
                return "missing"
            if isinstance(error, (KernelError, ReadToolError)) and error.code in {
                "timeout",
                "patch_timeout",
            }:
                raise
            return "unavailable"

    def reconcile(self, plan_id: UUID, operation: ReadOperation) -> PatchRecord:
        with self._guard():
            record, prepared, temporary = self._load(plan_id, operation)
            if record.state not in {"started", "uncertain"}:
                return record
            observed = self._observe(prepared, temporary, operation)
            if observed == "uncertain" and record.state == "uncertain":
                return record
            return self._append(record, observed, temporary)

    def close(self) -> None:
        with self._mutex:
            if not self._closed:
                self._closed = True
                self._resources.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
