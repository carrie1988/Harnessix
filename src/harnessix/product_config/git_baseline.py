"""从原认证Patch来源读取固定Git基准；不暂存、提交或改变用户Ref。"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread
from harnessix.delivery.contracts import MAX_TRANSACTION_FILE_BYTES, WorkspaceMutation
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.processes.contracts import ProcessResult
from harnessix.product_config.git_baseline_contracts import (
    GitBaselineMember,
    ProductGitDeliveryBaseline,
    product_git_baseline_digest,
)
from harnessix.product_config.git_delivery_source import collect_git_delivery_source
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.git import GitReadRuntime, _git_helper_key, _reject_git_helpers
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.workspace.snapshot import capture_workspace_snapshot, verify_workspace_snapshot

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

    def __init__(self, reader: GitReadRuntime, cancel: CancelToken) -> None:
        self.reader, self.cancel = reader, cancel

    async def result(self, *arguments: str) -> ProcessResult:
        self.cancel.checkpoint()
        return await self.reader._run(  # noqa: SLF001 - 固定内部查询不暴露给模型
            (*self.reader._global_arguments, *arguments), self.cancel
        )

    async def full(self, *arguments: str) -> bytes:
        result = await self.result(*arguments)
        if result.stdout.truncated:
            raise _reject("git_baseline_limit")
        return result.stdout.data()

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


async def _observe(query: _Queries) -> _Observation:
    config = await query.result("config", "--no-includes", "--null", "--name-only", "--list")
    if config.stdout.truncated:
        raise _reject("git_baseline_limit")
    if any(
        _git_helper_key(key) or _UNSAFE_CONFIG.fullmatch(key.lower())
        for key in config.stdout.data().split(b"\0")
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
    if index.stdout.observed_bytes > MAX_TRANSACTION_FILE_BYTES:
        raise _reject("git_baseline_limit")
    status = await query.result(
        "status", "--porcelain=v2", "--untracked-files=all", "--ignore-submodules=all", "-z"
    )
    if status.stdout.truncated:
        raise _reject("git_baseline_limit")
    return _Observation(
        head,
        tree,
        ref,
        index.stdout.observed_sha256,
        index.stdout.observed_bytes,
        status.stdout.observed_sha256,
        config.stdout.observed_sha256,
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
        if (
            result.stdout.observed_bytes != mutation.before.size
            or result.stdout.observed_sha256 != mutation.before.sha256
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
) -> ProductGitDeliveryBaseline:
    """先验原认证归属；精确观察后再次复核，拒绝任何漂移而非自动修复。"""
    deadline = time.monotonic() + _BASELINE_TIMEOUT_SECONDS

    def checkpoint() -> None:
        cancel.checkpoint()
        if time.monotonic() >= deadline:
            raise _reject("git_baseline_timeout")

    source = collect_git_delivery_source(
        thread, targets, router, transactions, checkpoint=checkpoint
    )
    contract = reader.contract()
    if contract["implementation"] != "git-baseline-read/v1":
        raise _reject("git_baseline_reader_required")
    # 原端口只验证自己的仓库根；还必须与认证Thread的原生根身份绑定。
    root = capture_workspace_snapshot(reader._root, platform=source.workspace.platform)  # noqa: SLF001
    if (root.workspace_id, root.root_path_digest, root.root_identity) != (
        source.workspace.workspace_id,
        source.workspace.root_path_digest,
        source.workspace.root_identity,
    ):
        raise _reject("git_baseline_workspace_mismatch")
    query = _Queries(reader, cancel)
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
            verify_workspace_snapshot(source.workspace, Path(thread.workspace))
            checkpoint()
    except TimeoutError:
        raise _reject("git_baseline_timeout") from None
    except (ReadToolError, UnicodeError):
        raise _reject("git_baseline_unavailable") from None
    candidate = ProductGitDeliveryBaseline.model_construct(
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
    result = ProductGitDeliveryBaseline(
        **candidate.model_dump(exclude={"digest"}), digest=product_git_baseline_digest(candidate)
    )
    checkpoint()
    return result
