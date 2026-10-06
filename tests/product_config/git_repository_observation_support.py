"""仓库观察测试的真实共享宿主 fixture 与只读认证证据 helper。"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.delivery.git_contracts import GitRepositoryBinding
from harnessix.execution.contracts import ExecutionApprovalCheckpoint, ExecutionPlanV2
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.supervisor import PosixProcessSupervisor, WindowsProcessSupervisor
from harnessix.product_config.git_delivery_process import (
    GitDeliveryProcess,
    GitOperationBudget,
    PreparedGitProcess,
)
from harnessix.product_config.git_process_host import GitProcessRuntimeHost
from harnessix.product_config.git_repository_observation import (
    GitRepositoryReadAuthorization,
    observe_product_git_repository,
)
from harnessix.product_config.state_owner import product_state_owner, state_owner_anchor
from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
from harnessix.secrets.publication import SecretPublicationScope
from tests.product_config import test_git_delivery_process as original
from tests.product_config import test_git_material_input as inputs
from tests.product_config import test_git_shared_process_host as shared

make_process = shared.make_process


@dataclass(frozen=True)
class _AuthorizedRead:
    prepared: PreparedGitProcess
    plan: ExecutionPlanV2
    approval: ExecutionApprovalCheckpoint | None


@dataclass
class _SharedRepository:
    case: original._ProcessCase
    host: GitProcessRuntimeHost
    expected: GitRepositoryBinding
    authorized: list[_AuthorizedRead] = field(default_factory=list)
    checkpoints: list[int] = field(default_factory=list)

    def approve(self, prepared: PreparedGitProcess) -> _AuthorizedRead:
        # 调用方使用原正式 V2 构建器；适配器不能自产 ALLOW 或补造批准。
        plan = original._plan(self.case, prepared)
        record = _AuthorizedRead(prepared, plan, original._checkpoint(plan))
        self.authorized.append(record)
        return record


@pytest.fixture
async def shared_repository(make_process, tmp_path: Path, request: pytest.FixtureRequest):
    case = make_process()
    mode = getattr(request, "param", "sha1")
    inputs._repository(case, tmp_path, "sha256" if mode == "sha256" else "sha1")
    # 安全 attributes 强制完整检查包含真实 cat-file 回读，而非只有空配置路径。
    (case.workspace / ".gitattributes").write_bytes(b"*.txt text\n")
    nested = case.workspace / "nested"
    nested.mkdir()
    (nested / ".gitattributes").write_bytes(b"*.dat -text\n")
    (nested / "sample.dat").write_bytes(b"\x00\xff\r\n")
    case.runner.run(case.workspace, ("add", "--", ".gitattributes", "nested"))
    case.runner.run(case.workspace, ("commit", "-qm", "safe attributes"))
    await case.port.aclose()
    # 同步入口的原 Runner 直接借给异步端口，避免不同 hooksPath 改变配置原始字节。
    with (
        inputs.SQLiteWorkspaceTransactionStore(tmp_path / "binding-workspace") as workspace,
        inputs.SQLiteGitDeliveryStore(tmp_path / "binding-git") as git,
        inputs.WorkspaceLeaseStore(tmp_path / "binding-leases.db") as leases,
    ):
        runtime = inputs.GitDeliveryRuntime(workspace, git, leases, case.runner.path)
        case.runner = runtime._git
        expected = runtime.bind_repository(case.workspace, "d" * 64)
    if mode == "protected-root":
        source = EnvironmentSecretSource("repository-canary", "v1", "REPOSITORY_CANARY")
        protection = SecretPublicationScope(
            (source,),
            EnvironmentSecretProvider(
                (source,), environment={"REPOSITORY_CANARY": str(case.workspace)}
            ),
        )
    else:
        protection = SecretPublicationScope((), {})
    supervisor_type = WindowsProcessSupervisor if os.name == "nt" else PosixProcessSupervisor
    try:
        with (
            product_state_owner(case.state) as owner,
            SQLiteExecutionPlanStore(case.state / "execution-plans.db") as plans,
        ):
            async with supervisor_type(
                case.state / "process-owner", output_redaction=protection
            ) as supervisor:
                host = GitProcessRuntimeHost(owner, supervisor, plans, protection)
                case.port = GitDeliveryProcess(
                    case.runner, case.state, output_redaction=protection, runtime_host=host
                )
                try:
                    yield _SharedRepository(case, host, expected)
                finally:
                    await case.port.aclose()
    finally:
        protection.close()


def _source_snapshot(root: Path) -> tuple:
    """仅观察临时 U 仓库；包含 HEAD、index、对象、配置和工作文件字节及写时间。"""
    result = []
    for path in (root, *sorted(root.rglob("*"))):
        stat = path.lstat()
        body = (
            os.readlink(path)
            if path.is_symlink()
            else path.read_bytes()
            if path.is_file()
            else None
        )
        result.append(
            (
                path.relative_to(root).as_posix(),
                stat.st_mode,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                body,
            )
        )
    return tuple(result)


def _assert_settled(repository: _SharedRepository, count: int | None = None) -> None:
    case, host = repository.case, repository.host
    records = repository.authorized if count is None else repository.authorized[:count]
    assert shared._rows(case.state) == (len(records), len(records))
    assert len(host.supervisor._handles) == len(records)
    assert not host.supervisor._store.active()
    for record in records:
        prepared, plan = record.prepared, record.plan
        handle = host.supervisor._handles[prepared.spec.process_id]
        lease = original._lease(case, prepared)
        receipt = original._receipt(case, prepared)
        assert handle.lease == lease
        assert host.plans.load_plan(plan.plan_id) == plan
        assert lease.plan_id == plan.plan_id and lease.plan_fingerprint == plan.fingerprint
        assert plan.intent.arguments == prepared.approval_arguments()
        assert plan.policy.decision == original.PolicyDecisionKind.REQUIRE_APPROVAL
        assert record.approval is not None
        assert record.approval.plan_id == plan.plan_id
        stdout = (
            case.state / "process-owner/runs" / str(lease.process_id) / "stdout.bin"
        ).read_bytes()
        stderr = (
            case.state / "process-owner/runs" / str(lease.process_id) / "stderr.bin"
        ).read_bytes()
        original._assert_completion(
            case,
            prepared,
            original.GitProcessCompletion(lease, receipt, stdout, stderr),
            stdout,
            stderr,
        )


async def _observe(
    repository: _SharedRepository,
    *,
    authorize: Callable[[PreparedGitProcess], Awaitable[GitRepositoryReadAuthorization]]
    | None = None,
    cancel: CancelToken | None = None,
    budget: GitOperationBudget | None = None,
    checkpoint: Callable[[], None] | None = None,
    root: Path | None = None,
) -> GitRepositoryBinding:
    operation_budget = budget if budget is not None else GitOperationBudget(45)

    async def approved(prepared: PreparedGitProcess) -> GitRepositoryReadAuthorization:
        assert prepared.budget is operation_budget
        record = repository.approve(prepared)
        return GitRepositoryReadAuthorization(record.plan, record.approval)

    def checked() -> None:
        repository.checkpoints.append(len(repository.host.supervisor._handles))
        if checkpoint is not None:
            checkpoint()

    return await observe_product_git_repository(
        repository.case.port,
        repository.case.workspace if root is None else root,
        repository.expected.workspace_id,
        authorize=approved if authorize is None else authorize,
        cancel=CancelToken() if cancel is None else cancel,
        budget=operation_budget,
        checkpoint=checked,
    )


def _assert_no_new_process(repository: _SharedRepository, count: int = 0) -> None:
    assert shared._rows(repository.case.state) == (count, count)
    supervisor = repository.host.supervisor
    # 原 Supervisor 关闭时会清空内存句柄；持久化 Lease/receipt 才是累计执行事实。
    assert len(supervisor._handles) == (0 if supervisor._closed else count)
    with original.SQLiteProcessLeaseStore(
        repository.case.state / "process-owner/process-leases.db", read_only=True
    ) as leases:
        assert not leases.active()


_UNSAFE_REPOSITORIES = [
    ("include", "git_config_unsupported"),
    ("include-if", "git_config_unsupported"),
    ("filter-clean", "git_filter_unsupported"),
    ("filter-smudge", "git_filter_unsupported"),
    ("filter-process", "git_filter_unsupported"),
    ("sparse", "git_sparse_checkout_unsupported"),
    ("submodule", "git_tree_unsupported"),
    ("gitmodules", "git_tree_unsupported"),
    ("lfsconfig", "git_tree_unsupported"),
    ("attributes-filter", "git_attributes_unsupported"),
    ("attributes-encoding", "git_attributes_unsupported"),
    ("dirty-worktree", "delivery_dirty_conflict"),
    ("dirty-index", "delivery_dirty_conflict"),
    ("untracked", "delivery_dirty_conflict"),
    ("alternates", "git_alternates_unsupported"),
    ("dangling-alternates", "git_alternates_unsupported"),
]


def _make_unsafe(repository: _SharedRepository, fault: str, tmp_path: Path) -> None:
    case = repository.case
    root = case.workspace
    if fault in {"include", "include-if"}:
        key = "include.path" if fault == "include" else "includeIf.gitdir:*/.path"
        case.runner.run(root, ("config", key, str(tmp_path / "external.cfg")))
    elif fault.startswith("filter-"):
        case.runner.run(root, ("config", "filter.test." + fault.removeprefix("filter-"), "cat"))
    elif fault == "sparse":
        case.runner.run(root, ("config", "core.sparseCheckout", "true"))
    elif fault in {"dirty-worktree", "dirty-index"}:
        (root / "file.txt").write_bytes(b"dirty\n")
        if fault == "dirty-index":
            case.runner.run(root, ("add", "--", "file.txt"))
    elif fault == "untracked":
        (root / "untracked.txt").write_bytes(b"untracked\n")
    elif fault in {"alternates", "dangling-alternates"}:
        alternates = root / ".git/objects/info/alternates"
        if fault == "alternates":
            alternates.write_bytes(b"")
        else:
            if os.name == "nt":
                pytest.skip("Windows 普通权限不保证可创建符号链接；普通 alternates 仍必测")
            alternates.symlink_to(tmp_path / "missing-alternates")
    else:
        if fault == "submodule":
            head = repository.expected.head_oid
            case.runner.run(root, ("update-index", "--add", "--cacheinfo", f"160000,{head},module"))
        else:
            paths = {
                "gitmodules": (root / ".gitmodules", b""),
                "lfsconfig": (root / "nested/.lfsconfig", b"[lfs]\n"),
                "attributes-filter": (root / ".gitattributes", b"*.txt FILTER=unconfigured\n"),
                "attributes-encoding": (
                    root / "nested/.gitattributes",
                    b"*.dat working-tree-encoding=UTF-8\n",
                ),
            }
            path, body = paths[fault]
            path.write_bytes(body)
            case.runner.run(root, ("add", "--", path.relative_to(root).as_posix()))
        case.runner.run(root, ("commit", "-qm", "unsafe repository fixture"))


async def _invalidate_host(repository: _SharedRepository, fault: str) -> None:
    case, host = repository.case, repository.host
    if fault == "owner":
        host.owner._active = False
    elif fault == "state-root":
        case.port._state = case.state / "other"
    elif fault == "plan-root":
        host.plans._path = case.state / "other.db"
    elif fault == "supervisor-root":
        host.supervisor._root = case.state / "other-owner"
    elif fault == "closed-plans":
        host.plans.close()
    elif fault == "closed-supervisor":
        await host.supervisor.aclose()
    elif fault == "closed-scope":
        host.protection.close()
    elif fault == "output-source":
        case.port._output_redaction = None
    elif fault == "host-replaced":
        case.port._runtime_host = GitProcessRuntimeHost(
            host.owner, host.supervisor, host.plans, host.protection
        )
    else:
        (state_owner_anchor(case.state) / "restore-active.json").write_text("{}")


_HOST_FAILURES = [
    ("owner", "product_state_owner_invalid"),
    ("state-root", "git_process_runtime_mismatch"),
    ("plan-root", "git_process_runtime_mismatch"),
    ("supervisor-root", "git_process_runtime_mismatch"),
    ("closed-plans", "git_process_runtime_mismatch"),
    ("closed-supervisor", "git_process_runtime_mismatch"),
    ("closed-scope", "trusted_action_secret_unavailable"),
    ("output-source", "git_process_runtime_mismatch"),
    ("host-replaced", "git_process_runtime_mismatch"),
    ("restore-pending", "product_state_restore_pending"),
]
