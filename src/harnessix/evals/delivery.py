"""严格通过的Coding Eval结果到显式目标仓库的单文件受控交付。"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import stat
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalOutcome, ApprovalRecord
from harnessix.evals.catalog import HistoricalCodingEval
from harnessix.evals.delivery_contracts import (
    MAX_CHANGE_IMAGE_BYTES,
    CodingEvalChangePackage,
    CodingEvalDeliveryPlan,
    CodingEvalDeliveryRecord,
    DeliveryStatus,
    change_image,
    transition,
)
from harnessix.evals.git_evidence import collect_git_evidence
from harnessix.evals.materializer import load_materialized_coding_eval
from harnessix.evals.report import eval_report_sha256, read_eval_report
from harnessix.evals.run_state import read_eval_run_state
from harnessix.patches.managed import PatchWorkspaces
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.workspace import ReadOperation, Workspace, digest, identity

MAX_CHANGE_PACKAGE_BYTES = 3 * 1024 * 1024
MAX_DELIVERY_RECORD_BYTES = 512 * 1024
_GIT_OUTPUT_BYTES = 16 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 30
_FILE_FLAGS = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_GIT_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_LITERAL_PATHSPECS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}
_GIT_GLOBAL_ARGUMENTS = (
    "--no-pager",
    "--no-optional-locks",
    "-c",
    "color.ui=false",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
)


def _fault(_: str) -> None:
    """仅供崩溃边界测试替换；生产不注入行为。"""


@contextmanager
def _delivery_errors() -> Iterator[None]:
    try:
        yield
    except ReadToolError:
        raise KernelError("eval_delivery_target_changed", "交付目标路径已变化") from None
    except OSError:
        raise KernelError("eval_delivery_storage_unavailable", "交付存储操作失败") from None


def _private_directory(path: Path, *, create: bool = False) -> Path:
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = path.resolve(strict=True)
        lexical = path.lstat()
        info = resolved.stat()
    except (OSError, RuntimeError):
        raise KernelError("eval_delivery_root_invalid", "交付状态根目录无效") from None
    if (
        stat.S_ISLNK(lexical.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise KernelError("eval_delivery_root_invalid", "交付状态根目录必须是当前用户0700目录")
    return resolved


def _git_executable(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except (OSError, RuntimeError):
        raise KernelError("eval_delivery_git_invalid", "交付Git绑定无效") from None
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        raise KernelError("eval_delivery_git_invalid", "交付Git绑定无效")
    return resolved


def _git(
    executable: Path,
    root: Path,
    arguments: tuple[str, ...],
    *,
    max_bytes: int = _GIT_OUTPUT_BYTES,
) -> bytes:
    try:
        result = subprocess.run(
            (str(executable), *_GIT_GLOBAL_ARGUMENTS, *arguments),
            cwd=root,
            env=_GIT_ENVIRONMENT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        raise KernelError("eval_delivery_git_failed", "交付Git固定命令执行失败") from None
    if result.returncode != 0 or len(result.stdout) > max_bytes:
        raise KernelError("eval_delivery_git_failed", "交付Git固定命令执行失败")
    return result.stdout


def _git_text(executable: Path, root: Path, arguments: tuple[str, ...]) -> str:
    try:
        return (
            _git(executable, root, arguments, max_bytes=4096)
            .decode("utf-8", errors="strict")
            .removesuffix("\n")
        )
    except UnicodeError:
        raise KernelError("eval_delivery_git_invalid", "交付Git元数据不是合法UTF-8") from None


@dataclass(frozen=True, slots=True)
class _RepositorySnapshot:
    """仅供模块内部按字段命名读取，避免把目标绝对路径写入契约。"""

    head: str
    tree_oid: str
    tree_sha256: str
    origin: str
    status: bytes


def _repository_snapshot(executable: Path, root: Path) -> _RepositorySnapshot:
    top = _git_text(executable, root, ("rev-parse", "--show-toplevel"))
    if top != str(root):
        raise KernelError("eval_delivery_target_not_root", "交付目标必须是精确Git仓库根")
    head = _git_text(executable, root, ("rev-parse", "--verify", "HEAD^{commit}"))
    tree_oid = _git_text(executable, root, ("rev-parse", "--verify", "HEAD^{tree}"))
    tree = _git(executable, root, ("ls-tree", "-r", "-z", "--full-tree", "HEAD"))
    origin = _git_text(executable, root, ("remote", "get-url", "origin"))
    status_body = _git(
        executable,
        root,
        ("status", "--porcelain=v2", "--untracked-files=all", "-z"),
        max_bytes=4 * 1024 * 1024,
    )
    return _RepositorySnapshot(
        head=head,
        tree_oid=tree_oid,
        tree_sha256=hashlib.sha256(tree).hexdigest(),
        origin=origin,
        status=status_body,
    )


def _require_repository(
    package: CodingEvalChangePackage,
    plan: CodingEvalDeliveryPlan | None,
    root: Path,
    git: Path,
    *,
    allowed_statuses: tuple[bytes, ...] | None = (b"",),
) -> _RepositorySnapshot:
    snapshot = _repository_snapshot(git, root)
    repository = package.repository
    if snapshot.origin != repository.origin:
        raise KernelError("eval_delivery_origin_mismatch", "交付目标仓库来源不匹配")
    if (
        snapshot.head != repository.source_revision
        or snapshot.tree_oid != package.source_tree_oid
        or snapshot.tree_sha256 != repository.baseline_tree_sha256
    ):
        raise KernelError("eval_delivery_source_drift", "交付目标HEAD或基线树已漂移")
    if plan is not None:
        with Workspace(root) as workspace:
            scope = workspace.scope
        if (
            plan.package_fingerprint != package.package_fingerprint
            or plan.target_scope != scope
            or plan.target_repository != repository
            or plan.target_tree_oid != package.source_tree_oid
            or plan.path != package.path
            or plan.source_mode != package.source_mode
            or plan.before_sha256 != package.before.sha256
            or plan.after_sha256 != package.after.sha256
        ):
            raise KernelError("eval_delivery_plan_mismatch", "交付计划与目标或变更包不一致")
    if allowed_statuses is not None and snapshot.status not in allowed_statuses:
        raise KernelError("eval_delivery_target_dirty", "交付目标工作区或暂存区不干净")
    return snapshot


def _read_image(root: Path, path: str) -> tuple[bytes, int, tuple[int, int]]:
    operation = ReadOperation()
    try:
        with Workspace(root) as workspace, workspace.open(path, operation, directory=False) as fd:
            info = os.fstat(fd)
            if info.st_size > MAX_CHANGE_IMAGE_BYTES:
                raise KernelError("eval_delivery_file_too_large", "交付文件超过单文件上限")
            chunks: list[bytes] = []
            remaining = MAX_CHANGE_IMAGE_BYTES + 1
            while remaining:
                operation.checkpoint()
                chunk = os.read(fd, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            body = b"".join(chunks)
            if len(body) != info.st_size or len(body) > MAX_CHANGE_IMAGE_BYTES:
                raise KernelError("eval_delivery_file_too_large", "交付文件超过单文件上限")
            try:
                body.decode("utf-8", errors="strict")
            except UnicodeError:
                raise KernelError("eval_delivery_file_invalid", "交付文件不是合法UTF-8") from None
            mode = stat.S_IMODE(info.st_mode)
            if mode not in {0o644, 0o755}:
                raise KernelError("eval_delivery_file_invalid", "交付文件权限不受支持")
            return body, mode, identity(info)
    except KernelError:
        raise
    except (OSError, ReadToolError):
        raise KernelError(
            "eval_delivery_target_changed", "交付文件缺失、类型不受支持或读取期间变化"
        ) from None


def _new_package(**fields: Any) -> CodingEvalChangePackage:
    candidate = CodingEvalChangePackage.model_construct(
        _fields_set=None, **fields, package_fingerprint="0" * 64
    )
    fingerprint = digest(candidate.model_dump(mode="json", exclude={"package_fingerprint"}))
    return CodingEvalChangePackage.model_validate(
        {**fields, "package_fingerprint": fingerprint}, strict=True
    )


def _new_plan(**fields: Any) -> CodingEvalDeliveryPlan:
    candidate = CodingEvalDeliveryPlan.model_construct(
        _fields_set=None, **fields, approval_fingerprint="0" * 64
    )
    fingerprint = digest(candidate.model_dump(mode="json", exclude={"approval_fingerprint"}))
    return CodingEvalDeliveryPlan.model_validate(
        {**fields, "approval_fingerprint": fingerprint}, strict=True
    )


def _new_record(**fields: Any) -> CodingEvalDeliveryRecord:
    candidate = CodingEvalDeliveryRecord.model_construct(
        _fields_set=None, **fields, record_fingerprint="0" * 64
    )
    fingerprint = digest(candidate.model_dump(mode="json", exclude={"record_fingerprint"}))
    return CodingEvalDeliveryRecord.model_validate(
        {**fields, "record_fingerprint": fingerprint}, strict=True
    )


async def build_coding_eval_change_package(
    runs_root: Path,
    git_executable: Path,
    definition: HistoricalCodingEval,
    run_id: UUID,
) -> CodingEvalChangePackage:
    """从completed且passed的真实运行重算报告、Git和前后镜像证据。"""

    git = _git_executable(git_executable)
    materialized = load_materialized_coding_eval(runs_root, git, definition, run_id)
    state = read_eval_run_state(materialized.run_root / "run-state.json")
    report = read_eval_report(materialized.run_root / "report.json")
    task = definition.task
    if (
        state.status != "completed"
        or state.run_id != run_id
        or state.task_id != task.task_id
        or state.task_version != task.task_version
        or state.task_fingerprint != task.fingerprint
        or state.report_sha256 != eval_report_sha256(report)
        or report.run_id != run_id
        or report.task_id != task.task_id
        or report.task_version != task.task_version
        or report.task_fingerprint != task.fingerprint
        or report.environment != state.environment
        or report.started_at != state.started_at
        or report.git.baseline_revision != state.baseline_revision
        or report.git.baseline_tree_sha256 != state.baseline_tree_sha256
        or report.outcome != "passed"
        or any(not check.passed for check in report.checks)
    ):
        raise KernelError("eval_change_package_not_passed", "只有证据完整的严格通过运行可交付")
    if (
        len(report.git.changed_paths) != 1
        or report.git.staged_paths
        or report.git.untracked_paths
        or report.git.unsupported_change_paths
        or report.git.changed_paths[0] not in task.allowed_changed_paths
        or report.final_answer is None
        or not report.final_answer.parsed
        or report.final_answer.changed_paths != report.git.changed_paths
    ):
        raise KernelError(
            "eval_change_package_unsupported", "当前交付切片只接受一个允许的已有普通文件"
        )

    factory = PatchWorkspaces(materialized.run_root / "managed")
    copy = factory.open(state.execution_workspace_id)
    try:
        evidence = await collect_git_evidence(
            copy.workspace.root,
            git,
            baseline_revision=state.baseline_revision,
            baseline_tree_sha256=state.baseline_tree_sha256,
        )
        if evidence != report.git:
            raise KernelError(
                "eval_change_package_workspace_drift", "Eval工作区与已评分Git证据不一致"
            )
        path = report.git.changed_paths[0]
        before, before_mode, _ = _read_image(materialized.workspace, path)
        after, after_mode, _ = _read_image(copy.workspace.root, path)
        manifest = next((item for item in copy.manifest.files if item.path == path), None)
        if (
            manifest is None
            or before_mode != manifest.mode
            or after_mode != before_mode
            or manifest.sha256 != hashlib.sha256(before).hexdigest()
            or before == after
        ):
            raise KernelError(
                "eval_change_package_workspace_drift", "Eval前后镜像与受管副本证据不一致"
            )
    finally:
        copy.close()

    return _new_package(
        run_id=run_id,
        task_id=task.task_id,
        task_version=task.task_version,
        task_fingerprint=task.fingerprint,
        report_sha256=state.report_sha256,
        repository=task.repository,
        source_tree_oid=materialized.manifest.source_tree_oid,
        path=path,
        source_mode=before_mode,
        before=change_image(before),
        after=change_image(after),
        workspace_diff_sha256=report.git.diff_sha256,
        created_at=report.completed_at,
    )


def _write_private(path: Path, body: bytes, maximum: int, code: str) -> None:
    if not 1 <= len(body) <= maximum:
        raise KernelError(code, "交付私有文件为空或超过上限")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        parent = path.parent.resolve(strict=True)
        if path.is_symlink():
            raise OSError
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        remaining = memoryview(body)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        directory = os.open(parent, _DIRECTORY_FLAGS)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise KernelError(code, "交付私有文件写入失败") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink()
        except OSError:
            pass


def _read_private(path: Path, maximum: int, code: str) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or not 1 <= info.st_size <= maximum
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) != info.st_size:
            raise OSError
        return body
    except OSError:
        raise KernelError(code, "交付私有文件缺失、损坏或权限无效") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def write_coding_eval_change_package(path: Path, package: CodingEvalChangePackage) -> None:
    """原子持久化受控编码变更包。"""
    package = CodingEvalChangePackage.model_validate_json(package.model_dump_json(), strict=True)
    _write_private(
        path,
        (package.model_dump_json(indent=2) + "\n").encode("utf-8"),
        MAX_CHANGE_PACKAGE_BYTES,
        "eval_change_package_write_failed",
    )


def read_coding_eval_change_package(path: Path) -> CodingEvalChangePackage:
    """读取并验证受控编码变更包及其完整性摘要。"""
    try:
        return CodingEvalChangePackage.model_validate_json(
            _read_private(path, MAX_CHANGE_PACKAGE_BYTES, "eval_change_package_invalid"),
            strict=True,
        )
    except (ValidationError, ValueError):
        raise KernelError("eval_change_package_invalid", "交付变更包无效") from None


def _write_record(path: Path, record: CodingEvalDeliveryRecord) -> None:
    record = CodingEvalDeliveryRecord.model_validate_json(record.model_dump_json(), strict=True)
    _write_private(
        path,
        (record.model_dump_json(indent=2) + "\n").encode("utf-8"),
        MAX_DELIVERY_RECORD_BYTES,
        "eval_delivery_state_write_failed",
    )


def _read_record(path: Path) -> CodingEvalDeliveryRecord:
    try:
        return CodingEvalDeliveryRecord.model_validate_json(
            _read_private(path, MAX_DELIVERY_RECORD_BYTES, "eval_delivery_state_invalid"),
            strict=True,
        )
    except (ValidationError, ValueError):
        raise KernelError("eval_delivery_state_invalid", "交付状态无效") from None


class _TargetParent:
    def __init__(
        self,
        workspace: Workspace,
        fd: int,
        links: list[tuple[int, str, int]],
        name: str,
    ) -> None:
        self.workspace = workspace
        self.fd = fd
        self.links = links
        self.name = name

    def verify(self) -> None:
        current = self.workspace._current_root()
        os.close(current)
        for parent, name, child in self.links:
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            Workspace._check_type(before, directory=True)
            if identity(before) != identity(os.fstat(child)):
                raise KernelError("eval_delivery_target_changed", "交付目标目录链已变化")


@contextmanager
def _target_parent(workspace: Workspace, path: str) -> Iterator[_TargetParent]:
    with ExitStack() as stack:
        parts = workspace.parts(path)
        fd = workspace._current_root()
        stack.callback(os.close, fd)
        links: list[tuple[int, str, int]] = []
        for name in parts[:-1]:
            parent = fd
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            Workspace._check_type(before, directory=True)
            fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
            stack.callback(os.close, fd)
            if identity(before) != identity(os.fstat(fd)):
                raise KernelError("eval_delivery_target_changed", "交付目标目录链已变化")
            links.append((parent, name, fd))
        result = _TargetParent(workspace, fd, links, parts[-1])
        result.verify()
        yield result
        result.verify()


def _replace_status(
    record: CodingEvalDeliveryRecord,
    status: DeliveryStatus,
    reason: str,
    *,
    approval: ApprovalRecord | None = None,
    temporary_identity: tuple[int, int] | None = None,
    error_code: str | None = None,
) -> CodingEvalDeliveryRecord:
    now = datetime.now(UTC)
    events = (
        *record.transitions,
        transition(len(record.transitions) + 1, record.status, status, reason, now),
    )
    return _new_record(
        delivery_id=record.delivery_id,
        plan=record.plan,
        status=status,
        approval=approval or record.approval,
        temporary_identity=temporary_identity,
        error_code=error_code,
        transitions=events,
        created_at=record.created_at,
        updated_at=now,
    )


class CodingEvalDeliveryStore:
    """私有交付状态工厂；每次显式调用均取得单交付进程锁。"""

    def __init__(self, private_root: Path, git_executable: Path) -> None:
        self.root = _private_directory(private_root, create=True)
        self.git = _git_executable(git_executable)

    def _directory(self, delivery_id: UUID) -> Path:
        if type(delivery_id) is not UUID:
            raise KernelError("eval_delivery_id_invalid", "交付ID无效")
        return self.root / str(delivery_id)

    @contextmanager
    def _lock(self, delivery_id: UUID) -> Iterator[Path]:
        directory = _private_directory(self._directory(delivery_id))
        descriptor: int | None = None
        try:
            descriptor = os.open(directory / "owner.lock", _FILE_FLAGS)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise OSError
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise KernelError("eval_delivery_busy", "交付正在由另一进程处理") from None
            yield directory
        except KernelError:
            raise
        except OSError:
            raise KernelError("eval_delivery_state_invalid", "交付锁无效") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def prepare(
        self,
        package: CodingEvalChangePackage,
        target_root: Path,
        *,
        delivery_id: UUID | None = None,
    ) -> CodingEvalDeliveryRecord:
        package = CodingEvalChangePackage.model_validate_json(
            package.model_dump_json(), strict=True
        )
        identifier = delivery_id or uuid4()
        if type(identifier) is not UUID:
            raise KernelError("eval_delivery_id_invalid", "交付ID无效")
        try:
            target = target_root.resolve(strict=True)
        except (OSError, RuntimeError):
            raise KernelError("eval_delivery_target_invalid", "交付目标目录无效") from None
        if target == self.root or target in self.root.parents or self.root in target.parents:
            raise KernelError("eval_delivery_target_invalid", "交付状态根与目标仓库不能重叠")
        snapshot = _require_repository(package, None, target, self.git)
        before, mode, _ = _read_image(target, package.path)
        if (
            hashlib.sha256(before).hexdigest() != package.before.sha256
            or mode != package.source_mode
        ):
            raise KernelError("eval_delivery_source_drift", "交付文件前镜像或权限已漂移")
        with Workspace(target) as workspace:
            scope = workspace.scope
        now = datetime.now(UTC)
        plan = _new_plan(
            delivery_id=identifier,
            package_fingerprint=package.package_fingerprint,
            task_id=package.task_id,
            task_version=package.task_version,
            target_repository=package.repository,
            target_scope=scope,
            target_tree_oid=snapshot.tree_oid,
            path=package.path,
            source_mode=package.source_mode,
            before_sha256=package.before.sha256,
            after_sha256=package.after.sha256,
            created_at=now,
        )
        initial = transition(1, None, "pending_approval", "prepared", now)
        record = _new_record(
            delivery_id=identifier,
            plan=plan,
            status="pending_approval",
            approval=None,
            temporary_identity=None,
            error_code=None,
            transitions=(initial,),
            created_at=now,
            updated_at=now,
        )
        directory = self._directory(identifier)
        created = False
        descriptor: int | None = None
        try:
            os.mkdir(directory, 0o700)
            created = True
            descriptor = os.open(
                directory / "owner.lock",
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            write_coding_eval_change_package(directory / "package.json", package)
            _write_record(directory / "state.json", record)
            parent = os.open(self.root, _DIRECTORY_FLAGS)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except (OSError, KernelError):
            if created:
                shutil.rmtree(directory, ignore_errors=True)
            raise KernelError("eval_delivery_prepare_failed", "交付计划持久化失败") from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        return record

    def get(self, delivery_id: UUID) -> CodingEvalDeliveryRecord:
        with _delivery_errors(), self._lock(delivery_id) as directory:
            return _read_record(directory / "state.json")

    def decide(self, delivery_id: UUID, approval: ApprovalRecord) -> CodingEvalDeliveryRecord:
        approval = ApprovalRecord.model_validate_json(approval.model_dump_json())
        with self._lock(delivery_id) as directory:
            path = directory / "state.json"
            record = _read_record(path)
            if approval.request_fingerprint != record.plan.approval_fingerprint:
                raise KernelError("eval_delivery_approval_mismatch", "交付决定未绑定当前计划")
            if record.approval is not None:
                if record.approval != approval:
                    raise KernelError("eval_delivery_approval_conflict", "交付已经保存不同决定")
                return record
            if record.status != "pending_approval":
                raise KernelError("eval_delivery_not_decidable", "交付当前状态不能批准")
            status: DeliveryStatus = (
                "approved" if approval.outcome is ApprovalOutcome.APPROVED else "rejected"
            )
            result = _replace_status(
                record,
                status,
                "approval_recorded" if status == "approved" else "rejection_recorded",
                approval=approval,
            )
            _write_record(path, result)
            return result

    def _load_locked(
        self, directory: Path
    ) -> tuple[CodingEvalDeliveryRecord, CodingEvalChangePackage]:
        record = _read_record(directory / "state.json")
        package = read_coding_eval_change_package(directory / "package.json")
        if record.plan.package_fingerprint != package.package_fingerprint:
            raise KernelError("eval_delivery_plan_mismatch", "交付状态与变更包不一致")
        return record, package

    @staticmethod
    def _temp_name(record: CodingEvalDeliveryRecord) -> str:
        return f".{record.plan.path.rsplit('/', 1)[-1]}.harnessix-{record.delivery_id}.tmp"

    @staticmethod
    def _temp_relative(record: CodingEvalDeliveryRecord) -> str:
        parent = record.plan.path.rsplit("/", 1)[0] if "/" in record.plan.path else ""
        name = CodingEvalDeliveryStore._temp_name(record)
        return f"{parent}/{name}" if parent else name

    @staticmethod
    def _allowed_temp_status(record: CodingEvalDeliveryRecord) -> bytes:
        return b"? " + CodingEvalDeliveryStore._temp_relative(record).encode("utf-8") + b"\0"

    def _reconcile_locked(
        self,
        directory: Path,
        record: CodingEvalDeliveryRecord,
        package: CodingEvalChangePackage,
        target: Path,
    ) -> CodingEvalDeliveryRecord:
        if record.status != "applying":
            return record
        try:
            _require_repository(
                package,
                record.plan,
                target,
                self.git,
                allowed_statuses=None,
            )
            body, mode, target_identity = _read_image(target, record.plan.path)
            body_sha = hashlib.sha256(body).hexdigest()
        except KernelError:
            result = _replace_status(
                record,
                "unknown",
                "reconciliation_unavailable",
                error_code="eval_delivery_reconciliation_unavailable",
            )
        else:
            if body_sha == package.after.sha256 and mode == package.source_mode:
                if target_identity == record.temporary_identity:
                    result = _replace_status(record, "applied", "postimage_observed")
                else:
                    self._cleanup_temp(record, target)
                    result = _replace_status(
                        record,
                        "unknown",
                        "postimage_unattributed",
                        error_code="eval_delivery_postimage_unattributed",
                    )
            elif body_sha == package.before.sha256 and mode == package.source_mode:
                self._cleanup_temp(record, target)
                result = _replace_status(
                    record,
                    "approved",
                    "effect_not_observed",
                    error_code="eval_delivery_effect_not_observed",
                )
            else:
                self._cleanup_temp(record, target)
                result = _replace_status(
                    record,
                    "conflicted",
                    "target_diverged",
                    error_code="eval_delivery_target_diverged",
                )
        _write_record(directory / "state.json", result)
        return result

    def reconcile(self, delivery_id: UUID, target_root: Path) -> CodingEvalDeliveryRecord:
        try:
            target = target_root.resolve(strict=True)
        except (OSError, RuntimeError):
            raise KernelError("eval_delivery_target_invalid", "交付目标目录无效") from None
        with _delivery_errors(), self._lock(delivery_id) as directory:
            record, package = self._load_locked(directory)
            return self._reconcile_locked(directory, record, package, target)

    def _cleanup_temp(self, record: CodingEvalDeliveryRecord, target: Path) -> None:
        expected = record.temporary_identity
        if expected is None:
            return
        try:
            with (
                Workspace(target) as workspace,
                _target_parent(workspace, record.plan.path) as parent,
            ):
                name = self._temp_name(record)
                info = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
                if identity(info) == expected:
                    os.unlink(name, dir_fd=parent.fd)
                    os.fsync(parent.fd)
        except (OSError, ReadToolError, KernelError):
            pass

    def execute(self, delivery_id: UUID, target_root: Path) -> CodingEvalDeliveryRecord:
        try:
            target = target_root.resolve(strict=True)
        except (OSError, RuntimeError):
            raise KernelError("eval_delivery_target_invalid", "交付目标目录无效") from None
        with _delivery_errors(), self._lock(delivery_id) as directory:
            record, package = self._load_locked(directory)
            if record.status == "applying":
                record = self._reconcile_locked(directory, record, package, target)
            if record.status == "applied":
                return record
            if record.status != "approved":
                raise KernelError("eval_delivery_not_executable", "交付没有可执行的批准")
            _require_repository(package, record.plan, target, self.git)
            before, mode, before_identity = _read_image(target, record.plan.path)
            if (
                hashlib.sha256(before).hexdigest() != package.before.sha256
                or mode != package.source_mode
            ):
                raise KernelError("eval_delivery_source_drift", "交付文件前镜像或权限已漂移")

            with (
                Workspace(target) as workspace,
                _target_parent(workspace, record.plan.path) as parent,
            ):
                name = self._temp_name(record)
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    package.source_mode,
                    dir_fd=parent.fd,
                )
                try:
                    body = package.after.text.encode("utf-8")
                    remaining = memoryview(body)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written <= 0:
                            raise OSError
                        remaining = remaining[written:]
                    os.fchmod(descriptor, package.source_mode)
                    os.fsync(descriptor)
                    temporary_identity = identity(os.fstat(descriptor))
                    applying = _replace_status(
                        record,
                        "applying",
                        "effect_intent_persisted",
                        temporary_identity=temporary_identity,
                    )
                    _write_record(directory / "state.json", applying)
                    _fault("delivery.intent_persisted")

                    try:
                        _require_repository(
                            package,
                            applying.plan,
                            target,
                            self.git,
                            allowed_statuses=(b"", self._allowed_temp_status(applying)),
                        )
                        current, current_mode, current_identity = _read_image(
                            target, applying.plan.path
                        )
                        if (
                            hashlib.sha256(current).hexdigest() != package.before.sha256
                            or current_mode != package.source_mode
                            or current_identity != before_identity
                        ):
                            raise KernelError(
                                "eval_delivery_target_changed", "交付文件在最终替换前变化"
                            )
                        parent.verify()
                        linked = os.stat(parent.name, dir_fd=parent.fd, follow_symlinks=False)
                        if identity(linked) != before_identity:
                            raise KernelError(
                                "eval_delivery_target_changed", "交付文件在最终替换前变化"
                            )
                    except KernelError as error:
                        self._cleanup_temp(applying, target)
                        restored = _replace_status(
                            applying,
                            "approved",
                            "precondition_failed",
                            error_code=error.code,
                        )
                        _write_record(directory / "state.json", restored)
                        raise

                    _fault("delivery.before_replace")
                    os.replace(name, parent.name, src_dir_fd=parent.fd, dst_dir_fd=parent.fd)
                    _fault("delivery.after_replace")
                    os.fsync(descriptor)
                    os.fsync(parent.fd)
                    _fault("delivery.directories_synced")
                    parent.verify()
                    after, after_mode, after_identity = _read_image(target, applying.plan.path)
                    if (
                        hashlib.sha256(after).hexdigest() != package.after.sha256
                        or after_mode != package.source_mode
                        or after_identity != temporary_identity
                    ):
                        raise KernelError(
                            "eval_delivery_postimage_unverified", "交付后镜像无法归因"
                        )
                    result = _replace_status(applying, "applied", "postimage_verified")
                    _write_record(directory / "state.json", result)
                    _fault("delivery.result_recorded")
                    return result
                finally:
                    os.close(descriptor)
