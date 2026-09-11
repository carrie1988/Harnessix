"""Workspace与Git交付：管理受控Git Worktree、Checkpoint与Commit生命周期。"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import WorkspaceMutation
from harnessix.delivery.git_contracts import (
    GitCheckpoint,
    GitCommitRecord,
    GitCommitSpec,
    GitCommitState,
    GitRepositoryBinding,
    GitWorktreeState,
    ManagedGitWorktreeBinding,
    ManagedGitWorktreePlan,
    ManagedGitWorktreeRecord,
    git_checkpoint_digest,
    git_commit_spec_fingerprint,
    git_repository_binding_digest,
    managed_git_worktree_binding_digest,
    managed_git_worktree_plan_fingerprint,
    transition_commit_record,
    transition_worktree_record,
)
from harnessix.delivery.git_store import SQLiteGitDeliveryStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.execution.contracts import canonical_digest
from harnessix.workspace.contracts import PlatformKind, WorkspaceLease
from harnessix.workspace.leases import WorkspaceLeaseStore
from harnessix.workspace.snapshot import verify_workspace_snapshot

_MAX_GIT_OUTPUT: Final = 1024 * 1024
_OID = re.compile(rb"^(?:[0-9a-f]{40}|[0-9a-f]{64})\n?$")


def _fault(_: str) -> None:
    """只供Git崩溃边界测试替换。"""


def _native_platform() -> PlatformKind:
    if os.name == "nt":
        return "windows"
    if os.name == "posix":
        return "posix"
    raise KernelError("git_platform_unsupported", "当前平台不支持Git交付")


def _path_text(path: Path) -> str:
    value = str(path)
    return os.path.normcase(value) if os.name == "nt" else value


def _path_sha256(path: Path) -> str:
    return hashlib.sha256(_path_text(path).encode("utf-8")).hexdigest()


def _identity(path: Path, *, directory: bool) -> str:
    try:
        info = path.lstat()
        valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not valid_type or stat.S_ISLNK(info.st_mode):
            raise OSError
        return canonical_digest(
            {
                "path": _path_text(path.resolve(strict=True)),
                "device": info.st_dev,
                "inode": info.st_ino,
                "mode_type": stat.S_IFMT(info.st_mode),
            }
        )
    except OSError:
        raise KernelError("git_binding_changed", "Git绑定对象身份无效") from None


def _executable_identity(path: Path) -> str:
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
            raise OSError
        return canonical_digest(
            {
                "path": _path_text(path.resolve(strict=True)),
                "device": info.st_dev,
                "inode": info.st_ino,
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "ctime_ns": info.st_ctime_ns,
                "mode": info.st_mode,
            }
        )
    except OSError:
        raise KernelError("git_executable_invalid", "Git可执行文件身份无效") from None


def git_delivery_implementation_digest() -> str:
    root = Path(__file__).parent
    try:
        return canonical_digest(
            {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (
                    root / "git.py",
                    root / "git_contracts.py",
                    root / "git_store.py",
                )
            }
        )
    except OSError:
        raise KernelError("git_capability_unavailable", "Git交付实现不可证明") from None


class _GitRunner:
    def __init__(self, executable: str | Path, state_root: Path) -> None:
        path = Path(executable)
        try:
            if not path.is_absolute():
                raise OSError
            self.path = path.resolve(strict=True)
        except OSError:
            raise KernelError("git_executable_invalid", "Git可执行文件必须是绝对普通文件") from None
        self.identity = _executable_identity(self.path)
        self._home = state_root / "git-home"
        self._hooks = state_root / "empty-hooks"
        self._temp = state_root / "tmp"
        for directory in (self._home, self._hooks, self._temp):
            try:
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                if directory.is_symlink() or not directory.is_dir():
                    raise OSError
                if os.name == "posix":
                    directory.chmod(0o700)
            except OSError:
                raise KernelError("git_state_invalid", "Git私有执行目录无效") from None
        self._global = (
            "--no-pager",
            "--no-optional-locks",
            "-c",
            "color.ui=false",
            "-c",
            "core.hooksPath=" + str(self._hooks),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.attributesFile=" + os.devnull,
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.safecrlf=false",
        )

    def run(
        self,
        cwd: Path,
        arguments: tuple[str, ...],
        *,
        input_data: bytes | None = None,
        index_file: Path | None = None,
        accepted: tuple[int, ...] = (0,),
        timeout: float = 20.0,
        allowed_protocols: tuple[str, ...] = ("file",),
    ) -> subprocess.CompletedProcess[bytes]:
        if _executable_identity(self.path) != self.identity:
            raise KernelError("git_executable_changed", "Git可执行文件身份已经变化")
        if (
            not allowed_protocols
            or len(set(allowed_protocols)) != len(allowed_protocols)
            or allowed_protocols != tuple(sorted(allowed_protocols))
            or any(value not in {"file", "https", "ssh"} for value in allowed_protocols)
        ):
            raise KernelError("git_protocol_invalid", "Git协议白名单无效")
        environment = {
            "PATH": os.pathsep.join((str(self.path.parent), os.defpath)),
            "HOME": str(self._home),
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "GIT_PAGER": "cat",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ALLOW_PROTOCOL": ":".join(allowed_protocols),
            "TMPDIR": str(self._temp),
            "TEMP": str(self._temp),
            "TMP": str(self._temp),
        }
        if os.name == "nt":
            system_root = os.environ.get("SystemRoot", r"C:\Windows")
            environment.update(
                {
                    "SystemRoot": system_root,
                    "WINDIR": system_root,
                    "COMSPEC": str(Path(system_root) / "System32/cmd.exe"),
                }
            )
        if index_file is not None:
            if not index_file.is_absolute() or index_file.parent != self._temp:
                raise KernelError("git_index_invalid", "Git临时索引不属于私有目录")
            environment["GIT_INDEX_FILE"] = str(index_file)
        try:
            completed = subprocess.run(
                (str(self.path), *self._global, *arguments),
                cwd=cwd,
                env=environment,
                input=input_data,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            raise KernelError("git_process_failed", "Git固定命令启动或等待失败") from None
        if (
            completed.returncode not in accepted
            or len(completed.stdout) > _MAX_GIT_OUTPUT
            or len(completed.stderr) > _MAX_GIT_OUTPUT
        ):
            raise KernelError("git_command_failed", "Git固定命令失败或输出超过上限")
        return completed

    def oid(
        self,
        cwd: Path,
        arguments: tuple[str, ...],
        *,
        input_data: bytes | None = None,
        index_file: Path | None = None,
    ) -> str:
        output = self.run(cwd, arguments, input_data=input_data, index_file=index_file).stdout
        if _OID.fullmatch(output) is None:
            raise KernelError("git_output_invalid", "Git对象OID输出无效")
        return output.decode("ascii").strip()


class GitDeliveryRuntime:
    """固定Git执行端口；源码仓库只读，所有交付进入新branch。"""

    def __init__(
        self,
        workspace_store: SQLiteWorkspaceTransactionStore,
        git_store: SQLiteGitDeliveryStore,
        leases: WorkspaceLeaseStore,
        git_executable: str | Path,
    ) -> None:
        self._workspace_store = workspace_store
        self._store = git_store
        self._leases = leases
        self._git = _GitRunner(git_executable, git_store.root)
        self._worktrees = git_store.root / "worktrees"
        self._worktrees.mkdir(mode=0o700, exist_ok=True)
        if self._worktrees.is_symlink() or not self._worktrees.is_dir():
            raise KernelError("git_state_invalid", "受管Worktree根无效")
        if os.name == "posix":
            self._worktrees.chmod(0o700)

    def bind_repository(self, root: str | Path, workspace_id: str) -> GitRepositoryBinding:
        repository = self._repository_root(root)
        self._reject_unsafe_configuration(repository)
        head = self._git.oid(repository, ("rev-parse", "--verify", "HEAD^{commit}"))
        tree = self._git.oid(repository, ("rev-parse", "--verify", "HEAD^{tree}"))
        object_format = "sha1" if len(head) == 40 else "sha256" if len(head) == 64 else ""
        if not object_format:
            raise KernelError("git_object_format_unsupported", "Git对象格式不受支持")
        status = self._git.run(
            repository,
            ("status", "--porcelain=v2", "--untracked-files=all", "-z"),
        ).stdout
        if status:
            raise KernelError("delivery_dirty_conflict", "Git来源仓库不是干净状态")
        common = self._common_directory(repository)
        alternates = common / "objects/info/alternates"
        if alternates.exists() or alternates.is_symlink():
            raise KernelError("git_alternates_unsupported", "Git alternates在0.7不受支持")
        config = self._git.run(
            repository, ("config", "--includes", "--null", "--list", "--show-origin")
        ).stdout
        version = self._text(self._git.run(repository, ("version",)).stdout).strip()
        candidate = GitRepositoryBinding.model_construct(
            _fields_set=None,
            platform=_native_platform(),
            workspace_id=workspace_id,
            root_path_sha256=_path_sha256(repository),
            root_identity=_identity(repository, directory=True),
            common_directory_sha256=_path_sha256(common),
            common_directory_identity=_identity(common, directory=True),
            head_oid=head,
            head_tree_oid=tree,
            object_format=object_format,
            status_sha256=hashlib.sha256(status).hexdigest(),
            config_sha256=hashlib.sha256(config).hexdigest(),
            git_executable_identity=self._git.identity,
            git_version=version,
            implementation_digest=git_delivery_implementation_digest(),
            digest="0" * 64,
        )
        return GitRepositoryBinding(
            **candidate.model_dump(exclude={"digest"}),
            digest=git_repository_binding_digest(candidate),
        )

    def plan_worktree(
        self,
        transaction_id: UUID,
        repository_root: str | Path,
        *,
        worktree_id: UUID | None = None,
        now: datetime | None = None,
    ) -> ManagedGitWorktreeRecord:
        transaction = self._workspace_store.load(transaction_id)
        binding = self.bind_repository(repository_root, transaction.plan.source.workspace_id)
        verify_workspace_snapshot(transaction.plan.source, repository_root)
        identifier = worktree_id or uuid4()
        path = (self._worktrees / identifier.hex).absolute()
        created = now or datetime.now(UTC)
        candidate = ManagedGitWorktreePlan.model_construct(
            _fields_set=None,
            worktree_id=identifier,
            transaction_id=transaction_id,
            transaction_plan_fingerprint=transaction.plan.fingerprint,
            repository=binding,
            path=str(path),
            created_at=created,
            fingerprint="0" * 64,
        )
        plan = ManagedGitWorktreePlan(
            **candidate.model_dump(exclude={"fingerprint"}),
            fingerprint=managed_git_worktree_plan_fingerprint(candidate),
        )
        return self._store.save_worktree(plan)

    def create_worktree(
        self,
        worktree_id: UUID,
        repository_root: str | Path,
        *,
        approval_fingerprint: str,
        lease: WorkspaceLease,
    ) -> ManagedGitWorktreeRecord:
        """校验批准、Lease与仓库身份后创建受管Worktree，并可由持久阶段幂等恢复。"""
        record = self._store.load_worktree(worktree_id)
        self._authorize_worktree(record, repository_root, approval_fingerprint, lease)
        if record.state == "ready":
            self._verify_worktree(record)
            return record
        if record.state in {"diverged", "unknown"}:
            raise KernelError("git_worktree_not_executable", "受管Git Worktree处于不可执行终态")
        if record.state == "prepared":
            record = self._advance_worktree(record, "creating")
        else:
            record = self.reconcile_worktree(worktree_id, repository_root)
            if record.state == "ready":
                return record
            if record.state != "creating":
                raise KernelError("git_worktree_not_executable", "受管Git Worktree不能安全恢复")
        path = Path(record.plan.path)
        if path.exists() or path.is_symlink():
            return self._mark_worktree(record, "diverged", "git_worktree_path_occupied")
        repository = self._repository_root(repository_root)
        self._git.run(
            repository,
            (
                "worktree",
                "add",
                "--detach",
                "--no-checkout",
                str(path),
                record.plan.repository.head_oid,
            ),
        )
        _fault("worktree_registered")
        self._git.run(
            path,
            (
                "reset",
                "--hard",
                "--no-recurse-submodules",
                record.plan.repository.head_oid,
            ),
        )
        binding = self._capture_worktree_binding(record.plan)
        return self._advance_worktree(record, "ready", binding=binding)

    def reconcile_worktree(
        self, worktree_id: UUID, repository_root: str | Path
    ) -> ManagedGitWorktreeRecord:
        record = self._store.load_worktree(worktree_id)
        self._verify_repository(record.plan.repository, repository_root)
        if record.state == "ready":
            self._verify_worktree(record)
            return record
        if record.state in {"diverged", "unknown"}:
            return record
        path = Path(record.plan.path)
        registered = self._registered_worktrees(self._repository_root(repository_root))
        present = path.exists() or path.is_symlink()
        expected_path = _path_text(path.absolute())
        if not present and expected_path not in registered:
            return record
        if not present or expected_path not in registered:
            return self._mark_worktree(record, "diverged", "git_worktree_registration_diverged")
        try:
            if record.state == "creating":
                self._git.run(
                    path,
                    (
                        "reset",
                        "--hard",
                        "--no-recurse-submodules",
                        record.plan.repository.head_oid,
                    ),
                )
            binding = self._capture_worktree_binding(record.plan)
        except KernelError:
            return self._mark_worktree(record, "diverged", "git_worktree_binding_changed")
        if record.state == "prepared":
            return self._mark_worktree(record, "diverged", "git_worktree_unowned_effect")
        return self._advance_worktree(record, "ready", binding=binding)

    def create_checkpoint(
        self,
        worktree_id: UUID,
        repository_root: str | Path,
        *,
        lease: WorkspaceLease,
        checkpoint_id: UUID | None = None,
        now: datetime | None = None,
    ) -> GitCheckpoint:
        """冻结ready Worktree的树、父提交和完整状态；任一观察漂移即拒绝Checkpoint。"""
        worktree = self._store.load_worktree(worktree_id)
        if worktree.state != "ready" or worktree.binding is None:
            raise KernelError("git_worktree_not_ready", "Git Checkpoint要求ready worktree")
        self._verify_worktree(worktree)
        self._verify_repository(worktree.plan.repository, repository_root)
        transaction = self._workspace_store.load(worktree.plan.transaction_id)
        if transaction.plan.fingerprint != worktree.plan.transaction_plan_fingerprint:
            raise KernelError("git_checkpoint_mismatch", "Git Checkpoint事务绑定不一致")
        self._assert_lease(worktree.plan.repository, lease)
        existing = self._store.checkpoint_for_worktree(worktree_id)
        if existing is not None:
            return existing
        index = self._git._temp / f"index-{worktree_id.hex}-{uuid4().hex}"
        repository = self._repository_root(repository_root)
        try:
            self._git.run(
                repository,
                ("read-tree", worktree.plan.repository.head_tree_oid),
                index_file=index,
            )
            for mutation in transaction.plan.mutations:
                self._verify_base_member(
                    repository, worktree.plan.repository.head_tree_oid, mutation
                )
                if mutation.after.presence == "absent":
                    self._git.run(
                        repository,
                        ("update-index", "--force-remove", "--", mutation.path),
                        index_file=index,
                    )
                else:
                    digest = mutation.after.sha256
                    mode = mutation.after.mode
                    if digest is None or mode is None:
                        raise KernelError("git_checkpoint_invalid", "Git目标文件版本不完整")
                    oid = self._git.oid(
                        repository,
                        ("hash-object", "-w", "--stdin"),
                        input_data=self._workspace_store.blob(digest),
                    )
                    self._git.run(
                        repository,
                        ("update-index", "--add", "--cacheinfo", f"{mode:o},{oid},{mutation.path}"),
                        index_file=index,
                    )
            tree = self._git.oid(repository, ("write-tree",), index_file=index)
            self._verify_tree_delta(
                repository, worktree.plan.repository.head_tree_oid, tree, transaction.plan.mutations
            )
            self._git.run(
                Path(worktree.plan.path),
                ("read-tree", "--reset", "-u", tree),
            )
            if self._git.oid(Path(worktree.plan.path), ("write-tree",)) != tree:
                raise KernelError(
                    "git_checkpoint_diverged", "受管Worktree索引与Checkpoint tree不一致"
                )
            self._verify_materialized(Path(worktree.plan.path), transaction.plan.mutations)
        finally:
            try:
                index.unlink()
            except OSError:
                pass
        identifier = checkpoint_id or uuid4()
        mutations_digest = canonical_digest(
            [item.model_dump(mode="json", warnings="error") for item in transaction.plan.mutations]
        )
        candidate = GitCheckpoint.model_construct(
            _fields_set=None,
            checkpoint_id=identifier,
            worktree_id=worktree_id,
            worktree_plan_fingerprint=worktree.plan.fingerprint,
            worktree_binding_digest=worktree.binding.digest,
            transaction_id=transaction.transaction_id,
            transaction_plan_fingerprint=transaction.plan.fingerprint,
            repository_binding_digest=worktree.plan.repository.digest,
            base_commit_oid=worktree.plan.repository.head_oid,
            base_tree_oid=worktree.plan.repository.head_tree_oid,
            tree_oid=tree,
            mutations_digest=mutations_digest,
            created_at=now or datetime.now(UTC),
            digest="0" * 64,
        )
        checkpoint = GitCheckpoint(
            **candidate.model_dump(exclude={"digest"}),
            digest=git_checkpoint_digest(candidate),
        )
        return self._store.save_checkpoint(checkpoint)

    def plan_commit(
        self,
        checkpoint_id: UUID,
        repository_root: str | Path,
        *,
        branch: str,
        author_name: str,
        author_email: str,
        message: str,
        authored_at: datetime,
        commit_id: UUID | None = None,
    ) -> GitCommitRecord:
        checkpoint = self._store.load_checkpoint(checkpoint_id)
        worktree = self._store.load_worktree(checkpoint.worktree_id)
        self._verify_repository(worktree.plan.repository, repository_root)
        repository = self._repository_root(repository_root)
        branch_ref = "refs/heads/" + branch
        self._git.run(repository, ("check-ref-format", branch_ref))
        if self._ref(repository, branch_ref) is not None:
            raise KernelError("git_branch_exists", "Git交付分支已经存在")
        normalized_message = message.rstrip("\n") + "\n"
        raw = _commit_bytes(
            checkpoint.tree_oid,
            checkpoint.base_commit_oid,
            author_name,
            author_email,
            authored_at,
            normalized_message,
        )
        expected = self._git.oid(
            repository, ("hash-object", "-t", "commit", "--stdin"), input_data=raw
        )
        identifier = commit_id or uuid4()
        candidate = GitCommitSpec.model_construct(
            _fields_set=None,
            commit_id=identifier,
            checkpoint=checkpoint,
            branch_ref=branch_ref,
            parent_oid=checkpoint.base_commit_oid,
            tree_oid=checkpoint.tree_oid,
            author_name=author_name,
            author_email=author_email,
            message=normalized_message,
            authored_at=authored_at,
            hooks_policy="disabled",
            raw_commit_sha256=hashlib.sha256(raw).hexdigest(),
            expected_commit_oid=expected,
            implementation_digest=git_delivery_implementation_digest(),
            fingerprint="0" * 64,
        )
        spec = GitCommitSpec(
            **candidate.model_dump(exclude={"fingerprint"}),
            fingerprint=git_commit_spec_fingerprint(candidate),
        )
        return self._store.save_commit(spec)

    def commit(
        self,
        commit_id: UUID,
        repository_root: str | Path,
        *,
        approval_fingerprint: str,
        lease: WorkspaceLease,
    ) -> GitCommitRecord:
        """在批准和Checkpoint仍成立时物化Commit；崩溃后依据对象与引用事实对账。"""
        record = self._store.load_commit(commit_id)
        if approval_fingerprint != record.spec.fingerprint:
            raise KernelError("git_commit_approval_mismatch", "Git Commit批准指纹不匹配")
        worktree = self._store.load_worktree(record.spec.checkpoint.worktree_id)
        self._verify_repository(worktree.plan.repository, repository_root)
        self._assert_lease(worktree.plan.repository, lease)
        if record.state == "committed":
            return self.reconcile_commit(commit_id, repository_root)
        if record.state in {"diverged", "unknown"}:
            raise KernelError("git_commit_not_executable", "Git Commit处于不可执行终态")
        if record.state == "prepared":
            record = self._advance_commit(record, "committing")
        else:
            record = self.reconcile_commit(commit_id, repository_root)
            if record.state == "committed":
                return record
            if record.state != "interrupted":
                raise KernelError("git_commit_not_executable", "Git Commit不能安全恢复")
            record = self._advance_commit(record, "committing")
        repository = self._repository_root(repository_root)
        raw = self._raw_commit(record.spec)
        if not self._object_matches(repository, record.spec.expected_commit_oid, raw):
            written = self._git.oid(
                repository,
                ("hash-object", "-t", "commit", "-w", "--stdin"),
                input_data=raw,
            )
            if written != record.spec.expected_commit_oid:
                return self._mark_commit(record, "unknown", "git_commit_object_unknown")
        _fault("commit_object_written")
        current_ref = self._ref(repository, record.spec.branch_ref)
        if current_ref is None:
            self._assert_lease(worktree.plan.repository, lease)
            zero = "0" * len(record.spec.expected_commit_oid)
            try:
                self._git.run(
                    repository,
                    (
                        "update-ref",
                        "--create-reflog",
                        "-m",
                        "harnessix delivery",
                        record.spec.branch_ref,
                        record.spec.expected_commit_oid,
                        zero,
                    ),
                )
            except KernelError:
                reconciled = self.reconcile_commit(commit_id, repository_root)
                if reconciled.state == "committed":
                    return reconciled
                raise
        elif current_ref != record.spec.expected_commit_oid:
            return self._mark_commit(record, "diverged", "git_ref_changed")
        _fault("commit_ref_updated")
        if not self._object_matches(repository, record.spec.expected_commit_oid, raw):
            return self._mark_commit(record, "unknown", "git_commit_object_unknown")
        if self._ref(repository, record.spec.branch_ref) != record.spec.expected_commit_oid:
            return self._mark_commit(record, "unknown", "git_ref_update_unknown")
        return self._advance_commit(record, "committed", commit_oid=record.spec.expected_commit_oid)

    def reconcile_commit(self, commit_id: UUID, repository_root: str | Path) -> GitCommitRecord:
        record = self._store.load_commit(commit_id)
        worktree = self._store.load_worktree(record.spec.checkpoint.worktree_id)
        self._verify_repository(worktree.plan.repository, repository_root)
        repository = self._repository_root(repository_root)
        raw = self._raw_commit(record.spec)
        current_ref = self._ref(repository, record.spec.branch_ref)
        matches = self._object_matches(repository, record.spec.expected_commit_oid, raw)
        if record.state == "committed":
            if current_ref != record.spec.expected_commit_oid or not matches:
                raise KernelError("git_commit_changed", "已提交Git ref或对象发生变化")
            return record
        if record.state in {"diverged", "unknown"}:
            return record
        if current_ref == record.spec.expected_commit_oid and matches:
            if record.state == "prepared":
                return self._mark_commit(record, "diverged", "git_commit_unowned_effect")
            return self._advance_commit(
                record, "committed", commit_oid=record.spec.expected_commit_oid
            )
        if current_ref is not None:
            return self._mark_commit(record, "diverged", "git_ref_changed")
        if record.state == "prepared":
            return record
        if record.state == "interrupted":
            return record
        return self._advance_commit(record, "interrupted")

    def _repository_root(self, supplied: str | Path) -> Path:
        try:
            path = Path(supplied)
            if not path.is_absolute() or any(ord(character) < 32 for character in str(path)):
                raise OSError
            root = path.resolve(strict=True)
            if not root.is_dir() or root.is_symlink():
                raise OSError
            reported = self._text(
                self._git.run(root, ("rev-parse", "--show-toplevel")).stdout
            ).strip()
            if _path_text(Path(reported).resolve(strict=True)) != _path_text(root):
                raise OSError
            return root
        except (OSError, RuntimeError):
            raise KernelError("git_repository_invalid", "路径不是精确Git仓库根") from None

    def _common_directory(self, repository: Path) -> Path:
        value = self._text(
            self._git.run(repository, ("rev-parse", "--git-common-dir")).stdout
        ).strip()
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = repository / candidate
        try:
            return candidate.resolve(strict=True)
        except OSError:
            raise KernelError("git_repository_invalid", "Git common directory无效") from None

    def _reject_unsafe_configuration(self, repository: Path) -> None:
        included = self._git.run(
            repository,
            ("config", "--local", "--no-includes", "--null", "--get-regexp", r"^include(If)?\."),
            accepted=(0, 1),
        ).stdout
        if included:
            raise KernelError("git_config_unsupported", "Git本地外部include配置在0.7不受支持")
        filters = self._git.run(
            repository,
            (
                "config",
                "--includes",
                "--null",
                "--get-regexp",
                r"^filter\..*\.(clean|smudge|process)$",
            ),
            accepted=(0, 1),
        ).stdout
        if filters:
            raise KernelError("git_filter_unsupported", "Git可执行filter在0.7不受支持")
        sparse = self._git.run(
            repository,
            ("config", "--bool", "--get", "core.sparseCheckout"),
            accepted=(0, 1),
        ).stdout.strip()
        if sparse == b"true":
            raise KernelError("git_sparse_checkout_unsupported", "Git sparse checkout在0.7不受支持")
        tree = self._git.run(repository, ("ls-tree", "-r", "-z", "--full-tree", "HEAD")).stdout
        for record in tree.split(b"\0"):
            if not record:
                continue
            try:
                metadata, raw_path = record.split(b"\t", 1)
                mode, kind, oid = metadata.decode("ascii").split(" ", 2)
                path = raw_path.decode("utf-8", errors="strict")
            except (UnicodeError, ValueError):
                raise KernelError("git_tree_invalid", "Git tree条目无法解析") from None
            name = path.rsplit("/", 1)[-1].casefold()
            if mode == "160000" or kind == "commit" or name in {".gitmodules", ".lfsconfig"}:
                raise KernelError("git_tree_unsupported", "Git submodule或LFS控制面在0.7不受支持")
            if name == ".gitattributes":
                body = self._git.run(repository, ("cat-file", "blob", oid)).stdout
                lowered = body.lower()
                if b"filter" in lowered or b"working-tree-encoding" in lowered:
                    raise KernelError("git_attributes_unsupported", "Git attributes包含转换规则")

    def _verify_repository(self, expected: GitRepositoryBinding, root: str | Path) -> None:
        actual = self.bind_repository(root, expected.workspace_id)
        if actual != expected:
            raise KernelError("git_repository_changed", "Git仓库绑定已经变化")

    def _registered_worktrees(self, repository: Path) -> set[str]:
        output = self._git.run(repository, ("worktree", "list", "--porcelain")).stdout
        result: set[str] = set()
        for line in output.splitlines():
            if line.startswith(b"worktree "):
                result.add(_path_text(Path(self._text(line[9:])).absolute()))
        return result

    def _capture_worktree_binding(self, plan: ManagedGitWorktreePlan) -> ManagedGitWorktreeBinding:
        path = Path(plan.path)
        gitfile = path / ".git"
        try:
            info = gitfile.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size > 4096:
                raise OSError
            body = gitfile.read_bytes()
            prefix = b"gitdir: "
            if not body.startswith(prefix):
                raise OSError
            admin = Path(self._text(body[len(prefix) :]).strip())
            if not admin.is_absolute():
                admin = gitfile.parent / admin
            admin = admin.resolve(strict=True)
            common = self._common_directory(
                self._repository_root_from_binding(plan.repository, path)
            )
            if admin.parent != common / "worktrees":
                raise OSError
            commondir = self._text((admin / "commondir").read_bytes()).strip()
            resolved_common = (admin / commondir).resolve(strict=True)
            backlink = (admin / "gitdir").read_bytes()
            backlink_path = Path(self._text(backlink).strip()).resolve(strict=True)
            if resolved_common != common or backlink_path != gitfile.resolve(strict=True):
                raise OSError
        except (OSError, RuntimeError, UnicodeError):
            raise KernelError("git_worktree_binding_invalid", "受管Git Worktree回链无效") from None
        head = self._git.oid(path, ("rev-parse", "--verify", "HEAD^{commit}"))
        if head != plan.repository.head_oid:
            raise KernelError("git_worktree_binding_invalid", "受管Git Worktree HEAD不一致")
        candidate = ManagedGitWorktreeBinding.model_construct(
            _fields_set=None,
            worktree_id=plan.worktree_id,
            plan_fingerprint=plan.fingerprint,
            path_identity=_identity(path, directory=True),
            gitfile_sha256=hashlib.sha256(body).hexdigest(),
            admin_directory_sha256=_path_sha256(admin),
            admin_directory_identity=_identity(admin, directory=True),
            backlink_sha256=hashlib.sha256(backlink).hexdigest(),
            head_oid=head,
            digest="0" * 64,
        )
        return ManagedGitWorktreeBinding(
            **candidate.model_dump(exclude={"digest"}),
            digest=managed_git_worktree_binding_digest(candidate),
        )

    def _repository_root_from_binding(
        self, binding: GitRepositoryBinding, worktree_path: Path
    ) -> Path:
        common = self._common_directory(worktree_path)
        for candidate in (common.parent, common):
            try:
                root = self._repository_root(candidate)
            except KernelError:
                continue
            if _path_sha256(root) == binding.root_path_sha256:
                return root
        raise KernelError("git_repository_changed", "无法从受管Worktree恢复来源仓库")

    def _verify_worktree(self, record: ManagedGitWorktreeRecord) -> None:
        if record.binding is None or self._capture_worktree_binding(record.plan) != record.binding:
            raise KernelError("git_worktree_changed", "受管Git Worktree身份已经变化")

    def _authorize_worktree(
        self,
        record: ManagedGitWorktreeRecord,
        repository_root: str | Path,
        approval_fingerprint: str,
        lease: WorkspaceLease,
    ) -> None:
        if approval_fingerprint != record.plan.transaction_plan_fingerprint:
            raise KernelError("git_worktree_approval_mismatch", "Git Worktree批准指纹不匹配")
        self._verify_repository(record.plan.repository, repository_root)
        self._assert_lease(record.plan.repository, lease)

    def _assert_lease(self, binding: GitRepositoryBinding, lease: WorkspaceLease) -> None:
        if lease.workspace_id != binding.workspace_id:
            raise KernelError("workspace_lease_lost", "Git交付租约不属于来源Workspace")
        self._leases.assert_current(lease)

    def _verify_base_member(self, repository: Path, tree: str, mutation: WorkspaceMutation) -> None:
        output = self._git.run(repository, ("ls-tree", "-z", tree, "--", mutation.path)).stdout
        if mutation.before.presence == "absent":
            if output:
                raise KernelError("git_checkpoint_mismatch", "Git基准路径应当缺失")
            return
        try:
            metadata, raw_path = output.removesuffix(b"\0").split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split(" ", 2)
            path = raw_path.decode("utf-8", errors="strict")
        except (UnicodeError, ValueError):
            raise KernelError("git_checkpoint_mismatch", "Git基准tree条目无效") from None
        expected_mode = mutation.before.mode
        expected_digest = mutation.before.sha256
        if (
            path != mutation.path
            or kind != "blob"
            or expected_mode is None
            or mode != f"100{expected_mode:o}"[-6:]
            or expected_digest is None
        ):
            raise KernelError("git_checkpoint_mismatch", "Git基准tree与Workspace版本不一致")
        body = self._git.run(repository, ("cat-file", "blob", oid)).stdout
        if hashlib.sha256(body).hexdigest() != expected_digest or len(body) != mutation.before.size:
            raise KernelError("git_checkpoint_mismatch", "Git基准blob与Workspace版本不一致")

    def _verify_tree_delta(
        self, repository: Path, before: str, after: str, mutations: tuple[WorkspaceMutation, ...]
    ) -> None:
        output = self._git.run(
            repository,
            (
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "--no-renames",
                "-r",
                "-z",
                before,
                after,
            ),
        ).stdout
        try:
            paths = tuple(
                item.decode("utf-8", errors="strict") for item in output.split(b"\0") if item
            )
        except UnicodeError:
            raise KernelError("git_checkpoint_mismatch", "Git tree路径不是有效UTF-8") from None
        expected = tuple(item.path for item in mutations)
        if tuple(sorted(paths)) != tuple(sorted(expected)) or len(paths) != len(expected):
            raise KernelError("git_checkpoint_mismatch", "Git Checkpoint包含计划外路径")

    def _verify_materialized(self, root: Path, mutations: tuple[WorkspaceMutation, ...]) -> None:
        for mutation in mutations:
            path = root / Path(*mutation.path.split("/"))
            if mutation.after.presence == "absent":
                if path.exists() or path.is_symlink():
                    raise KernelError("git_checkpoint_diverged", "Git删除路径仍然存在")
                continue
            try:
                info = path.lstat()
                body = path.read_bytes()
            except OSError:
                raise KernelError("git_checkpoint_diverged", "Git目标文件无法读取") from None
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise KernelError("git_checkpoint_diverged", "Git目标不是普通文件")
            if (
                mutation.after.sha256 != hashlib.sha256(body).hexdigest()
                or mutation.after.size != len(body)
                or (os.name == "posix" and mutation.after.mode != stat.S_IMODE(info.st_mode))
            ):
                raise KernelError("git_checkpoint_diverged", "Git目标文件与Checkpoint不一致")

    def _raw_commit(self, spec: GitCommitSpec) -> bytes:
        raw = _commit_bytes(
            spec.tree_oid,
            spec.parent_oid,
            spec.author_name,
            spec.author_email,
            spec.authored_at,
            spec.message,
        )
        if hashlib.sha256(raw).hexdigest() != spec.raw_commit_sha256:
            raise KernelError("git_commit_spec_invalid", "Git Commit原始对象摘要不一致")
        return raw

    def _object_matches(self, repository: Path, oid: str, expected: bytes) -> bool:
        result = self._git.run(
            repository, ("cat-file", "-e", oid + "^{commit}"), accepted=(0, 1, 128)
        )
        if result.returncode != 0:
            return False
        return self._git.run(repository, ("cat-file", "commit", oid)).stdout == expected

    def _ref(self, repository: Path, ref: str) -> str | None:
        result = self._git.run(repository, ("rev-parse", "--verify", ref), accepted=(0, 1, 128))
        if result.returncode != 0:
            return None
        if _OID.fullmatch(result.stdout) is None:
            raise KernelError("git_output_invalid", "Git ref OID输出无效")
        return result.stdout.decode("ascii").strip()

    @staticmethod
    def _text(body: bytes) -> str:
        try:
            return body.decode("utf-8", errors="strict")
        except UnicodeError:
            raise KernelError("git_output_invalid", "Git输出不是有效UTF-8") from None

    def _advance_worktree(
        self,
        record: ManagedGitWorktreeRecord,
        state: GitWorktreeState,
        *,
        binding: ManagedGitWorktreeBinding | None = None,
        error_code: str | None = None,
    ) -> ManagedGitWorktreeRecord:
        updated = transition_worktree_record(
            record,
            state=state,
            now=datetime.now(UTC),
            binding=binding,
            error_code=error_code,
        )
        self._store.transition_worktree(record, updated)
        return updated

    def _mark_worktree(
        self, record: ManagedGitWorktreeRecord, state: GitWorktreeState, error: str
    ) -> ManagedGitWorktreeRecord:
        return self._advance_worktree(
            record,
            state,
            binding=None if state != "ready" else record.binding,
            error_code=error,
        )

    def _advance_commit(
        self,
        record: GitCommitRecord,
        state: GitCommitState,
        *,
        commit_oid: str | None = None,
        error_code: str | None = None,
    ) -> GitCommitRecord:
        updated = transition_commit_record(
            record,
            state=state,
            now=datetime.now(UTC),
            commit_oid=commit_oid,
            error_code=error_code,
        )
        self._store.transition_commit(record, updated)
        return updated

    def _mark_commit(
        self, record: GitCommitRecord, state: GitCommitState, error: str
    ) -> GitCommitRecord:
        return self._advance_commit(record, state, error_code=error)


def _commit_bytes(
    tree_oid: str,
    parent_oid: str,
    author_name: str,
    author_email: str,
    authored_at: datetime,
    message: str,
) -> bytes:
    if authored_at.tzinfo is None:
        raise KernelError("git_commit_time_invalid", "Git Commit时间必须包含时区")
    offset = authored_at.utcoffset()
    if offset is None or offset.total_seconds() % 60:
        raise KernelError("git_commit_time_invalid", "Git Commit时区偏移无效")
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    absolute = abs(minutes)
    timezone = f"{sign}{absolute // 60:02d}{absolute % 60:02d}"
    try:
        return (
            f"tree {tree_oid}\n"
            f"parent {parent_oid}\n"
            f"author {author_name} <{author_email}> {int(authored_at.timestamp())} {timezone}\n"
            f"committer {author_name} <{author_email}> {int(authored_at.timestamp())} {timezone}\n"
            f"\n{message}"
        ).encode("utf-8", errors="strict")
    except UnicodeError:
        raise KernelError("git_commit_invalid", "Git Commit内容不是有效UTF-8") from None
