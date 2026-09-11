"""从固定历史revision构造不含后续Git历史的私有Eval工作区。"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.evals.catalog import HistoricalCodingEval
from harnessix.evals.contracts import CodingEvalMaterialization

_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_TREE_BYTES = 16 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_MANIFEST_BYTES = 64 * 1024
_GIT_TIMEOUT_SECONDS = 30
_GIT_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}
_COMMIT_ENVIRONMENT = {
    **_GIT_ENVIRONMENT,
    "GIT_AUTHOR_NAME": "Harnessix Eval",
    "GIT_AUTHOR_EMAIL": "eval@harnessix.invalid",
    "GIT_COMMITTER_NAME": "Harnessix Eval",
    "GIT_COMMITTER_EMAIL": "eval@harnessix.invalid",
    "GIT_AUTHOR_DATE": "2026-09-03T03:02:41Z",
    "GIT_COMMITTER_DATE": "2026-09-03T03:02:41Z",
}


@dataclass(frozen=True, slots=True)
class MaterializedCodingEval:
    """隔离物化后的Eval Workspace与隐藏检查入口。"""

    run_root: Path
    workspace: Path
    manifest: CodingEvalMaterialization


def _git_executable(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except (OSError, RuntimeError):
        raise KernelError("eval_git_binding_invalid", "Eval Git可执行文件绑定无效") from None
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        raise KernelError("eval_git_binding_invalid", "Eval Git可执行文件绑定无效")
    return resolved


def _run_git(
    git: Path,
    cwd: Path,
    arguments: tuple[str, ...],
    *,
    environment: dict[str, str] = _GIT_ENVIRONMENT,
    output: int | None = subprocess.PIPE,
) -> bytes:
    try:
        result = subprocess.run(
            (str(git), *arguments),
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        raise KernelError("eval_git_command_failed", "Eval固定Git命令执行失败") from None
    if result.returncode != 0:
        raise KernelError("eval_git_command_failed", "Eval固定Git命令执行失败")
    body = result.stdout if isinstance(result.stdout, bytes) else b""
    if len(body) > _MAX_TREE_BYTES:
        raise KernelError("eval_git_output_too_large", "Eval Git元数据超过大小上限")
    return body


def _text(value: bytes) -> str:
    try:
        return value.decode("utf-8", errors="strict").removesuffix("\n")
    except UnicodeError:
        raise KernelError("eval_git_output_invalid", "Eval Git元数据不是合法UTF-8") from None


def _tree_inventory(git: Path, root: Path, revision: str) -> tuple[str, int]:
    body = _run_git(git, root, ("ls-tree", "-r", "-z", "--full-tree", revision))
    files = body.count(b"\0")
    if not 1 <= files <= _MAX_ARCHIVE_MEMBERS:
        raise KernelError("eval_source_tree_invalid", "Eval来源树文件数量无效")
    return hashlib.sha256(body).hexdigest(), files


def _archive_sha256(path: Path) -> tuple[str, int]:
    size = path.stat().st_size
    if not 1 <= size <= _MAX_ARCHIVE_BYTES:
        raise KernelError("eval_source_archive_too_large", "Eval来源归档为空或超过上限")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest(), size


def _extract_archive(archive: Path, workspace: Path) -> None:
    try:
        with tarfile.open(archive, mode="r:") as bundle:
            members = bundle.getmembers()
            if not 1 <= len(members) <= _MAX_ARCHIVE_MEMBERS:
                raise ValueError
            total = 0
            for member in members:
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or not path.parts
                    or any(part in {"", ".", "..", ".git"} for part in path.parts)
                    or not (member.isdir() or member.isfile())
                ):
                    raise ValueError
                total += member.size
                if total > _MAX_ARCHIVE_BYTES:
                    raise ValueError
            bundle.extractall(workspace, members=members, filter="data")
    except (OSError, tarfile.TarError, ValueError):
        raise KernelError("eval_source_archive_invalid", "Eval来源归档结构无效") from None


def _write_manifest(path: Path, manifest: CodingEvalMaterialization) -> None:
    body = (manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
    if len(body) > _MAX_MANIFEST_BYTES:
        raise KernelError("eval_materialization_manifest_too_large", "Eval物化清单超过上限")
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor: int | None = None
    try:
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
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise KernelError("eval_materialization_manifest_failed", "Eval物化清单发布失败") from None
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


def _read_manifest(path: Path) -> CodingEvalMaterialization:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o777 != 0o600
            or not 1 <= info.st_size <= _MAX_MANIFEST_BYTES
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = _MAX_MANIFEST_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(16_384, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) != info.st_size:
            raise OSError
        return CodingEvalMaterialization.model_validate_json(body, strict=True)
    except (OSError, ValidationError, ValueError):
        raise KernelError(
            "eval_materialization_manifest_invalid", "Eval物化清单缺失、损坏或不受支持"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _require_manifest_task(
    manifest: CodingEvalMaterialization, definition: HistoricalCodingEval, run_id: UUID
) -> None:
    task = definition.task
    if (
        manifest.run_id != run_id
        or manifest.task_id != task.task_id
        or manifest.task_version != task.task_version
        or manifest.task_fingerprint != task.fingerprint
        or manifest.source_revision != task.repository.source_revision
        or manifest.source_tree_oid != definition.source_tree_oid
        or manifest.baseline_tree_sha256 != task.repository.baseline_tree_sha256
    ):
        raise KernelError("eval_materialization_mismatch", "Eval物化清单与任务身份不一致")


def _require_private_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise KernelError("eval_materialization_incomplete", "Eval物化目录未完整发布") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o777 != 0o700:
        raise KernelError("eval_materialization_incomplete", "Eval物化目录未完整发布")


def load_materialized_coding_eval(
    runs_root: Path,
    git_executable: Path,
    definition: HistoricalCodingEval,
    run_id: UUID,
) -> MaterializedCodingEval:
    """从最后发布的ready清单重开工作区；允许保留Agent未提交的工作树变更。"""

    git = _git_executable(git_executable)
    try:
        root = runs_root.resolve(strict=True)
        run_root = root / str(run_id)
        _require_private_directory(run_root)
        manifest = _read_manifest(run_root / "materialization.json")
        workspace = run_root / manifest.workspace_directory
        _require_private_directory(workspace)
    except KernelError:
        raise
    except (OSError, RuntimeError):
        raise KernelError("eval_materialization_incomplete", "Eval物化目录未完整发布") from None
    _require_manifest_task(manifest, definition, run_id)
    head = _text(_run_git(git, workspace, ("rev-parse", "--verify", "HEAD^{commit}")))
    tree_sha256, files = _tree_inventory(git, workspace, "HEAD")
    if (
        head != manifest.baseline_revision
        or tree_sha256 != manifest.baseline_tree_sha256
        or files != manifest.tracked_files
    ):
        raise KernelError("eval_materialization_changed", "Eval基线提交或树身份已变化")
    return MaterializedCodingEval(run_root=run_root, workspace=workspace, manifest=manifest)


def materialize_historical_coding_eval(
    source_root: Path,
    runs_root: Path,
    git_executable: Path,
    definition: HistoricalCodingEval,
    run_id: UUID,
) -> MaterializedCodingEval:
    """物化固定历史任务；相同run_id只重开ready事实，不覆盖已有目录。"""

    git = _git_executable(git_executable)
    try:
        source = source_root.resolve(strict=True)
        root = runs_root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise KernelError("eval_materialization_path_invalid", "Eval来源或运行根目录无效") from None
    if source == root or source in root.parents:
        raise KernelError("eval_materialization_path_invalid", "Eval运行根不能位于来源仓库内")
    run_root = root / str(run_id)
    if run_root.exists() or run_root.is_symlink():
        return load_materialized_coding_eval(root, git, definition, run_id)

    task = definition.task
    created = False
    try:
        top = _text(_run_git(git, source, ("rev-parse", "--show-toplevel")))
        if top != str(source):
            raise KernelError("eval_source_root_mismatch", "Eval来源必须是精确Git仓库根")
        revision = _text(
            _run_git(
                git,
                source,
                ("rev-parse", "--verify", f"{task.repository.source_revision}^{{commit}}"),
            )
        )
        tree_oid = _text(_run_git(git, source, ("rev-parse", "--verify", f"{revision}^{{tree}}")))
        tree_sha256, tracked_files = _tree_inventory(git, source, revision)
        if (
            revision != task.repository.source_revision
            or tree_oid != definition.source_tree_oid
            or tree_sha256 != task.repository.baseline_tree_sha256
        ):
            raise KernelError("eval_source_tree_mismatch", "Eval来源revision或树与任务不一致")

        os.mkdir(run_root, 0o700)
        created = True
        workspace = run_root / "workspace"
        workspace.mkdir(mode=0o700)
        archive = run_root / ".source.tar"
        _run_git(
            git,
            source,
            ("archive", "--format=tar", f"--output={archive}", revision),
            output=subprocess.DEVNULL,
        )
        archive_sha256, archive_bytes = _archive_sha256(archive)
        _extract_archive(archive, workspace)
        archive.unlink()

        _run_git(
            git,
            workspace,
            ("-c", "init.defaultBranch=main", "init", "-q"),
            output=subprocess.DEVNULL,
        )
        _run_git(
            git,
            workspace,
            ("add", "--all", "--force"),
            output=subprocess.DEVNULL,
        )
        _run_git(
            git,
            workspace,
            ("commit", "-q", "--no-gpg-sign", "-m", "Harnessix Coding Eval baseline"),
            environment=_COMMIT_ENVIRONMENT,
            output=subprocess.DEVNULL,
        )
        baseline_revision = _text(
            _run_git(git, workspace, ("rev-parse", "--verify", "HEAD^{commit}"))
        )
        materialized_tree, materialized_files = _tree_inventory(git, workspace, "HEAD")
        status = _run_git(git, workspace, ("status", "--porcelain=v1", "-z"))
        if status or materialized_tree != tree_sha256 or materialized_files != tracked_files:
            raise KernelError("eval_materialization_tree_mismatch", "Eval私有基线树物化不一致")
        git_version = _text(_run_git(git, source, ("--version",)))
        manifest = CodingEvalMaterialization(
            run_id=run_id,
            task_id=task.task_id,
            task_version=task.task_version,
            task_fingerprint=task.fingerprint,
            source_revision=revision,
            source_tree_oid=tree_oid,
            source_archive_sha256=archive_sha256,
            baseline_revision=baseline_revision,
            baseline_tree_sha256=tree_sha256,
            tracked_files=tracked_files,
            archive_bytes=archive_bytes,
            git_version=git_version,
            created_at=datetime.now(UTC),
        )
        _write_manifest(run_root / "materialization.json", manifest)
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        return MaterializedCodingEval(run_root=run_root, workspace=workspace, manifest=manifest)
    except KernelError:
        if created:
            shutil.rmtree(run_root, ignore_errors=True)
        raise
    except (OSError, ValueError, tarfile.TarError):
        if created:
            shutil.rmtree(run_root, ignore_errors=True)
        raise KernelError("eval_materialization_failed", "Eval历史工作区物化失败") from None
