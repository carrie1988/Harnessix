"""从内置Task Pack Archive构造私有、单提交且可恢复的Eval工作区。"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.evals.task_pack import (
    LoadedCodingEvalTaskPack,
    _read_regular,
    _resource,
    _verified_builtin_task_pack,
)
from harnessix.evals.task_pack_contracts import (
    CodingEvalTaskPack,
    CodingEvalTaskPackCase,
    CodingEvalTaskPackMaterialization,
    CodingEvalTaskPackRepository,
)

_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_TREE_BYTES = 16 * 1024 * 1024
_MAX_MANIFEST_BYTES = 128 * 1024
_GIT_TIMEOUT_SECONDS = 30
_COMMIT_MESSAGE = "Harnessix Eval Task Pack baseline"
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


@dataclass(frozen=True, slots=True)
class MaterializedCodingEvalTaskPackCase:
    """一个Task Pack Case已物化的私有Git工作区和身份清单。"""

    run_root: Path
    workspace: Path
    case: CodingEvalTaskPackCase
    manifest: CodingEvalTaskPackMaterialization


def _git_executable(path: Path) -> Path:
    try:
        candidate = path.resolve(strict=True)
        info = candidate.stat()
    except (OSError, RuntimeError):
        raise KernelError("eval_task_pack_git_invalid", "Task Pack Git绑定无效") from None
    if not stat.S_ISREG(info.st_mode) or not os.access(candidate, os.X_OK):
        raise KernelError("eval_task_pack_git_invalid", "Task Pack Git绑定无效")
    return candidate


def _run_git(
    git: Path,
    cwd: Path,
    arguments: tuple[str, ...],
    *,
    environment: dict[str, str] | None = None,
) -> bytes:
    env = _GIT_ENVIRONMENT if environment is None else environment
    try:
        result = subprocess.run(
            (str(git), "-c", "core.autocrlf=false", *arguments),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        raise KernelError("eval_task_pack_git_failed", "Task Pack固定Git命令失败") from None
    if result.returncode != 0 or len(result.stdout) > _MAX_TREE_BYTES:
        raise KernelError("eval_task_pack_git_failed", "Task Pack固定Git命令失败")
    return result.stdout


def _git_text(body: bytes) -> str:
    try:
        return body.decode("utf-8", errors="strict").removesuffix("\n")
    except UnicodeError:
        raise KernelError("eval_task_pack_git_failed", "Task Pack Git输出不是UTF-8") from None


def _tree_inventory(git: Path, root: Path, revision: str) -> tuple[str, int]:
    body = _run_git(git, root, ("ls-tree", "-r", "-z", "--full-tree", revision))
    files = body.count(b"\0")
    if not 1 <= files <= _MAX_ARCHIVE_MEMBERS:
        raise KernelError("eval_task_pack_tree_invalid", "Task Pack Git树文件数无效")
    return hashlib.sha256(body).hexdigest(), files


def _safe_archive_members(
    archive: tarfile.TarFile,
    repository: CodingEvalTaskPackRepository,
) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    if not 1 <= len(members) <= _MAX_ARCHIVE_MEMBERS:
        raise ValueError
    files = 0
    total = 0
    paths: set[str] = set()
    for member in members:
        name = member.name[:-1] if member.isdir() and member.name.endswith("/") else member.name
        path = PurePosixPath(name)
        parts = name.split("/")
        mode = member.mode & 0o777
        if (
            not name
            or len(name.encode("utf-8")) > 1024
            or path.is_absolute()
            or str(path) != name
            or "\\" in name
            or len(parts) > 64
            or any(
                part in {"", ".", ".."}
                or part.casefold() == ".git"
                or len(part.encode("utf-8")) > 255
                or any(ord(character) < 32 or ord(character) == 127 for character in part)
                for part in parts
            )
            or name in paths
            or not (member.isdir() or member.isfile())
            or member.mode & 0o7000
            or (member.isdir() and mode != 0o755)
            or (member.isfile() and mode not in {0o644, 0o755})
            or member.size < 0
            or (member.isdir() and member.size != 0)
        ):
            raise ValueError
        paths.add(name)
        if member.isfile():
            files += 1
            total += member.size
        if total > _MAX_ARCHIVE_BYTES:
            raise ValueError
    if files != repository.tracked_files:
        raise ValueError
    return members


def _extract_archive(
    archive_path: Path,
    workspace: Path,
    repository: CodingEvalTaskPackRepository,
) -> None:
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            members = _safe_archive_members(archive, repository)
            archive.extractall(workspace, members=members, filter="data")
        license_path = workspace / repository.license.license_file
        info = license_path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise OSError
        if (
            hashlib.sha256(license_path.read_bytes()).hexdigest()
            != repository.license.license_sha256
        ):
            raise ValueError
    except (OSError, tarfile.TarError, ValueError):
        raise KernelError(
            "eval_task_pack_archive_invalid", "Task Pack Archive结构或许可证无效"
        ) from None


def _verify_review_oracle(workspace: Path, case: CodingEvalTaskPackCase) -> None:
    """把Review Finding绑定到Archive内的精确源码行字节，而不是信任清单声明。"""

    oracle = case.review_oracle
    if oracle is None:
        return
    try:
        root = workspace.resolve(strict=True)
        for finding in oracle.required_findings:
            expected = workspace / finding.path
            target = expected.resolve(strict=True)
            if target != expected or root not in target.parents:
                raise OSError
            body = _read_regular(
                target,
                _MAX_TREE_BYTES,
                code="eval_task_pack_review_oracle_invalid",
                label="Task Pack Review Oracle源码",
            )
            body.decode("utf-8", errors="strict")
            lines = body.splitlines(keepends=True)
            if finding.end_line > len(lines):
                raise ValueError
            evidence = b"".join(lines[finding.start_line - 1 : finding.end_line])
            if hashlib.sha256(evidence).hexdigest() != finding.evidence_sha256:
                raise ValueError
    except KernelError:
        raise
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise KernelError(
            "eval_task_pack_review_oracle_invalid",
            "Task Pack Review Oracle源码证据不匹配",
        ) from None


def _commit_environment(repository: CodingEvalTaskPackRepository) -> dict[str, str]:
    timestamp = repository.commit_created_at.isoformat().replace("+00:00", "Z")
    return {
        **_GIT_ENVIRONMENT,
        "GIT_AUTHOR_NAME": "Harnessix Eval",
        "GIT_AUTHOR_EMAIL": "eval@harnessix.invalid",
        "GIT_COMMITTER_NAME": "Harnessix Eval",
        "GIT_COMMITTER_EMAIL": "eval@harnessix.invalid",
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_COMMITTER_DATE": timestamp,
    }


def _create_baseline_commit(
    git: Path,
    workspace: Path,
    repository: CodingEvalTaskPackRepository,
) -> None:
    _run_git(git, workspace, ("init", "--quiet"))
    _run_git(git, workspace, ("symbolic-ref", "HEAD", "refs/heads/main"))
    _run_git(git, workspace, ("add", "--force", "--all"))
    _run_git(
        git,
        workspace,
        ("-c", "commit.gpgsign=false", "commit", "--quiet", "--no-gpg-sign", "-m", _COMMIT_MESSAGE),
        environment=_commit_environment(repository),
    )


def _write_manifest(path: Path, manifest: CodingEvalTaskPackMaterialization) -> None:
    body = (manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
    if len(body) > _MAX_MANIFEST_BYTES:
        raise KernelError("eval_task_pack_materialization_too_large", "Task Pack物化清单过大")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
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
        raise KernelError(
            "eval_task_pack_materialization_failed", "Task Pack物化清单写入失败"
        ) from None
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


def _read_manifest(path: Path) -> CodingEvalTaskPackMaterialization:
    body = _read_regular(
        path,
        _MAX_MANIFEST_BYTES,
        code="eval_task_pack_materialization_invalid",
        label="Task Pack物化清单",
    )
    try:
        info = path.stat()
        if info.st_mode & 0o777 != 0o600:
            raise OSError
        return CodingEvalTaskPackMaterialization.model_validate_json(body, strict=True)
    except (OSError, ValidationError, ValueError):
        raise KernelError(
            "eval_task_pack_materialization_invalid", "Task Pack物化清单损坏或权限无效"
        ) from None


def _secured_directory(path: Path, expected_mode: int) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise KernelError(
            "eval_task_pack_materialization_incomplete", "Task Pack物化目录不完整"
        ) from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_mode & 0o777 != expected_mode
    ):
        raise KernelError("eval_task_pack_materialization_incomplete", "Task Pack物化目录不安全")


def _checked_pack(loaded: LoadedCodingEvalTaskPack) -> LoadedCodingEvalTaskPack:
    return _verified_builtin_task_pack(loaded)


def _require_manifest(
    manifest: CodingEvalTaskPackMaterialization,
    pack: CodingEvalTaskPack,
    case: CodingEvalTaskPackCase,
    repository: CodingEvalTaskPackRepository,
    run_id: UUID,
) -> None:
    task = case.task
    if (
        manifest.run_id != run_id
        or manifest.pack_id != pack.pack_id
        or manifest.pack_version != pack.pack_version
        or manifest.pack_sha256 != pack.pack_sha256
        or manifest.case_id != case.case_id
        or manifest.task_id != task.task_id
        or manifest.task_version != task.task_version
        or manifest.task_fingerprint != task.fingerprint
        or manifest.repository_id != repository.repository_id
        or manifest.source_revision != repository.repository.source_revision
        or manifest.source_tree_oid != repository.source_tree_oid
        or manifest.source_archive_sha256 != repository.archive_sha256
        or manifest.baseline_tree_sha256 != repository.repository.baseline_tree_sha256
        or manifest.tracked_files != repository.tracked_files
        or manifest.archive_bytes != repository.archive_bytes
    ):
        raise KernelError("eval_task_pack_materialization_mismatch", "Task Pack物化身份不匹配")


def load_materialized_task_pack_case(
    loaded: LoadedCodingEvalTaskPack,
    runs_root: Path,
    git_executable: Path,
    case_id: str,
    run_id: UUID,
) -> MaterializedCodingEvalTaskPackCase:
    """重开同一Run的Task Pack工作区，只读取固定HEAD身份而不覆盖Agent变更。"""

    verified = _checked_pack(loaded)
    pack = verified.manifest
    try:
        case = pack.case(case_id)
        repository = pack.repository(case.repository_id)
    except KeyError:
        raise KernelError("eval_task_pack_case_not_found", "Task Pack Case不存在") from None
    git = _git_executable(git_executable)
    try:
        root = runs_root.resolve(strict=True)
        run_root = root / str(run_id)
        _secured_directory(run_root, 0o700)
        manifest = _read_manifest(run_root / "materialization.json")
        workspace = run_root / manifest.workspace_directory
        _secured_directory(workspace, 0o755)
    except KernelError:
        raise
    except (OSError, RuntimeError):
        raise KernelError(
            "eval_task_pack_materialization_incomplete", "Task Pack物化目录不完整"
        ) from None
    _require_manifest(manifest, pack, case, repository, run_id)
    head = _git_text(_run_git(git, workspace, ("rev-parse", "--verify", "HEAD^{commit}")))
    tree_oid = _git_text(_run_git(git, workspace, ("rev-parse", "--verify", "HEAD^{tree}")))
    tree_sha256, files = _tree_inventory(git, workspace, "HEAD")
    if (
        head != repository.repository.source_revision
        or tree_oid != repository.source_tree_oid
        or tree_sha256 != repository.repository.baseline_tree_sha256
        or files != repository.tracked_files
    ):
        raise KernelError("eval_task_pack_materialization_changed", "Task Pack基线提交或树已变化")
    return MaterializedCodingEvalTaskPackCase(run_root, workspace, case, manifest)


def materialize_task_pack_case(
    loaded: LoadedCodingEvalTaskPack,
    runs_root: Path,
    git_executable: Path,
    case_id: str,
    run_id: UUID,
) -> MaterializedCodingEvalTaskPackCase:
    """从内置Archive物化固定Case；同一Run ID存在时只执行严格重开。"""

    verified = _checked_pack(loaded)
    pack = verified.manifest
    try:
        case = pack.case(case_id)
        repository = pack.repository(case.repository_id)
    except KeyError:
        raise KernelError("eval_task_pack_case_not_found", "Task Pack Case不存在") from None
    git = _git_executable(git_executable)
    try:
        root = runs_root.resolve(strict=True)
        resource_root = verified.resource_root
    except (OSError, RuntimeError):
        raise KernelError(
            "eval_task_pack_materialization_path_invalid", "Task Pack路径无效"
        ) from None
    if root == resource_root or root in resource_root.parents or resource_root in root.parents:
        raise KernelError(
            "eval_task_pack_materialization_path_invalid", "Task Pack资源与运行根必须分离"
        )
    run_root = root / str(run_id)
    if run_root.exists() or run_root.is_symlink():
        return load_materialized_task_pack_case(loaded, root, git, case_id, run_id)
    archive = _resource(
        resource_root,
        repository.archive_file,
        code="eval_task_pack_archive_invalid",
        label="Task Pack Archive",
    )
    body = _read_regular(
        archive,
        _MAX_ARCHIVE_BYTES,
        code="eval_task_pack_archive_invalid",
        label="Task Pack Archive",
    )
    if (
        len(body) != repository.archive_bytes
        or hashlib.sha256(body).hexdigest() != repository.archive_sha256
    ):
        raise KernelError("eval_task_pack_archive_invalid", "Task Pack Archive摘要或大小不一致")

    created = False
    try:
        os.mkdir(run_root, 0o700)
        created = True
        workspace = run_root / "workspace"
        # 父目录0700维持宿主私有边界；Workspace需允许固定非root容器读取只读挂载。
        os.mkdir(workspace, 0o755)
        _extract_archive(archive, workspace, repository)
        _verify_review_oracle(workspace, case)
        _create_baseline_commit(git, workspace, repository)
        head = _git_text(_run_git(git, workspace, ("rev-parse", "--verify", "HEAD^{commit}")))
        tree_oid = _git_text(_run_git(git, workspace, ("rev-parse", "--verify", "HEAD^{tree}")))
        tree_sha256, files = _tree_inventory(git, workspace, "HEAD")
        if (
            head != repository.repository.source_revision
            or tree_oid != repository.source_tree_oid
            or tree_sha256 != repository.repository.baseline_tree_sha256
            or files != repository.tracked_files
        ):
            raise KernelError(
                "eval_task_pack_source_mismatch", "Task Pack Archive无法重建固定Git来源"
            )
        manifest = CodingEvalTaskPackMaterialization(
            run_id=run_id,
            pack_id=pack.pack_id,
            pack_version=pack.pack_version,
            pack_sha256=pack.pack_sha256,
            case_id=case.case_id,
            task_id=case.task.task_id,
            task_version=case.task.task_version,
            task_fingerprint=case.task.fingerprint,
            repository_id=repository.repository_id,
            source_revision=repository.repository.source_revision,
            source_tree_oid=repository.source_tree_oid,
            source_archive_sha256=repository.archive_sha256,
            baseline_revision=head,
            baseline_tree_sha256=tree_sha256,
            tracked_files=files,
            archive_bytes=len(body),
            created_at=utc_now(),
        )
        _write_manifest(run_root / "materialization.json", manifest)
        return MaterializedCodingEvalTaskPackCase(run_root, workspace, case, manifest)
    except KernelError:
        if created:
            shutil.rmtree(run_root, ignore_errors=True)
        raise
    except (OSError, RuntimeError, ValueError):
        if created:
            shutil.rmtree(run_root, ignore_errors=True)
        raise KernelError("eval_task_pack_materialization_failed", "Task Pack物化失败") from None
