"""真实原生事实与原私有 CAS 的完整父历史 Reader 验真和控制异常测试。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.tools.contracts import ReadToolError
from harnessix.workspace.contracts import WorkspaceResourceObservation, WorkspaceResourceRequest
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from harnessix.workspace.parent_closure_contracts import WorkspaceParentClosureManifest
from harnessix.workspace.snapshot import capture_workspace_snapshot
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2
from harnessix.workspace.snapshot_v2 import capture_workspace_snapshot_v2


def _checkpoint() -> None:
    pass


def _canonical(value: object) -> bytes:
    # 独立计算正式 JSON 与摘要，避免用被测 Codec 自证其编码正确。
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True)
class _History:
    root: Path
    external: Path
    state: Path
    store: SQLiteWorkspaceTransactionStore
    snapshot: WorkspaceSnapshotV2
    parents: tuple[WorkspaceResourceObservation, ...]

    def manifest(self) -> dict:
        return json.loads(self.store._read_blob(self.snapshot.parent_closure.sha256))


@pytest.fixture
def history(tmp_path: Path) -> Iterator[_History]:
    root, external, state = tmp_path / "workspace", tmp_path / "external", tmp_path / "state"
    for base, path in (
        (root, "src/deep/alpha.txt"),
        (root, "src/other/beta.txt"),
        (root, "zz-spare/unused.txt"),
        (external, "data/deep/external.txt"),
    ):
        target = base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.encode("utf-8"))
    roots = {"cache": (external, ("read",))}
    expected_paths = (
        ("cache", "."),
        ("cache", "data"),
        ("cache", "data/deep"),
        ("workspace", "."),
        ("workspace", "src"),
        ("workspace", "src/deep"),
        ("workspace", "src/other"),
    )
    # 小规模对照用原 v1 原生端口独立观察全部父项，不伪造身份或缩减容量。
    native = capture_workspace_snapshot(
        root,
        cwd="src",
        resources=tuple(
            WorkspaceResourceRequest(location=location, path=path, access="read")
            for location, path in expected_paths
        ),
        external_roots=roots,
    )
    with SQLiteWorkspaceTransactionStore(state) as store:
        snapshot = capture_workspace_snapshot_v2(
            root,
            cwd="src",
            resources=(
                WorkspaceResourceRequest(path="src/deep/alpha.txt", access="read"),
                WorkspaceResourceRequest(path="src/other/beta.txt", access="write"),
                WorkspaceResourceRequest(
                    location="cache", path="data/deep/external.txt", access="read"
                ),
            ),
            external_roots=roots,
            checkpoint=_checkpoint,
            write_blob=store.put_blob,
            read_blob=store._read_blob,
        )
        parents = read_workspace_parent_closure(snapshot, store._read_blob, checkpoint=_checkpoint)
        assert tuple((item.location, item.path) for item in parents) == expected_paths
        assert parents == native.resources
        assert len(parents) == snapshot.parent_closure.parent_count == 7
        yield _History(root, external, state, store, snapshot, parents)


def _put(store: SQLiteWorkspaceTransactionStore, body: bytes) -> str:
    digest = _sha(body)
    store.put_blob(digest, body)
    assert store._read_blob(digest) == body
    return digest


def _resign(snapshot: WorkspaceSnapshotV2, **changes: object) -> WorkspaceSnapshotV2:
    payload = snapshot.model_dump(mode="json")
    payload.update(changes)
    payload["revision"] = _sha(
        _canonical(
            {
                key: value
                for key, value in payload.items()
                if key not in {"spec_version", "revision"}
            }
        )
    )
    # 不使用 model_copy 绕过校验；每个反例都有正式合法的新 revision。
    result = WorkspaceSnapshotV2.model_validate_json(_canonical(payload), strict=True)
    assert result.revision != snapshot.revision
    return result


def _publish(history: _History, manifest: dict | bytes) -> WorkspaceSnapshotV2:
    body = manifest if isinstance(manifest, bytes) else _canonical(manifest)
    reference = history.snapshot.parent_closure.model_dump(mode="json")
    reference.update(sha256=_put(history.store, body), size=len(body))
    return _resign(history.snapshot, parent_closure=reference)


def _chunk_reference(history: _History, start: int, entries: list[dict]) -> dict:
    body = _canonical(
        {
            "spec_version": "harnessix.workspace-parent-observations/v1",
            "start_index": start,
            "entries": entries,
        }
    )
    return {
        "sha256": _put(history.store, body),
        "size": len(body),
        "start_index": start,
        "count": len(entries),
    }


def _replace_chunk(history: _History, manifest: dict, index: int, chunk: dict | bytes) -> None:
    body = chunk if isinstance(chunk, bytes) else _canonical(chunk)
    manifest["chunks"][index].update(sha256=_put(history.store, body), size=len(body))


def _entries(parents: tuple[WorkspaceResourceObservation, ...]) -> list[dict]:
    return [
        item.model_dump(mode="json", exclude={"location", "path", "access"}) for item in parents
    ]


def _two_chunks(history: _History) -> _History:
    manifest = history.manifest()
    entries = _entries(history.parents)
    split = len(entries) // 2
    manifest["chunks"] = [
        _chunk_reference(history, 0, entries[:split]),
        _chunk_reference(history, split, entries[split:]),
    ]
    snapshot = _publish(history, manifest)
    # 合法重分块只改变物理引用；路径、所有源事实和观察集合摘要保持原字节。
    assert manifest["observations_digest"] == _sha(
        _canonical([item.model_dump(mode="json") for item in history.parents])
    )
    assert snapshot.resources == history.snapshot.resources
    assert (
        read_workspace_parent_closure(snapshot, history.store._read_blob, checkpoint=_checkpoint)
        == history.parents
    )
    return replace(history, snapshot=snapshot)


def _reject(history: _History, snapshot: WorkspaceSnapshotV2) -> list[str]:
    reads: list[str] = []

    def read_blob(digest: str) -> bytes:
        reads.append(digest)
        return history.store._read_blob(digest)

    with pytest.raises(KernelError) as error:
        read_workspace_parent_closure(snapshot, read_blob, checkpoint=_checkpoint)
    assert error.value.code == "workspace_closure_corrupt"
    assert reads[0] == snapshot.parent_closure.sha256
    return reads


def _noncanonical(payload: dict, case: str) -> bytes:
    if case == "spaces":
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")
    if case == "extra_key":
        return _canonical({**payload, "unexpected": True})
    # 重复键的两个值相同，普通 JSON 解析后仍是原对象，必须由规范字节校验拒绝。
    return (
        b'{"spec_version":' + _canonical(payload["spec_version"]) + b"," + _canonical(payload)[1:]
    )


@pytest.mark.parametrize("layer", ["manifest", "chunk"])
def test_reader_rejects_unknown_versions_with_valid_sha_and_revision(
    history: _History, layer: str
) -> None:
    manifest = history.manifest()
    if layer == "manifest":
        manifest["spec_version"] = "harnessix.workspace-parent-closure/v999"
    else:
        chunk = json.loads(history.store._read_blob(manifest["chunks"][0]["sha256"]))
        chunk["spec_version"] = "harnessix.workspace-parent-observations/v999"
        _replace_chunk(history, manifest, 0, chunk)
    snapshot = _publish(history, manifest)
    reads = _reject(history, snapshot)
    assert len(reads) == (1 if layer == "manifest" else 2)


@pytest.mark.parametrize("layer", ["manifest", "chunk"])
@pytest.mark.parametrize("case", ["spaces", "extra_key", "duplicate_key"])
def test_reader_rejects_noncanonical_readdressed_json(
    history: _History, layer: str, case: str
) -> None:
    manifest = history.manifest()
    if layer == "manifest":
        raw = _noncanonical(manifest, case)
        snapshot = _publish(history, raw)
    else:
        chunk = json.loads(history.store._read_blob(manifest["chunks"][0]["sha256"]))
        _replace_chunk(history, manifest, 0, _noncanonical(chunk, case))
        snapshot = _publish(history, manifest)
    assert len(_reject(history, snapshot)) == (1 if layer == "manifest" else 2)


@pytest.mark.parametrize(
    "field",
    [
        "platform",
        "workspace_id",
        "root_path_digest",
        "root_identity",
        "external_path",
        "external_identity",
        "external_location",
        "external_access",
        "external_missing",
        "cwd",
        "target_set_digest",
    ],
)
def test_reader_rejects_manifest_scope_and_target_binding(history: _History, field: str) -> None:
    manifest = history.manifest()
    if field == "platform":
        manifest[field] = "windows" if history.snapshot.platform == "posix" else "posix"
    elif field == "cwd":
        manifest[field] = "src/deep"
    elif field == "external_missing":
        manifest["external_roots"] = []
    elif field.startswith("external_"):
        external_field = {
            "external_path": "path_digest",
            "external_identity": "identity",
            "external_location": "location",
            "external_access": "access",
        }[field]
        manifest["external_roots"][0][external_field] = (
            "other"
            if external_field == "location"
            else ["read", "write"]
            if external_field == "access"
            else _sha(field.encode())
        )
    else:
        manifest[field] = _sha(field.encode())
    # 这些对象本身合法，拒绝点必须是 Snapshot 与 Manifest 的深层绑定。
    WorkspaceParentClosureManifest.model_validate_json(_canonical(manifest), strict=True)
    assert _reject(history, _publish(history, manifest)) == [_sha(_canonical(manifest))]


@pytest.mark.parametrize("case", ["missing", "extra"])
def test_reader_requires_exact_dictionary_not_only_chunk_coverage(
    history: _History, case: str
) -> None:
    manifest = history.manifest()
    parents = history.parents
    if case == "missing":
        manifest["nodes"].pop()
        parents = parents[:-1]
    else:
        donor = capture_workspace_snapshot_v2(
            history.root,
            cwd="src",
            resources=(WorkspaceResourceRequest(path="zz-spare/unused.txt", access="read"),),
            checkpoint=_checkpoint,
            write_blob=history.store.put_blob,
            read_blob=history.store._read_blob,
        )
        spare = next(
            item
            for item in read_workspace_parent_closure(
                donor, history.store._read_blob, checkpoint=_checkpoint
            )
            if item.path == "zz-spare"
        )
        root_index = next(
            index
            for index, node in enumerate(manifest["nodes"])
            if node["location"] == "workspace" and node["name"] == "."
        )
        manifest["nodes"].append({"parent": root_index, "name": "zz-spare", "location": None})
        parents = (*parents, spare)
    manifest["chunks"] = [_chunk_reference(history, 0, _entries(parents))]
    manifest["observations_digest"] = _sha(
        _canonical([item.model_dump(mode="json") for item in parents])
    )
    WorkspaceParentClosureManifest.model_validate_json(_canonical(manifest), strict=True)
    assert _reject(history, _publish(history, manifest)) == [_sha(_canonical(manifest))]


@pytest.mark.parametrize(
    "case",
    [
        "duplicate",
        "out_of_order",
        "escape",
        "embedded_separator",
        "self_edge",
        "forward_edge",
        "cycle",
    ],
)
def test_reader_rejects_malformed_path_dictionary(history: _History, case: str) -> None:
    manifest = history.manifest()
    nodes = manifest["nodes"]
    if case == "duplicate":
        nodes[-1] = copy.deepcopy(nodes[-2])
    elif case == "out_of_order":
        nodes[-1], nodes[-2] = nodes[-2], nodes[-1]
    elif case == "escape":
        nodes[-1]["name"] = ".."
    elif case == "embedded_separator":
        nodes[-1]["name"] = "../outside"
    elif case == "self_edge":
        nodes[-1]["parent"] = len(nodes) - 1
    elif case == "forward_edge":
        nodes[-2]["parent"] = len(nodes) - 1
    else:
        nodes[-2]["parent"], nodes[-1]["parent"] = len(nodes) - 1, len(nodes) - 2
    assert _reject(history, _publish(history, manifest)) == [_sha(_canonical(manifest))]


@pytest.mark.parametrize("layer", ["manifest", "first_chunk", "last_chunk"])
def test_reader_rejects_missing_readdressed_blobs(history: _History, layer: str) -> None:
    split = _two_chunks(history)
    manifest = split.manifest()
    missing = (
        split.snapshot.parent_closure.sha256
        if layer == "manifest"
        else manifest["chunks"][0 if layer == "first_chunk" else -1]["sha256"]
    )
    # 仅删除本例自建 CAS 中重新合法寻址的 Blob，不替换真实读取端口。
    (split.state / "blobs" / missing).unlink()
    reads = _reject(split, split.snapshot)
    assert reads[-1] == missing
    assert len(reads) == {"manifest": 1, "first_chunk": 2, "last_chunk": 3}[layer]


@pytest.mark.parametrize("layer", ["manifest", "chunk"])
@pytest.mark.parametrize("delta", [-1, 1])
def test_reader_rejects_wrong_declared_blob_size(history: _History, layer: str, delta: int) -> None:
    if layer == "manifest":
        split = _two_chunks(history)
        reference = split.snapshot.parent_closure.model_dump(mode="json")
        reference["size"] += delta
        snapshot = _resign(split.snapshot, parent_closure=reference)
        assert _sha(history.store._read_blob(reference["sha256"])) == reference["sha256"]
    else:
        manifest = history.manifest()
        manifest["chunks"][0]["size"] += delta
        snapshot = _publish(history, manifest)
    assert len(_reject(history, snapshot)) == (1 if layer == "manifest" else 2)


@pytest.mark.parametrize(
    "case",
    [
        "order",
        "duplicate",
        "omitted",
        "ref_start",
        "ref_count",
        "body_start",
        "body_count",
        "entry_order",
        "entry_identity",
        "collection_digest",
    ],
)
def test_reader_validates_chunk_sequence_indexes_and_complete_observations(
    history: _History, case: str
) -> None:
    split = _two_chunks(history)
    manifest = split.manifest()
    refs = manifest["chunks"]
    if case == "order":
        refs.reverse()
    elif case == "duplicate":
        refs[1] = copy.deepcopy(refs[0])
    elif case == "omitted":
        refs.pop()
    elif case == "ref_start":
        refs[0]["start_index"] = 1
    elif case == "ref_count":
        # 引用仍连续且总数仍正确；只有与真实块内容对比才会发现错误。
        refs[0]["count"] -= 1
        refs[1]["start_index"] -= 1
        refs[1]["count"] += 1
    elif case == "collection_digest":
        manifest["observations_digest"] = _sha(b"wrong complete collection")
    else:
        chunk = json.loads(split.store._read_blob(refs[-1]["sha256"]))
        if case == "body_start":
            chunk["start_index"] += 1
        elif case == "body_count":
            chunk["entries"].pop()
        elif case == "entry_order":
            chunk["entries"].reverse()
        else:
            chunk["entries"][-1]["identity"] = _sha(b"wrong parent identity")
        _replace_chunk(split, manifest, 1, chunk)
    snapshot = _publish(split, manifest)
    reads = _reject(split, snapshot)
    if case in {"order", "duplicate", "omitted", "ref_start"}:
        assert reads == [snapshot.parent_closure.sha256]
    elif case == "ref_count":
        assert reads == [snapshot.parent_closure.sha256, refs[0]["sha256"]]
    else:
        assert reads == [snapshot.parent_closure.sha256, *(item["sha256"] for item in refs)]


@pytest.mark.parametrize("field", ["identity", "size"])
@pytest.mark.parametrize("side", ["explicit", "parent"])
def test_reader_rejects_explicit_cwd_disagreement_after_collection_authentication(
    history: _History, field: str, side: str
) -> None:
    if side == "explicit":
        resources = history.snapshot.model_dump(mode="json")["resources"]
        cwd = next(
            item for item in resources if item["location"] == "workspace" and item["path"] == "src"
        )
        cwd[field] = _sha(b"wrong cwd identity") if field == "identity" else cwd[field] + 1
        snapshot = _resign(history.snapshot, resources=resources)
    else:
        manifest = history.manifest()
        parents = [item.model_dump(mode="json") for item in history.parents]
        index = next(
            i
            for i, item in enumerate(parents)
            if item["location"] == "workspace" and item["path"] == "src"
        )
        parents[index][field] = (
            _sha(b"wrong cwd identity") if field == "identity" else parents[index][field] + 1
        )
        chunk = json.loads(history.store._read_blob(manifest["chunks"][0]["sha256"]))
        chunk["entries"][index][field] = parents[index][field]
        _replace_chunk(history, manifest, 0, chunk)
        # 同时重新绑定完整集合摘要，确保失败不是 SHA 或集合摘要失配。
        manifest["observations_digest"] = _sha(_canonical(parents))
        snapshot = _publish(history, manifest)
    assert len(_reject(history, snapshot)) == 2


@pytest.mark.parametrize("location", ["workspace", "cache"])
def test_reader_rejects_missing_closure_root_after_collection_authentication(
    history: _History, location: str
) -> None:
    manifest = history.manifest()
    parents = [item.model_dump(mode="json") for item in history.parents]
    index = next(
        i for i, item in enumerate(parents) if item["location"] == location and item["path"] == "."
    )
    parents[index].update(kind="missing", size=0)
    chunk = json.loads(history.store._read_blob(manifest["chunks"][0]["sha256"]))
    chunk["entries"][index].update(kind="missing", size=0)
    _replace_chunk(history, manifest, 0, chunk)
    # missing/零大小是合法观察；独立重算集合摘要，专门验证闭包根必须是目录。
    manifest["observations_digest"] = _sha(_canonical(parents))
    snapshot = _publish(history, manifest)
    assert len(_reject(history, snapshot)) == 2


@pytest.mark.parametrize("used", [True, False], ids=["used_write_only", "unused_write_only"])
def test_snapshot_read_grant_requirement_is_bound_to_used_external_roots(
    history: _History, used: bool
) -> None:
    donor = capture_workspace_snapshot_v2(
        history.root,
        cwd="src",
        resources=(
            WorkspaceResourceRequest(
                location="cache" if used else "workspace",
                path="data/deep/external.txt" if used else "src/deep/alpha.txt",
                access="write",
            ),
        ),
        external_roots={"cache": (history.external, ("read", "write"))},
        checkpoint=_checkpoint,
        write_blob=history.store.put_blob,
        read_blob=history.store._read_blob,
    )
    original = read_workspace_parent_closure(
        donor, history.store._read_blob, checkpoint=_checkpoint
    )
    roots = donor.model_dump(mode="json")["external_roots"]
    roots[0]["access"] = ["write"]
    manifest = json.loads(history.store._read_blob(donor.parent_closure.sha256))
    manifest["external_roots"] = roots
    body = _canonical(manifest)
    reference = donor.parent_closure.model_dump(mode="json")
    reference.update(sha256=_put(history.store, body), size=len(body))
    if used:
        # 叶的 write 仍被正式授予，引用、目标与 revision 也已重算；仅父 read 授权缺失。
        with pytest.raises(ValidationError, match="派生父目录未获外部根read授权"):
            _resign(donor, external_roots=roots, parent_closure=reference)
    else:
        snapshot = _resign(donor, external_roots=roots, parent_closure=reference)
        assert (
            read_workspace_parent_closure(
                snapshot, history.store._read_blob, checkpoint=_checkpoint
            )
            == original
        )


def test_capture_does_not_borrow_external_write_grant_for_parent_read(history: _History) -> None:
    before = _cas_state(history.state)
    with pytest.raises(KernelError) as error:
        capture_workspace_snapshot_v2(
            history.root,
            cwd="src",
            resources=(
                WorkspaceResourceRequest(
                    location="cache", path="data/deep/external.txt", access="write"
                ),
            ),
            external_roots={"cache": (history.external, ("write",))},
            checkpoint=_checkpoint,
            write_blob=history.store.put_blob,
            read_blob=history.store._read_blob,
        )
    assert error.value.code == "workspace_access_denied"
    assert _cas_state(history.state) == before


def test_reader_reads_every_rechunked_blob_and_returns_original_native_facts(
    history: _History,
) -> None:
    split = _two_chunks(history)
    manifest = split.manifest()
    reads: list[str] = []

    def read_blob(digest: str) -> bytes:
        reads.append(digest)
        return split.store._read_blob(digest)

    assert (
        read_workspace_parent_closure(split.snapshot, read_blob, checkpoint=_checkpoint)
        == history.parents
    )
    assert reads == [
        split.snapshot.parent_closure.sha256,
        *(ref["sha256"] for ref in manifest["chunks"]),
    ]


def _control_error(kind: str) -> BaseException:
    return {
        "cancelled": lambda: asyncio.CancelledError("parent cancelled"),
        "timeout": lambda: TimeoutError("parent deadline"),
        "turn_cancelled": lambda: TurnCancelled("parent turn cancelled"),
        "read_tool": lambda: ReadToolError("timeout"),
        "kernel": lambda: KernelError("workspace_path_denied", "父读取拒绝"),
    }[kind]()


@pytest.mark.parametrize("kind", ["cancelled", "timeout", "turn_cancelled", "read_tool", "kernel"])
@pytest.mark.parametrize("boundary", [0, 1, 2], ids=["manifest", "first_chunk", "last_chunk"])
def test_reader_read_callback_preserves_original_control_exception(
    history: _History, kind: str, boundary: int
) -> None:
    split = _two_chunks(history)
    refs = split.manifest()["chunks"]
    addresses = [split.snapshot.parent_closure.sha256, *(ref["sha256"] for ref in refs)]
    original = _control_error(kind)
    reads: list[str] = []

    def read_blob(digest: str) -> bytes:
        reads.append(digest)
        if digest == addresses[boundary]:
            raise original
        return split.store._read_blob(digest)

    with pytest.raises(type(original)) as error:
        read_workspace_parent_closure(split.snapshot, read_blob, checkpoint=_checkpoint)
    assert error.value is original
    assert reads == addresses[: boundary + 1]


@pytest.mark.parametrize("kind", ["cancelled", "timeout", "turn_cancelled", "read_tool", "kernel"])
def test_reader_every_checkpoint_preserves_original_control_exception(
    history: _History, kind: str
) -> None:
    split = _two_chunks(history)
    trace: list[tuple[str, str]] = []

    def checkpoint() -> None:
        trace.append(("checkpoint", ""))

    def read_blob(digest: str) -> bytes:
        trace.append(("read", digest))
        return split.store._read_blob(digest)

    assert (
        read_workspace_parent_closure(split.snapshot, read_blob, checkpoint=checkpoint)
        == history.parents
    )
    checkpoints = [i for i, event in enumerate(trace) if event[0] == "checkpoint"]
    assert len(checkpoints) >= 2 * (1 + 2) + len(history.parents)

    def assert_interrupted_at(stop: int) -> None:
        observed: list[tuple[str, str]] = []
        original = _control_error(kind)

        def failing_checkpoint() -> None:
            observed.append(("checkpoint", ""))
            if len(observed) == stop + 1:
                raise original

        def tracked_read(digest: str) -> bytes:
            observed.append(("read", digest))
            return split.store._read_blob(digest)

        with pytest.raises(type(original)) as error:
            read_workspace_parent_closure(
                split.snapshot, tracked_read, checkpoint=failing_checkpoint
            )
        assert error.value is original
        assert observed == trace[: stop + 1]

    for stop in checkpoints:
        assert_interrupted_at(stop)


def _cas_state(state: Path) -> dict[str, tuple[int, int, int, str]]:
    return {
        path.name: (
            path.stat().st_mode,
            path.stat().st_size,
            path.stat().st_mtime_ns,
            _sha(path.read_bytes()),
        )
        for path in (state / "blobs").iterdir()
    }


@pytest.mark.parametrize("read_only", [False, True], ids=["writable", "readonly_reopen"])
def test_reader_performs_zero_cas_or_business_writes(
    history: _History, monkeypatch: pytest.MonkeyPatch, read_only: bool
) -> None:
    split = _two_chunks(history)
    if read_only:
        history.store.close()
        store = SQLiteWorkspaceTransactionStore(history.state, read_only=True)
    else:
        store = history.store
    try:
        before = _cas_state(history.state)
        changes = store._db.total_changes
        writes: list[str] = []

        def forbidden_write(*args: object, **kwargs: object) -> None:
            writes.append("write")
            pytest.fail("Reader 不得写入、迁移或补签私有 CAS 或业务账本")

        for name in ("put_blob", "_put_blob", "save", "transition"):
            monkeypatch.setattr(store, name, forbidden_write)
        assert (
            read_workspace_parent_closure(split.snapshot, store._read_blob, checkpoint=_checkpoint)
            == history.parents
        )
        assert not writes
        assert store._db.total_changes == changes
        assert _cas_state(history.state) == before
    finally:
        if read_only:
            store.close()
