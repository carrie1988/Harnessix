"""从原认证Patch来源读取固定Git基准；不暂存、提交或改变用户Ref。"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread
from harnessix.delivery.contracts import MAX_TRANSACTION_FILE_BYTES, WorkspaceMutation
from harnessix.delivery.git_authentication_control import io_git_authentication
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.processes.git_observation import GitBaselineReadResult
from harnessix.product_config.git_baseline_contracts import (
    GitBaselineMember,
    ProductGitDeliveryBaseline,
    product_git_baseline_digest,
)
from harnessix.product_config.git_delivery_source import (
    _verify_final_snapshot,
    collect_git_delivery_source,
)
from harnessix.product_config.git_parent_contracts import (
    ProductGitDeliveryBaselineV2,
    ProductGitDeliverySourceV2,
)
from harnessix.product_config.workspace_patch_source_contracts import ProductGitDeliverySource
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.git import GitReadRuntime, _git_helper_key, _reject_git_helpers
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.snapshot import verify_workspace_snapshot
from harnessix.workspace.snapshot_capture import capture_snapshot_facts
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts

_OID = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\n")
_UNSAFE_CONFIG = re.compile(
    rb"^(?:core\.sparsecheckout|extensions\.partialclone|remote\..*\.promisor)$"
)
_BASELINE_TIMEOUT_SECONDS = 60.0


def _reject(code: str) -> KernelError:
    """固定公开消息不包含路径、命令输出、配置值或第三方异常。"""
    return KernelError(code, "Git交付基准不满足原来源、索引或完整观察要求")


class _Queries:
    """仅接收模块内部固定查询；复用原端口的取消、期限和完整流证据。"""

    def __init__(
        self,
        reader: GitReadRuntime,
        cancel: CancelToken,
        checkpoint: Callable[[], None] | None = None,
    ) -> None:
        self.reader, self.cancel = reader, cancel
        self.checkpoint = checkpoint or cancel.checkpoint

    async def result(self, *arguments: str) -> GitBaselineReadResult:
        self.checkpoint()
        result = await self.reader._run_baseline(  # noqa: SLF001 - 固定内部查询不暴露给模型
            (*self.reader._global_arguments, *arguments), self.cancel
        )
        self.checkpoint()
        return result

    async def full(self, *arguments: str) -> bytes:
        result = await self.result(*arguments)
        return _full_stdout(result)

    async def oid(self, *arguments: str) -> str:
        output = await self.full(*arguments)
        if _OID.fullmatch(output) is None:
            raise _reject("git_baseline_output_invalid")
        return output[:-1].decode("ascii")


@dataclass(frozen=True)
class _Observation:
    """只保存Repo和完整逻辑Index摘要，不保留无关Index正文。"""

    head: str
    tree: str
    ref: str
    index_sha256: str
    index_bytes: int
    status_sha256: str
    config_sha256: str


def _full_stdout(result: GitBaselineReadResult) -> bytes:
    try:
        return result.full_stdout()
    except ReadToolError as error:
        if error.code == "limit_exceeded":
            raise _reject("git_baseline_limit") from None
        raise


async def _observe(query: _Queries) -> _Observation:
    config = await query.result("config", "--no-includes", "--null", "--name-only", "--list")
    if any(
        _git_helper_key(key) or _UNSAFE_CONFIG.fullmatch(key.lower())
        for key in _full_stdout(config).split(b"\0")
    ):
        raise _reject("git_baseline_config_unsupported")
    head = await query.oid("rev-parse", "--verify", "HEAD^{commit}")
    # 树从刚取得的固定Commit派生，避免两次HEAD读取跨越外部提交。
    tree = await query.oid("rev-parse", "--verify", head + "^{tree}")
    ref_bytes = await query.full("rev-parse", "--symbolic-full-name", "HEAD")
    if not ref_bytes.endswith(b"\n") or ref_bytes.count(b"\n") != 1:
        raise _reject("git_baseline_output_invalid")
    ref = ref_bytes[:-1].decode("utf-8")
    index = await query.result("ls-files", "--stage", "--debug", "-z")
    if index.raw_stdout.observed_bytes > MAX_TRANSACTION_FILE_BYTES:
        raise _reject("git_baseline_limit")
    status = await query.result(
        "status", "--porcelain=v2", "--untracked-files=all", "--ignore-submodules=all", "-z"
    )
    if status.result.stdout.truncated:
        raise _reject("git_baseline_limit")
    return _Observation(
        head,
        tree,
        ref,
        index.raw_stdout.sha256,
        index.raw_stdout.observed_bytes,
        status.raw_stdout.sha256,
        config.raw_stdout.sha256,
    )


def _tree_member(raw: bytes, mutation: WorkspaceMutation, oid_length: int) -> GitBaselineMember:
    """严格解析NUL条目；不把目录、链接或子模块当作普通文件。"""
    if mutation.before.presence == "absent":
        if raw:
            raise _reject("git_baseline_before_mismatch")
        return GitBaselineMember(path=mutation.path)
    try:
        if not raw.endswith(b"\0") or raw.count(b"\0") != 1:
            raise ValueError
        metadata, path = raw[:-1].split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split(" ")
        if (
            path.decode("utf-8") != mutation.path
            or kind != "blob"
            or mode not in {"100644", "100755"}
            or len(oid) != oid_length
            or re.fullmatch(r"[0-9a-f]+", oid) is None
            or int(mode[-3:], 8) != mutation.before.mode
        ):
            raise ValueError
        return GitBaselineMember(
            path=mutation.path, oid=oid, mode=cast(Literal["100644", "100755"], mode)
        )
    except (UnicodeError, ValueError):
        raise _reject("git_baseline_before_mismatch") from None


async def _member(query: _Queries, tree: str, mutation: WorkspaceMutation) -> GitBaselineMember:
    member = _tree_member(
        await query.full("ls-tree", "-z", "--full-tree", tree, "--", mutation.path),
        mutation,
        len(tree),
    )
    index = await query.full("ls-files", "--stage", "-z", "--", mutation.path)
    expected = (
        b"" if member.oid is None else f"{member.mode} {member.oid} 0\t{member.path}\0".encode()
    )
    if index != expected:
        raise _reject("git_baseline_index_conflict")
    flags = await query.full("ls-files", "-v", "-z", "--", mutation.path)
    if flags != (b"" if member.oid is None else f"H {member.path}\0".encode()):
        raise _reject("git_baseline_index_conflict")
    debug = await query.full("ls-files", "--debug", "-z", "--", mutation.path)
    if member.oid is None:
        valid_flags = not debug
    else:
        flag_values = re.findall(rb"\tflags: ([0-9a-f]+)\n", debug)
        valid_flags = (
            debug.startswith(member.path.encode() + b"\0")
            and len(flag_values) == 1
            and int(flag_values[0], 16) == 0
        )
    if not valid_flags:
        raise _reject("git_baseline_index_conflict")
    if member.oid is not None:
        result = await query.result("cat-file", "blob", member.oid)
        if result.raw_stdout.observed_bytes > MAX_TRANSACTION_FILE_BYTES:
            raise _reject("git_baseline_limit")
        if (
            result.raw_stdout.observed_bytes != mutation.before.size
            or result.raw_stdout.sha256 != mutation.before.sha256
        ):
            raise _reject("git_baseline_before_mismatch")
    return member


async def collect_product_git_baseline(
    thread: Thread,
    targets: tuple[UUID, ...],
    router: TrustedActionRouter,
    transactions: SQLiteWorkspaceTransactionStore,
    reader: GitReadRuntime,
    *,
    cancel: CancelToken,
    snapshot_ports: WorkspaceSnapshotPorts | None = None,
) -> ProductGitDeliveryBaseline:
    """先验原认证归属；精确观察后再次复核，拒绝任何漂移而非自动修复。"""
    deadline = time.monotonic() + _BASELINE_TIMEOUT_SECONDS

    def checkpoint() -> None:
        cancel.checkpoint()
        if time.monotonic() >= deadline:
            raise _reject("git_baseline_timeout")

    source = collect_git_delivery_source(
        thread, targets, router, transactions, checkpoint=checkpoint, snapshot_ports=snapshot_ports
    )
    return await _collect_baseline_from_source(
        source,
        thread,
        reader,
        cancel=cancel,
        snapshot_ports=snapshot_ports,
        checkpoint=checkpoint,
        deadline=deadline,
    )


def _root_binding_matches(
    source: ProductGitDeliverySource | ProductGitDeliverySourceV2,
    root: Path,
    checkpoint: Callable[[], None],
) -> bool:
    """原生只读根捕获是 I/O 段；内部逐项消费父取消、共同期限和原本地控制。"""
    with io_git_authentication(checkpoint) as progress:

        def controlled() -> None:
            try:
                progress()
            except UpstreamCheckpointError:
                raise
            except BaseException as error:
                raise UpstreamCheckpointError(error) from None

        # 段入口/成功出口在此 try 外；只有原捕获端口沿旧边界解包上游异常。
        try:
            facts = capture_snapshot_facts(
                root,
                cwd=".",
                resources=(),
                external_roots=None,
                platform=source.workspace.platform,
                checkpoint=controlled,
            )
        except UpstreamCheckpointError as error:
            raise error.error from None
    return all(
        facts.scope[name] == getattr(source.workspace, name)
        for name in ("workspace_id", "root_path_digest", "root_identity")
    )


async def _collect_baseline_from_source(
    source: ProductGitDeliverySource | ProductGitDeliverySourceV2,
    thread: Thread,
    reader: GitReadRuntime,
    *,
    cancel: CancelToken,
    snapshot_ports: WorkspaceSnapshotPorts | None,
    checkpoint: Callable[[], None],
    deadline: float,
) -> ProductGitDeliveryBaseline | ProductGitDeliveryBaselineV2:
    """唯一基准算法消费已验真来源；新产品观察不重复捕获或放宽旧合同。"""
    checkpoint()
    contract = reader.contract()
    if contract["implementation"] != "git-baseline-read/v1":
        raise _reject("git_baseline_reader_required")
    # 原端口只验证自己的仓库根；还必须与认证Thread的原生根身份绑定。
    if not _root_binding_matches(source, reader._root, checkpoint):
        raise _reject("git_baseline_workspace_mismatch")
    query = _Queries(reader, cancel, checkpoint)
    try:
        async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
            await reader._require_repository_root(cancel)  # noqa: SLF001 - 原根核验前置
            await _reject_git_helpers(reader, cancel)
            before = await _observe(query)
            members = []
            for mutation in source.mutations:
                checkpoint()
                members.append(await _member(query, before.tree, mutation))
            if await _observe(query) != before:
                raise _reject("git_baseline_changed")
            checkpoint()
            if isinstance(source, ProductGitDeliverySourceV2):
                _verify_final_snapshot(
                    source.workspace, Path(thread.workspace), checkpoint, snapshot_ports
                )
            else:
                verify_workspace_snapshot(source.workspace, Path(thread.workspace))
            checkpoint()
    except TimeoutError:
        raise _reject("git_baseline_timeout") from None
    except (ReadToolError, UnicodeError):
        raise _reject("git_baseline_unavailable") from None
    model = (
        ProductGitDeliveryBaselineV2
        if isinstance(source, ProductGitDeliverySourceV2)
        else ProductGitDeliveryBaseline
    )
    candidate = model.model_construct(
        source=source,
        head_oid=before.head,
        head_tree_oid=before.tree,
        head_ref=before.ref,
        index_observation_sha256=before.index_sha256,
        index_observation_bytes=before.index_bytes,
        status_sha256=before.status_sha256,
        config_names_sha256=before.config_sha256,
        reader_binding=contract["binding"],
        members=tuple(members),
        digest="0" * 64,
    )
    result = model(
        **candidate.model_dump(exclude={"digest"}), digest=product_git_baseline_digest(candidate)
    )
    checkpoint()
    return result
