"""以真实微小文件验证原 Snapshot 表示边界与 T Planner 父目录扩张。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import traceback
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.delivery.planner import (
    DesiredWorkspaceFile,
    PreparedWorkspaceTransaction,
    prepare_workspace_transaction,
)
from harnessix.workspace.contracts import WorkspaceResourceRequest, WorkspaceSnapshot
from harnessix.workspace.snapshot import capture_workspace_snapshot, verify_workspace_snapshot

_BEFORE = b"before\n"
_AFTER = b"after\n"


def _record(group: str, name: str, facts: object) -> None:
    """仅显式取证时独占创建私有事实文件，普通回归不产生额外交付物。"""
    target = os.environ.get("HARNESSIX_PROJECTION_CAPACITY_EVIDENCE")
    if target is None:
        return
    path = Path(target) / group / f"{name}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(facts, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.write("\n")


def _snapshot_requests(error: Exception) -> Sequence[WorkspaceResourceRequest]:
    """读取原异常栈中的真实资源入参，不推测失败调用的父目录集合。"""
    frame = error.__traceback__
    while frame is not None:
        if frame.tb_frame.f_code is capture_workspace_snapshot.__code__:
            return frame.tb_frame.f_locals["resources"]
        frame = frame.tb_next
    return ()


def _assert_snapshot_limit(error: KernelError, expected: set[tuple[str, str, str]]) -> None:
    """校验原错误码及实际拒绝请求的精确数量和全集。"""
    requests = _snapshot_requests(error)
    assert error.code == "workspace_snapshot_limit"
    assert len(requests) == len(expected)
    assert {(item.location, item.path, item.access) for item in requests} == expected


def _invoke[T: WorkspaceSnapshot | PreparedWorkspaceTransaction](
    name: str, operation: Callable[[], T]
) -> T:
    """先保留原返回或异常；只读异常栈获取实际请求，不替换任何生产端口。"""
    try:
        result = operation()
    except Exception as error:
        facts: dict[str, object] = {
            "outcome": "error",
            "type": type(error).__name__,
            "code": getattr(error, "code", None),
            "message": str(error),
            "traceback": "".join(traceback.format_exception(error)),
        }
        if isinstance(error, ValidationError):
            facts["validation_errors"] = error.errors(include_input=False)
        requests = _snapshot_requests(error)
        facts["actual_snapshot_requests"] = [item.model_dump(mode="json") for item in requests]
        facts["actual_snapshot_request_count"] = len(requests)
        _record("original-results", name, facts)
        raise
    if isinstance(result, PreparedWorkspaceTransaction):
        facts = {
            "outcome": "returned",
            "plan": result.plan.model_dump(mode="json"),
            "blobs_hex": {key: body.hex() for key, body in result.blobs.items()},
        }
    else:
        facts = {"outcome": "returned", "snapshot": result.model_dump(mode="json")}
    _record("original-results", name, facts)
    return result


def _members(root: Path, name: str, count: int, *, scattered: bool) -> tuple[str, ...]:
    """复用 tmp_path 创建真实 0644 文件，并在原调用前锁定字节与完整资源集合。"""
    paths = tuple(f"d{index:03}/f" if scattered else f"f{index:03}" for index in range(count))
    files = []
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_BEFORE)
        target.chmod(0o644)
        files.append(
            {
                "path": path,
                "before_hex": target.read_bytes().hex(),
                "before_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "before_bytes": target.stat().st_size,
                "actual_mode": stat.S_IMODE(target.stat().st_mode),
                "desired_hex": _AFTER.hex(),
                "desired_sha256": hashlib.sha256(_AFTER).hexdigest(),
                "desired_bytes": len(_AFTER),
                "desired_mode": 0o644,
            }
        )
    planner_keys = {(path, "write") for path in paths} | {(".", "read")}
    if scattered:
        planner_keys |= {(f"d{index:03}", "read") for index in range(count)}
    _record(
        "locked-inputs",
        name,
        {
            "root": str(root),
            "files": files,
            "source_leaf_read_requests": [
                {"location": "workspace", "path": path, "access": "read"} for path in paths
            ],
            "source_expected_with_cwd_count": count + 1,
            "planner_expected_resources": [
                {"location": "workspace", "path": path, "access": access}
                for path, access in sorted(planner_keys)
            ],
            "planner_expected_resource_count": len(planner_keys),
            "before_total_bytes": count * len(_BEFORE),
            "desired_total_bytes": count * len(_AFTER),
            "transaction_image_bytes": count * (len(_BEFORE) + len(_AFTER)),
        },
    )
    return paths


def _reads(paths: Sequence[str]) -> tuple[WorkspaceResourceRequest, ...]:
    """只生成每叶 read，不代替原 Snapshot 的隐式 cwd 规则。"""
    return tuple(WorkspaceResourceRequest(path=path, access="read") for path in paths)


def _keys(snapshot: WorkspaceSnapshot) -> set[tuple[str, str, str]]:
    """比较原观察中的完整位置、路径和访问模式，不折叠 read 与 write。"""
    return {(item.location, item.path, item.access) for item in snapshot.resources}


def _prepare(root: Path, paths: Sequence[str]) -> PreparedWorkspaceTransaction:
    """固定事务元数据并完整提交所有目标，不拆分、裁剪或发布事务。"""
    return prepare_workspace_transaction(
        root,
        {path: DesiredWorkspaceFile(_AFTER, 0o644) for path in paths},
        request_id="projection-capacity-proof",
        transaction_id=UUID("00000000-0000-4000-8000-000000000001"),
        now=datetime(2026, 10, 3, tzinfo=UTC),
    )


@pytest.mark.parametrize("count", [127, 128])
def test_distinct_parent_projection_capacity(tmp_path: Path, count: int) -> None:
    """127 个独立父目录准备通过；128 个叶可被原 Snapshot 表示但原 T 拒绝。"""
    name = f"scattered-{count}"
    paths = _members(tmp_path, name, count, scattered=True)
    expected = {("workspace", path, "write") for path in paths} | {
        ("workspace", parent, "read") for parent in (".", *(f"d{i:03}" for i in range(count)))
    }
    source = _invoke(
        f"{name}-leaf-snapshot",
        lambda: capture_workspace_snapshot(tmp_path, resources=_reads(paths)),
    )
    assert len(source.resources) == count + 1
    assert _keys(source) == {("workspace", path, "read") for path in (".", *paths)}
    assert all(
        item.content_sha256 == hashlib.sha256(_BEFORE).hexdigest() and item.size == len(_BEFORE)
        for item in source.resources
        if item.kind == "file"
    )
    assert _invoke(
        f"{name}-snapshot-recheck", lambda: verify_workspace_snapshot(source, tmp_path)
    ) == (source)
    if count == 128:
        with pytest.raises(KernelError) as denied:
            _invoke(f"{name}-planner", lambda: _prepare(tmp_path, paths))
        assert len(expected) == 257
        _assert_snapshot_limit(denied.value, expected)
    else:
        prepared = _invoke(f"{name}-planner", lambda: _prepare(tmp_path, paths))
        assert len(prepared.plan.source.resources) == 255
        assert _keys(prepared.plan.source) == expected
        assert len(prepared.plan.mutations) == count
        assert all(item.before.mode == item.after.mode == 0o644 for item in prepared.plan.mutations)
        assert set(prepared.blobs.values()) == {_BEFORE, _AFTER}
    assert all((tmp_path / path).read_bytes() == _BEFORE for path in paths)


@pytest.mark.parametrize("count", [255, 256])
def test_flat_projection_capacity(tmp_path: Path, count: int) -> None:
    """cwd 内 255 叶合并为 256 资源通过，256 叶的原 T 保持限额拒绝。"""
    name = f"flat-{count}"
    paths = _members(tmp_path, name, count, scattered=False)
    if count == 256:
        with pytest.raises(KernelError) as denied:
            _invoke(f"{name}-planner", lambda: _prepare(tmp_path, paths))
        expected = {("workspace", path, "write") for path in paths} | {("workspace", ".", "read")}
        assert len(expected) == 257
        _assert_snapshot_limit(denied.value, expected)
    else:
        source = _invoke(
            f"{name}-leaf-snapshot",
            lambda: capture_workspace_snapshot(tmp_path, resources=_reads(paths)),
        )
        prepared = _invoke(f"{name}-planner", lambda: _prepare(tmp_path, paths))
        assert len(source.resources) == len(prepared.plan.source.resources) == 256
        assert _keys(source) == {("workspace", path, "read") for path in (".", *paths)}
        assert _keys(prepared.plan.source) == {("workspace", path, "write") for path in paths} | {
            ("workspace", ".", "read")
        }
        assert len(prepared.plan.mutations) == 255
        assert all(item.before.mode == item.after.mode == 0o644 for item in prepared.plan.mutations)
        assert set(prepared.blobs.values()) == {_BEFORE, _AFTER}
    assert all((tmp_path / path).read_bytes() == _BEFORE for path in paths)


@pytest.mark.parametrize("count", [256, 257])
def test_snapshot_explicit_cwd_preserves_original_limit(tmp_path: Path, count: int) -> None:
    """超出可表示全集时显式带 cwd，原输入限额必须返回原错误码。"""
    name = f"explicit-cwd-{count}"
    paths = _members(tmp_path, name, count, scattered=False)
    requests = (*_reads(paths), WorkspaceResourceRequest(path=".", access="read"))
    with pytest.raises(KernelError) as denied:
        _invoke(name, lambda: capture_workspace_snapshot(tmp_path, resources=requests))
    _assert_snapshot_limit(denied.value, {("workspace", path, "read") for path in (".", *paths)})


def test_snapshot_implicit_cwd_contract_boundary(tmp_path: Path) -> None:
    """256 叶输入后补 cwd 得到 257 项，以正式限额错误拒绝而不构造超限合同。"""
    name = "implicit-cwd-256"
    paths = _members(tmp_path, name, 256, scattered=False)
    with pytest.raises(KernelError) as denied:
        _invoke(name, lambda: capture_workspace_snapshot(tmp_path, resources=_reads(paths)))
    _assert_snapshot_limit(denied.value, {("workspace", path, "read") for path in paths})


def test_snapshot_resource_keys_and_cwd(tmp_path: Path) -> None:
    """显隐 cwd 等价且仅一项；同路径不同访问模式独立，重复 read 原样拒绝。"""
    paths = _members(tmp_path, "resource-rules", 1, scattered=False)
    leaf = _reads(paths)
    cwd = WorkspaceResourceRequest(path=".", access="read")
    implicit = _invoke(
        "rules-implicit-cwd", lambda: capture_workspace_snapshot(tmp_path, resources=leaf)
    )
    explicit = _invoke(
        "rules-explicit-cwd", lambda: capture_workspace_snapshot(tmp_path, resources=(*leaf, cwd))
    )
    assert implicit == explicit and len(explicit.resources) == 2
    both = _invoke(
        "rules-read-write",
        lambda: capture_workspace_snapshot(
            tmp_path,
            resources=(*leaf, WorkspaceResourceRequest(path=paths[0], access="write"), cwd),
        ),
    )
    assert _keys(both) == {
        ("workspace", ".", "read"),
        ("workspace", paths[0], "read"),
        ("workspace", paths[0], "write"),
    }
    with pytest.raises(KernelError) as denied:
        _invoke(
            "rules-duplicate-read",
            lambda: capture_workspace_snapshot(tmp_path, resources=(*leaf, *leaf)),
        )
    assert denied.value.code == "workspace_snapshot_duplicate"
