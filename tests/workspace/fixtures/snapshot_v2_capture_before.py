# Frozen Git commit: 80c4184f3d20e0d7e6741dbe2ab7c701ee5fb710
# Frozen Git path: src/harnessix/workspace/snapshot_v2.py
# Frozen source SHA256: 42bd9d9e71da70ffda7253c1b7128767eebbdc32e3bd3c7e32892a8beab162e0
# The source below is byte-for-byte unchanged.
"""显式宿主 Snapshot v2 API；默认执行、审批和事务仍由原代际约束。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.workspace.contracts import PlatformKind, ResourceAccess, WorkspaceResourceRequest
from harnessix.workspace.parent_closure_codec import (
    encode_parent_closure,
    read_verified_body,
    read_workspace_parent_closure,
)
from harnessix.workspace.parent_closure_paths import target_set_payload
from harnessix.workspace.parent_closure_wire import canonical_bytes, canonical_digest
from harnessix.workspace.snapshot_capture import SnapshotCapture, capture_snapshot_facts
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2, snapshot_requests
from harnessix.workspace.snapshot_ports import WorkspacePureProgressFactory


def _encode_snapshot(
    facts: SnapshotCapture,
    checkpoint: Callable[[], None],
) -> tuple[WorkspaceSnapshotV2, tuple[tuple[str, bytes], ...]]:
    platform = facts.scope["platform"]
    if platform != "posix" and platform != "windows":
        raise KernelError("workspace_platform_unsupported", "Workspace平台不受支持")
    closure = encode_parent_closure(
        facts.scope,
        facts.parents,
        canonical_digest(target_set_payload(facts.requests, platform)),
        checkpoint=checkpoint,
    )
    payload = {
        **facts.scope,
        "resources": [item.model_dump(mode="json") for item in facts.resources],
        "parent_closure": closure.reference.model_dump(mode="json"),
        "algorithm": "selected-resources-parent-closure-sha256/v2",
    }
    snapshot = WorkspaceSnapshotV2.model_validate_json(
        canonical_bytes(
            {
                **payload,
                "revision": canonical_digest(payload),
                "spec_version": "harnessix.workspace-snapshot/v2",
            }
        ),
        strict=True,
    )
    checkpoint()
    return snapshot, closure.blobs


def capture_workspace_snapshot_v2(
    root: str | Path,
    *,
    checkpoint: Callable[[], None],
    write_blob: Callable[[str, bytes], None],
    read_blob: Callable[[str], bytes],
    cwd: str = ".",
    resources: Sequence[WorkspaceResourceRequest] = (),
    external_roots: Mapping[str, tuple[str | Path, tuple[ResourceAccess, ...]]] | None = None,
    platform: PlatformKind | None = None,
) -> WorkspaceSnapshotV2:
    """原耐久写入后完整回读；取消或确认丢失不产生业务事务成功。"""
    facts = capture_snapshot_facts(
        root,
        cwd=cwd,
        resources=resources,
        external_roots=external_roots,
        platform=platform,
        checkpoint=checkpoint,
    )
    snapshot, blobs = _encode_snapshot(facts, checkpoint)
    for digest, body in blobs:
        checkpoint()
        write_blob(digest, body)
        checkpoint()
        # 每块先耐久确认才能登记 Manifest；原 Store.put_blob 同时负责刷盘。
        if read_verified_body(digest, len(body), read_blob, checkpoint) != body:
            raise KernelError("workspace_closure_corrupt", "Workspace父目录历史回读不一致")
        checkpoint()
    if read_workspace_parent_closure(snapshot, read_blob, checkpoint=checkpoint) != facts.parents:
        raise KernelError("workspace_closure_corrupt", "Workspace父目录历史回读不一致")
    checkpoint()
    return snapshot


def verify_workspace_snapshot_v2(
    expected: WorkspaceSnapshotV2,
    root: str | Path,
    *,
    checkpoint: Callable[[], None],
    read_blob: Callable[[str], bytes],
    external_roots: Mapping[str, tuple[str | Path, tuple[ResourceAccess, ...]]] | None = None,
    native_progress: AbstractContextManager[Callable[[], None]] | None = None,
    pure_progress: WorkspacePureProgressFactory | None = None,
) -> WorkspaceSnapshotV2:
    """先验证完整旧历史，再只观察当前事实；没有 CAS 写入或补签端口。"""
    if pure_progress is None:
        historical = read_workspace_parent_closure(expected, read_blob, checkpoint=checkpoint)
    else:
        historical = read_workspace_parent_closure(
            expected, read_blob, checkpoint=checkpoint, pure_progress=pure_progress
        )
    capture_progress = native_progress if native_progress is not None else nullcontext(checkpoint)
    with capture_progress as native_check:
        facts = capture_snapshot_facts(
            root,
            cwd=expected.cwd,
            resources=snapshot_requests(expected),
            external_roots=external_roots,
            platform=expected.platform,
            checkpoint=native_check,
        )
    # 原生捕获已经结束；新纯段只重编码事实，不读取或追加 CAS。
    with pure_progress() if pure_progress is not None else nullcontext(checkpoint) as pure_check:
        current, _ = _encode_snapshot(facts, pure_check)
    if current != expected or facts.parents != historical:
        raise KernelError("execution_plan_stale", "Workspace Snapshot已变化")
    return current
