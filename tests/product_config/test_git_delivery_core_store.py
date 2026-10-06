"""完整 Core 的原 CAS 耐久入口：内容地址不证明 Owner、归属、材料或批准。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery import store as store_module
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_delivery_plan_contracts import ProductGitDeliveryCore
from harnessix.product_config.git_delivery_plan_wire import (
    MAX_PRODUCT_GIT_PLAN_BYTES,
    decode_product_git_delivery_core,
    encode_product_git_delivery_core,
)
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from tests.product_config.git_delivery_plan_support import (
    ZERO,
    canonical_bytes,
    make_case,
    observe_state,
    sealed,
)


@pytest.fixture
def cas(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        yield GitMaterialCAS(store)


def _body(core):
    """独立计算完整预期字节，不调用被测编码器，不丢弃任何父引用。"""
    return canonical_bytes(core.model_dump(mode="json", exclude={"fingerprint"}, warnings="error"))


def _reject(operation, code, *values, **kwargs):
    with pytest.raises(KernelError) as caught:
        operation(*values, checkpoint=lambda: None, **kwargs)
    assert caught.value.code == code
    assert caught.value.__cause__ is None
    assert "新代码" not in str(caught.value)
    assert "native-sensitive" not in str(caught.value)
    return caught.value


def _save_raw(store, body):
    digest = hashlib.sha256(body).hexdigest()
    store.put_blob(digest, body)
    return digest


def _database(store):
    return tuple(store._db.iterdump()), store._db.total_changes


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
@pytest.mark.parametrize("action", ["checkpoint", "commit"])
@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_complete_core_persists_before_route_and_exact_raw_sha_is_fingerprint(
    cas, tmp_path, fmt, action, platform
):
    """真实原 CAS 往返；Windows 仅为逻辑声明，未调用 Windows Native。"""
    case = make_case(cas, tmp_path, fmt, action, platform)
    core_store = ProductGitDeliveryCoreStore(cas.store)
    before_db = _database(cas.store)
    before_blobs = {path.name for path in cas.store._blobs.iterdir()}
    body = _body(case.core)
    assert hashlib.sha256(body).hexdigest() == case.core.fingerprint
    assert "fingerprint" not in json.loads(body)
    assert encode_product_git_delivery_core(case.core, checkpoint=lambda: None) == body
    result = core_store.persist(case.core, checkpoint=lambda: None)
    assert result == case.core and result is not case.core
    assert result.baseline.source.workspace is not case.core.baseline.source.workspace
    assert result.object_scope.objects[0].material is not case.core.object_scope.objects[0].material
    assert cas.store.blob(result.fingerprint) == body
    assert {path.name for path in cas.store._blobs.iterdir()} - before_blobs == {result.fingerprint}
    restored = core_store.load(result.fingerprint, checkpoint=lambda: None)
    assert restored == result and restored is not result
    assert decode_product_git_delivery_core(body, checkpoint=lambda: None) == restored
    assert _database(cas.store) == before_db
    assert not (tmp_path / "worktrees").exists()
    assert not hasattr(restored, "approved") and not hasattr(restored, "authorize")
    payload = json.loads(body)
    assert (
        not {"route", "review_artifact", "phase", "sequence", "mac", "signature"} & payload.keys()
    )
    assert payload["baseline"]["source"] == case.core.baseline.source.model_dump(mode="json")
    assert payload["object_scope"] == case.core.model_dump(mode="json")["object_scope"]
    for node in payload["object_scope"]["objects"]:
        for entry in node["tree_entries"]:
            assert bytes.fromhex(entry["name"])
            assert entry["name"] == entry["name"].lower()


@pytest.mark.parametrize("action", ["checkpoint", "commit"])
def test_closed_writer_reopen_readonly_and_idempotent_full_durable_read(cas, tmp_path, action):
    case = make_case(cas, tmp_path, action=action)
    owner = ProductGitDeliveryCoreStore(cas.store)
    saved = owner.persist(case.core, checkpoint=lambda: None)
    assert owner.persist(case.core, checkpoint=lambda: None) == saved
    body = _body(saved)
    root = cas.store._root
    cas.store.close()
    with SQLiteWorkspaceTransactionStore(root, read_only=True) as reader:
        before_db = _database(reader)
        readonly = ProductGitDeliveryCoreStore(reader)
        assert readonly.load(saved.fingerprint, checkpoint=lambda: None) == saved
        _reject(readonly.persist, "git_delivery_core_write_failed", saved)
        assert reader.blob(saved.fingerprint) == body
        assert _database(reader) == before_db


def test_all_parent_manifest_chunk_and_full_history_references_survive(cas, tmp_path):
    case = make_case(cas, tmp_path, depth=64)
    snapshot = case.core.baseline.source.workspace
    manifest_body = cas.store.blob(snapshot.parent_closure.sha256)
    manifest = json.loads(manifest_body)
    chunks = tuple((row.copy(), cas.store.blob(row["sha256"])) for row in manifest["chunks"])
    assert len(case.history) == len(manifest["nodes"]) == 65
    owner = ProductGitDeliveryCoreStore(cas.store)
    saved = owner.persist(case.core, checkpoint=lambda: None)
    restored = owner.load(saved.fingerprint, checkpoint=lambda: None)
    assert restored.baseline == case.core.baseline
    assert restored.object_scope.external_history == case.core.object_scope.external_history
    assert restored.baseline.source.workspace.parent_closure == snapshot.parent_closure
    assert cas.store.blob(snapshot.parent_closure.sha256) == manifest_body
    assert (
        tuple((row.copy(), cas.store.blob(row["sha256"])) for row in manifest["chunks"]) == chunks
    )
    assert (
        read_workspace_parent_closure(
            restored.baseline.source.workspace, cas.store.blob, checkpoint=lambda: None
        )
        == case.history
    )


def test_returned_models_detach_nested_arguments_and_every_material_alias(cas, tmp_path):
    case = make_case(cas, tmp_path)
    owner = ProductGitDeliveryCoreStore(cas.store)
    saved = owner.persist(case.core, checkpoint=lambda: None)
    stored = cas.store.blob(saved.fingerprint)
    case.core.call.arguments["patches"].append(str(UUID(int=99)))
    assert saved.call.arguments["patches"] == [str(UUID(int=10))]
    saved.call.arguments["patches"].append(str(UUID(int=100)))
    restored = owner.load(saved.fingerprint, checkpoint=lambda: None)
    assert restored.call.arguments["patches"] == [str(UUID(int=10))]
    assert restored.object_scope is not saved.object_scope
    assert restored.object_scope.objects[0] is not saved.object_scope.objects[0]
    assert cas.store.blob(saved.fingerprint) == stored


@pytest.mark.parametrize("wrong", [None, True, {}, object()])
def test_only_actual_original_store_type_is_accepted(wrong):
    with pytest.raises(KernelError) as caught:
        ProductGitDeliveryCoreStore(wrong)
    assert caught.value.code == "git_delivery_core_store_invalid"
    assert caught.value.__cause__ is None


def test_store_subclass_and_forged_store_replacement_have_no_io(cas, tmp_path, monkeypatch):
    class StoreSubclass(SQLiteWorkspaceTransactionStore):
        pass

    subclass = object.__new__(StoreSubclass)
    with pytest.raises(KernelError) as caught:
        ProductGitDeliveryCoreStore(subclass)
    assert caught.value.code == "git_delivery_core_store_invalid"
    case = make_case(cas, tmp_path)
    owner = ProductGitDeliveryCoreStore(cas.store)
    object.__setattr__(owner, "store", subclass)
    monkeypatch.setattr(
        StoreSubclass, "put_blob", lambda *_, **kwargs: pytest.fail("拒绝前不可写入")
    )
    monkeypatch.setattr(StoreSubclass, "blob", lambda *_, **kwargs: pytest.fail("拒绝前不可读取"))
    _reject(owner.persist, "git_delivery_core_store_invalid", case.core)
    _reject(owner.load, "git_delivery_core_store_invalid", case.core.fingerprint)


@pytest.mark.parametrize(
    "fingerprint", [None, True, 1, b"a" * 64, "", "a" * 63, "a" * 65, "A" * 64, "a" * 63 + "/"]
)
def test_invalid_actual_fingerprint_never_reaches_blob(cas, fingerprint, monkeypatch):
    monkeypatch.setattr(
        cas.store, "blob", lambda *_, **kwargs: pytest.fail("非法摘要不可成为原 CAS 地址")
    )
    _reject(
        ProductGitDeliveryCoreStore(cas.store).load, "git_delivery_core_read_failed", fingerprint
    )


def test_fingerprint_subclass_is_not_silently_normalized(cas, tmp_path, monkeypatch):
    class Digest(str):
        pass

    case = make_case(cas, tmp_path)
    monkeypatch.setattr(
        cas.store, "blob", lambda *_, **kwargs: pytest.fail("摘要子类不可成为 CAS 地址")
    )
    _reject(
        ProductGitDeliveryCoreStore(cas.store).load,
        "git_delivery_core_read_failed",
        Digest(case.core.fingerprint),
    )


@pytest.mark.parametrize("wrong", [None, {}, True, b"core"])
def test_wrong_actual_core_type_rejected_before_blob_io(cas, wrong, monkeypatch):
    monkeypatch.setattr(
        cas.store, "put_blob", lambda *_, **kwargs: pytest.fail("非法 Core 不可持久化")
    )
    _reject(ProductGitDeliveryCoreStore(cas.store).persist, "git_delivery_plan_invalid", wrong)


@pytest.mark.parametrize(
    "fault",
    [
        "fingerprint",
        "extra",
        "pydantic-extra",
        "uuid-string",
        "diff-bool",
        "call-dict",
        "argument-list-subclass",
        "nested-missing",
        "scope-list",
        "scope-subclass",
        "blob-bytearray",
    ],
)
def test_forged_core_actual_types_extra_or_missing_fields_do_not_create_blob(
    cas, tmp_path, monkeypatch, fault
):
    case = make_case(cas, tmp_path)
    core = case.core.model_copy(deep=True)
    if fault == "fingerprint":
        core.__dict__["fingerprint"] = ZERO
    elif fault == "extra":
        core.__dict__["undeclared"] = "native-sensitive"
    elif fault == "pydantic-extra":
        object.__setattr__(core, "__pydantic_extra__", {"hidden": True})
    elif fault == "uuid-string":
        core.__dict__["delivery_id"] = str(core.delivery_id)
    elif fault == "diff-bool":
        core.__dict__["diff_bytes"] = True
    elif fault == "call-dict":
        core.__dict__["call"] = core.call.model_dump(mode="json")
    elif fault == "argument-list-subclass":

        class OtherList(list):
            pass

        core.call.arguments["patches"] = OtherList(core.call.arguments["patches"])
    elif fault == "nested-missing":
        core.baseline.source.workspace.__dict__.pop("parent_closure")
    elif fault == "scope-list":
        object.__setattr__(core.object_scope, "objects", list(core.object_scope.objects))
    elif fault == "scope-subclass":

        class OtherScope(type(core.object_scope)):
            pass

        scope = core.object_scope
        other = object.__new__(OtherScope)
        for name in scope.__dataclass_fields__:
            object.__setattr__(other, name, getattr(scope, name))
        core.__dict__["object_scope"] = other
    else:
        node = next(n for n in core.object_scope.objects if n.tree_entries)
        entry = node.tree_entries[0]
        object.__setattr__(entry, "name", bytearray(entry.name))
    before = observe_state(case)
    monkeypatch.setattr(
        cas.store, "put_blob", lambda *_, **kwargs: pytest.fail("严格快照失败不可写入")
    )
    _reject(ProductGitDeliveryCoreStore(cas.store).persist, "git_delivery_plan_invalid", core)
    assert observe_state(case) == before


def test_actual_core_subclass_is_not_an_entry_contract(cas, tmp_path, monkeypatch):
    class OtherCore(ProductGitDeliveryCore):
        pass

    case = make_case(cas, tmp_path)
    core = OtherCore.model_validate_json(case.core.model_dump_json())
    monkeypatch.setattr(
        cas.store, "put_blob", lambda *_, **kwargs: pytest.fail("Core 子类不可持久化")
    )
    _reject(ProductGitDeliveryCoreStore(cas.store).persist, "git_delivery_plan_invalid", core)


@pytest.mark.parametrize(
    "mutation",
    [
        "space",
        "newline",
        "bom",
        "duplicate",
        "nested-duplicate",
        "escaped-duplicate",
        "reordered",
        "escaped-key",
        "escaped-unicode",
        "invalid-utf8",
        "nan",
        "infinity",
        "array",
        "null",
        "extra",
        "includes-fingerprint",
        "uuid-alias",
        "hex-alias",
        "boolean-integer",
        "empty",
        "oversize",
        "truncated",
    ],
)
def test_original_cas_with_matching_raw_sha_still_rejects_noncanonical_or_corrupt_core(
    cas, tmp_path, mutation
):
    case = make_case(
        cas, tmp_path, action="commit" if mutation == "escaped-unicode" else "checkpoint"
    )
    body = _body(case.core)
    payload = json.loads(body)
    if mutation == "space":
        body = b" " + body
    elif mutation == "newline":
        body += b"\n"
    elif mutation == "bom":
        body = b"\xef\xbb\xbf" + body
    elif mutation == "duplicate":
        body = b'{"delivery_id":"' + str(case.core.delivery_id).encode() + b'",' + body[1:]
    elif mutation == "nested-duplicate":
        body = body.replace(b'"common_directory_identity":', b'"diff_bytes":1,"diff_bytes":')
    elif mutation == "escaped-duplicate":
        body = b'{"\\u0064elivery_id":"' + str(case.core.delivery_id).encode() + b'",' + body[1:]
    elif mutation == "reordered":
        body = json.dumps(dict(reversed(list(payload.items()))), ensure_ascii=False).encode()
    elif mutation == "escaped-key":
        body = body.replace(b'"delivery_id"', b'"\\u0064elivery_id"', 1)
    elif mutation == "escaped-unicode":
        body = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
    elif mutation == "invalid-utf8":
        body = b"\xff" + body
    elif mutation in {"nan", "infinity"}:
        body = b'{"diff_bytes":' + (b"NaN" if mutation == "nan" else b"Infinity") + b"}"
    elif mutation == "array":
        body = b"[]"
    elif mutation == "null":
        body = b"null"
    elif mutation == "extra":
        body = canonical_bytes({**payload, "unexpected": "native-sensitive"})
    elif mutation == "includes-fingerprint":
        body = canonical_bytes({**payload, "fingerprint": case.core.fingerprint})
    elif mutation == "uuid-alias":
        payload["delivery_id"] = payload["delivery_id"].replace("-", "")
        body = canonical_bytes(payload)
    elif mutation == "hex-alias":
        node = next(n for n in payload["object_scope"]["objects"] if n["tree_entries"])
        entry = next(
            e
            for n in payload["object_scope"]["objects"]
            for e in n["tree_entries"]
            if e["name"] != e["name"].upper()
        )
        assert node["tree_entries"]
        entry["name"] = entry["name"].upper()
        body = canonical_bytes(payload)
    elif mutation == "boolean-integer":
        payload["diff_bytes"] = True
        body = canonical_bytes(payload)
    elif mutation == "empty":
        body = b""
    elif mutation == "oversize":
        body = b" " * (MAX_PRODUCT_GIT_PLAN_BYTES + 1)
    else:
        body = body[:-1]
    digest = _save_raw(cas.store, body)
    before = observe_state(case)
    _reject(ProductGitDeliveryCoreStore(cas.store).load, "git_delivery_core_read_failed", digest)
    _reject(decode_product_git_delivery_core, "git_delivery_plan_invalid", body)
    assert observe_state(case) == before


@pytest.mark.parametrize(
    "name", [n for n in ProductGitDeliveryCore.model_fields if n != "fingerprint"]
)
def test_missing_any_top_level_core_field_even_default_version_is_not_completed(
    cas, tmp_path, name
):
    case = make_case(cas, tmp_path)
    payload = json.loads(_body(case.core))
    del payload[name]
    body = canonical_bytes(payload)
    digest = _save_raw(cas.store, body)
    _reject(ProductGitDeliveryCoreStore(cas.store).load, "git_delivery_core_read_failed", digest)


@pytest.mark.parametrize(
    "path",
    [
        ("baseline",),
        ("baseline", "source"),
        ("baseline", "source", "workspace"),
        ("baseline", "source", "workspace", "parent_closure"),
        ("object_scope",),
        ("object_scope", "roots"),
        ("object_scope", "roots", "base_commit"),
        ("object_scope", "objects", 0),
        ("object_scope", "objects", 0, "material"),
        ("object_scope", "external_history"),
        ("object_scope", "metrics"),
        ("object_scope", "limits"),
        ("call",),
        ("call", "arguments"),
        ("index_file_observation",),
        ("anchor_intent",),
    ],
)
def test_extra_nested_fields_are_rejected_even_when_raw_digest_is_correct(cas, tmp_path, path):
    case = make_case(cas, tmp_path)
    payload = json.loads(_body(case.core))
    target = payload
    for key in path:
        target = target[key]
    target["unexpected"] = "native-sensitive"
    digest = _save_raw(cas.store, canonical_bytes(payload))
    _reject(ProductGitDeliveryCoreStore(cas.store).load, "git_delivery_core_read_failed", digest)


@pytest.mark.parametrize(
    "path,name",
    [
        (("baseline",), "spec_version"),
        (("baseline", "source"), "spec_version"),
        (("baseline", "source", "workspace"), "spec_version"),
        (("call",), "kind"),
        (("call",), "tool_fingerprint"),
        (("anchor_intent",), "expected_missing"),
        (("object_scope", "objects", 0, "material"), "version"),
    ],
)
def test_nested_default_fields_are_not_silently_reconstructed(cas, tmp_path, path, name):
    case = make_case(cas, tmp_path)
    payload = json.loads(_body(case.core))
    target = payload
    for key in path:
        target = target[key]
    del target[name]
    digest = _save_raw(cas.store, canonical_bytes(payload))
    _reject(ProductGitDeliveryCoreStore(cas.store).load, "git_delivery_core_read_failed", digest)


@pytest.mark.parametrize("fault", ["missing", "corrupt", "directory", "symlink", "wrong-sha"])
def test_real_original_blob_missing_or_damaged_never_returns_core(cas, tmp_path, fault):
    case = make_case(cas, tmp_path)
    owner = ProductGitDeliveryCoreStore(cas.store)
    saved = owner.persist(case.core, checkpoint=lambda: None)
    path = cas.store._blobs / saved.fingerprint
    if fault == "wrong-sha":
        other = _save_raw(cas.store, b"native-sensitive")
        _reject(owner.load, "git_delivery_core_read_failed", other)
        return
    moved = path.with_name(path.name + ".original")
    path.rename(moved)
    try:
        if fault == "corrupt":
            path.write_bytes(b"native-sensitive")
            path.chmod(0o600)
        elif fault == "directory":
            path.mkdir()
        elif fault == "symlink":
            path.symlink_to(moved)
        _reject(owner.load, "git_delivery_core_read_failed", saved.fingerprint)
    finally:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
        moved.rename(path)


@pytest.mark.parametrize("entry", ["load", "persist-readback"])
@pytest.mark.parametrize("body_type", ["bytes-subclass", "bytearray", "memoryview", "changed"])
def test_exact_readback_actual_bytes_sha_and_full_body_are_required(
    cas, tmp_path, monkeypatch, entry, body_type
):
    case = make_case(cas, tmp_path)
    owner = ProductGitDeliveryCoreStore(cas.store)
    owner.persist(case.core, checkpoint=lambda: None)
    body = _body(case.core)

    class OtherBytes(bytes):
        pass

    bad = {
        "bytes-subclass": OtherBytes(body),
        "bytearray": bytearray(body),
        "memoryview": memoryview(body),
        "changed": body + b"\n",
    }[body_type]
    real_put = cas.store.put_blob
    if entry == "persist-readback":

        def write(digest, actual_body, **kwargs):
            real_put(digest, actual_body, **kwargs)
            monkeypatch.setattr(cas.store, "blob", lambda *_, **kwargs: bad)

        monkeypatch.setattr(cas.store, "put_blob", write)
        _reject(owner.persist, "git_delivery_core_write_failed", case.core)
    else:
        monkeypatch.setattr(cas.store, "blob", lambda *_, **kwargs: bad)
        _reject(owner.load, "git_delivery_core_read_failed", case.core.fingerprint)


@pytest.mark.parametrize("stage", ["put", "readback", "load"])
@pytest.mark.parametrize("failure", [RuntimeError("native-sensitive"), OSError("native-sensitive")])
def test_io_failure_public_error_is_fixed_and_does_not_remove_written_orphan(
    cas, tmp_path, monkeypatch, stage, failure
):
    case = make_case(cas, tmp_path)
    owner = ProductGitDeliveryCoreStore(cas.store)
    real_put, real_blob = cas.store.put_blob, cas.store.blob

    def failed(*args, **kwargs):
        raise failure

    if stage == "put":
        monkeypatch.setattr(cas.store, "put_blob", failed)
    elif stage == "readback":

        def write_then_fail(digest, body, **kwargs):
            real_put(digest, body, **kwargs)
            monkeypatch.setattr(cas.store, "blob", failed)

        monkeypatch.setattr(cas.store, "put_blob", write_then_fail)
    else:
        owner.persist(case.core, checkpoint=lambda: None)
        monkeypatch.setattr(cas.store, "blob", failed)
    if stage == "load":
        _reject(owner.load, "git_delivery_core_read_failed", case.core.fingerprint)
    else:
        _reject(owner.persist, "git_delivery_core_write_failed", case.core)
    if stage != "put":
        monkeypatch.setattr(cas.store, "blob", real_blob)
        assert real_blob(case.core.fingerprint) == _body(case.core)


@pytest.mark.parametrize("entry", ["persist", "load", "encode", "decode"])
@pytest.mark.parametrize(
    "failure",
    [
        TurnCancelled(),
        asyncio.CancelledError(),
        RuntimeError("native-sensitive"),
        ValueError("native-sensitive"),
        TypeError("native-sensitive"),
        KernelError("parent_cancel", "原检查点终止"),
    ],
)
@pytest.mark.parametrize("at", [1, 150])
def test_caller_checkpoint_exception_identity_is_preserved_at_snapshot_and_codec(
    cas, tmp_path, entry, failure, at
):
    case = make_case(cas, tmp_path)
    owner = ProductGitDeliveryCoreStore(cas.store)
    owner.persist(case.core, checkpoint=lambda: None)
    operation, value = {
        "persist": (owner.persist, case.core),
        "load": (owner.load, case.core.fingerprint),
        "encode": (encode_product_git_delivery_core, case.core),
        "decode": (decode_product_git_delivery_core, _body(case.core)),
    }[entry]
    count = 0

    def checkpoint():
        nonlocal count
        count += 1
        if count == at:
            raise failure

    with pytest.raises(type(failure)) as caught:
        operation(value, checkpoint=checkpoint)
    assert caught.value is failure and count == at


@pytest.mark.parametrize("stage", ["before-write", "after-write", "after-readback", "after-load"])
@pytest.mark.parametrize(
    "failure",
    [
        TurnCancelled(),
        asyncio.CancelledError(),
        ValueError("native-sensitive"),
        KernelError("parent_cancel", "原检查点终止"),
    ],
)
def test_cancellation_between_original_io_stages_never_reports_success(
    cas, tmp_path, monkeypatch, stage, failure
):
    case = make_case(cas, tmp_path)
    owner = ProductGitDeliveryCoreStore(cas.store)
    real_put, real_blob = cas.store.put_blob, cas.store.blob
    if stage == "after-load":
        owner.persist(case.core, checkpoint=lambda: None)
    armed, count, before_write_at = False, 0, None
    if stage == "before-write":

        def counting():
            nonlocal count
            count += 1

        def probe(*args, **kwargs):
            nonlocal before_write_at
            before_write_at = count
            raise RuntimeError("定位写入前最后检查点，不产生写效果")

        monkeypatch.setattr(cas.store, "put_blob", probe)
        with pytest.raises(KernelError):
            owner.persist(case.core, checkpoint=counting)
        count = 0

    def checkpoint():
        nonlocal count
        count += 1
        if armed or (stage == "before-write" and count == before_write_at):
            raise failure

    def write(digest, body, **kwargs):
        nonlocal armed
        real_put(digest, body, **kwargs)
        if stage == "after-write":
            armed = True

    def read(digest, **kwargs):
        nonlocal armed
        body = real_blob(digest, **kwargs)
        if stage in {"after-readback", "after-load"}:
            armed = True
        return body

    monkeypatch.setattr(cas.store, "put_blob", write)
    # put_blob 内部也会读原 Blob；只在公共写操作返回之后武装独立回读端口。
    if stage in {"after-readback", "after-load"}:
        if stage == "after-readback":

            def write_then_wrap_read(digest, body, **kwargs):
                real_put(digest, body, **kwargs)
                monkeypatch.setattr(cas.store, "blob", read)

            monkeypatch.setattr(cas.store, "put_blob", write_then_wrap_read)
        else:
            monkeypatch.setattr(cas.store, "blob", read)
    with pytest.raises(type(failure)) as caught:
        if stage == "after-load":
            owner.load(case.core.fingerprint, checkpoint=checkpoint)
        else:
            owner.persist(case.core, checkpoint=checkpoint)
    assert caught.value is failure
    monkeypatch.setattr(cas.store, "blob", real_blob)
    if stage == "before-write":
        assert not (cas.store._blobs / case.core.fingerprint).exists()
    else:
        assert real_blob(case.core.fingerprint) == _body(case.core)


def _sized_commit(cas, tmp_path, desired):
    """构造合同有效的确切 UTF-8 字节边界；不改变生产上限或省略原字段。"""
    small = make_case(cas, tmp_path / "small", action="commit", commit_message="\n")
    emoji_count = (desired - len(_body(small.core)) - 100) // 8
    large = make_case(
        cas, tmp_path / "large", action="commit", commit_message="🙂" * emoji_count + "\n"
    )
    fields = {
        name: getattr(large.core, name)
        for name in type(large.core).model_fields
        if name != "fingerprint"
    }
    increase = desired - len(_body(large.core))
    assert 0 <= increase <= 243
    fields["call"] = large.core.call.model_copy(
        update={"provider_call_id": "c" * (len(large.core.call.provider_call_id) + increase)}
    )
    core = sealed(ProductGitDeliveryCore, fields)
    assert len(_body(core)) == desired
    return replace(large, core=core)


@pytest.mark.parametrize("extra", [-1, 0, 1])
def test_same_512kib_exclusive_fingerprint_full_body_boundary(cas, tmp_path, extra):
    case = _sized_commit(cas, tmp_path, MAX_PRODUCT_GIT_PLAN_BYTES + extra)
    owner = ProductGitDeliveryCoreStore(cas.store)
    assert MAX_PRODUCT_GIT_PLAN_BYTES == 512 * 1024
    body = _body(case.core)
    if extra <= 0:
        saved = owner.persist(case.core, checkpoint=lambda: None)
        assert cas.store.blob(saved.fingerprint) == body
        assert owner.load(saved.fingerprint, checkpoint=lambda: None) == case.core
    else:
        before = observe_state(case)
        _reject(owner.persist, "git_delivery_plan_invalid", case.core)
        assert observe_state(case) == before
        digest = _save_raw(cas.store, body)
        _reject(owner.load, "git_delivery_core_read_failed", digest)


def test_loader_does_not_infer_material_integrity_or_current_owner_from_core_hash(cas, tmp_path):
    """此入口只耐久保存声明；原完整材料/Session/MAC/Owner 验证不能被它取代。"""
    case = make_case(cas, tmp_path)
    owner = ProductGitDeliveryCoreStore(cas.store)
    saved = owner.persist(case.core, checkpoint=lambda: None)
    material_path = cas.store._blobs / saved.object_scope.objects[0].material.cas_digest
    moved = material_path.with_name(material_path.name + ".missing")
    material_path.rename(moved)
    try:
        assert owner.load(saved.fingerprint, checkpoint=lambda: None) == saved
        assert not hasattr(saved, "owner") and not hasattr(saved, "scope")
    finally:
        moved.rename(material_path)


@pytest.mark.parametrize("entry", ["new-write", "existing-write", "load"])
@pytest.mark.parametrize(
    "failure",
    [
        TurnCancelled(),
        asyncio.CancelledError(),
        TimeoutError("native-sensitive"),
        ValueError("native-sensitive"),
        TypeError("native-sensitive"),
        OSError("native-sensitive"),
        KernelError("time_budget_exceeded", "原绝对期限终止"),
        KernelError("delivery_blob_corrupt", "原检查点同 code 终止"),
    ],
)
def test_original_constructed_store_checkpoint_identity_at_every_io_checkpoint(
    tmp_path, entry, failure
):
    """遍历真实新写/已有刷盘/读取原回调；不能替换共享 _checkpoint 或补签。"""
    count, fail_at = 0, None

    def original_checkpoint():
        nonlocal count
        count += 1
        if count == fail_at:
            raise failure

    with SQLiteWorkspaceTransactionStore(
        tmp_path / "state", checkpoint=original_checkpoint
    ) as store:
        case = make_case(GitMaterialCAS(store), tmp_path)
        owner = ProductGitDeliveryCoreStore(store)
        count = 0
        owner.persist(case.core, checkpoint=lambda: None)
        new_write_calls = count
        count = 0
        owner.persist(case.core, checkpoint=lambda: None)
        existing_write_calls = count
        count = 0
        owner.load(case.core.fingerprint, checkpoint=lambda: None)
        load_calls = count
        checkpoints = {
            "new-write": new_write_calls,
            "existing-write": existing_write_calls,
            "load": load_calls,
        }[entry]
        assert checkpoints >= 2
        blob = store._blobs / case.core.fingerprint
        moved = blob.with_name(blob.name + ".known-good")
        before_db = _database(store)
        for at in range(1, checkpoints + 1):
            if entry == "new-write":
                blob.rename(moved)
            count, fail_at = 0, at
            try:
                with pytest.raises(type(failure)) as caught:
                    if entry == "load":
                        owner.load(case.core.fingerprint, checkpoint=lambda: None)
                    else:
                        owner.persist(case.core, checkpoint=lambda: None)
                assert caught.value is failure and count == at
                assert store._checkpoint is original_checkpoint
                assert _database(store) == before_db
            finally:
                fail_at = None
                if entry == "new-write":
                    if blob.exists():
                        assert blob.read_bytes() == _body(case.core)
                        blob.unlink()
                    moved.rename(blob)
            assert not tuple(store._blobs.glob(".*.tmp"))


@pytest.mark.parametrize("phase", ["write", "readback", "load"])
@pytest.mark.parametrize("code", ["time_budget_exceeded", "delivery_blob_corrupt"])
def test_same_kernel_error_code_outside_actual_store_checkpoint_is_not_control_bypass(
    cas, tmp_path, monkeypatch, phase, code
):
    """同类型、同 code 的普通端口错误仍收敛；不得以异常分类冒充原控制来源。"""
    case = make_case(cas, tmp_path)
    owner = ProductGitDeliveryCoreStore(cas.store)
    real_put = cas.store.put_blob
    failure = KernelError(code, "native-sensitive")

    def failed(*args, **kwargs):
        raise failure

    if phase == "load":
        owner.persist(case.core, checkpoint=lambda: None)
        monkeypatch.setattr(cas.store, "blob", failed)
        error = _reject(owner.load, "git_delivery_core_read_failed", case.core.fingerprint)
    else:
        if phase == "write":
            monkeypatch.setattr(cas.store, "put_blob", failed)
        else:

            def write(digest, body, **kwargs):
                real_put(digest, body, **kwargs)
                monkeypatch.setattr(cas.store, "blob", failed)

            monkeypatch.setattr(cas.store, "put_blob", write)
        error = _reject(owner.persist, "git_delivery_core_write_failed", case.core)
    assert error is not failure


def test_fresh_python_process_loads_original_durable_core_without_route_or_sql_entry(cas, tmp_path):
    case = make_case(cas, tmp_path, action="commit")
    owner = ProductGitDeliveryCoreStore(cas.store)
    saved = owner.persist(case.core, checkpoint=lambda: None)
    root = cas.store._root
    cas.store.close()
    script = """
import hashlib, json, sys
sys.path.insert(0, sys.argv[1])
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_delivery_plan_wire import encode_product_git_delivery_core
with SQLiteWorkspaceTransactionStore(sys.argv[2], read_only=True) as store:
    core = ProductGitDeliveryCoreStore(store).load(sys.argv[3], checkpoint=lambda: None)
    body = encode_product_git_delivery_core(core, checkpoint=lambda: None)
    assert hashlib.sha256(body).hexdigest() == sys.argv[3] == core.fingerprint
    assert store.blob(core.fingerprint) == body
    assert store._db.execute("SELECT count(*) FROM workspace_transactions").fetchone() == (0,)
    assert "fingerprint" not in json.loads(body)
    print(core.fingerprint)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            str(Path(store_module.__file__).resolve().parents[2]),
            str(root),
            saved.fingerprint,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.stdout.strip() == saved.fingerprint and result.stderr == ""


@pytest.mark.parametrize("mode", ["default", "explicit-original", "explicit-caller"])
@pytest.mark.parametrize("entry", ["new-put", "existing-put", "blob"])
@pytest.mark.parametrize(
    "failure",
    [
        TurnCancelled(),
        asyncio.CancelledError(),
        OSError("native-sensitive"),
        KernelError("time_budget_exceeded", "原绝对期限终止"),
    ],
)
def test_original_cas_default_raw_and_explicit_typed_control_at_every_checkpoint(
    tmp_path, mode, entry, failure
):
    """默认入口原异常不变；显式模式只将实际原/调用方检查点包为单个标记。"""
    count, fail_at = 0, None
    order = []
    source = "caller" if mode == "explicit-caller" else "original"

    def tick(kind):
        nonlocal count
        order.append(kind)
        if source == kind:
            count += 1
            if count == fail_at:
                raise failure

    def original():
        tick("original")

    def caller():
        tick("caller")

    body = b"per-operation-durable-core-control"
    digest = hashlib.sha256(body).hexdigest()
    with SQLiteWorkspaceTransactionStore(tmp_path / "state", checkpoint=original) as store:
        kwargs = {} if mode == "default" else {"checkpoint": caller}
        store.put_blob(digest, body, **kwargs)
        blob = store._blobs / digest
        backup = blob.with_name(blob.name + ".known-good")
        if entry == "new-put":
            blob.rename(backup)
        count, order = 0, []
        if entry == "blob":
            store.blob(digest, **kwargs)
        else:
            store.put_blob(digest, body, **kwargs)
        total = count
        assert total >= 2
        assert order == (
            ["original"] * total if mode == "default" else ["original", "caller"] * total
        )
        if entry == "new-put":
            blob.unlink()
            backup.rename(blob)
        for at in range(1, total + 1):
            if entry == "new-put":
                blob.rename(backup)
            count, fail_at = 0, at
            try:
                with pytest.raises(
                    type(failure) if mode == "default" else UpstreamCheckpointError
                ) as caught:
                    if entry == "blob":
                        store.blob(digest, **kwargs)
                    else:
                        store.put_blob(digest, body, **kwargs)
                assert (caught.value if mode == "default" else caught.value.error) is failure
                assert count == at and store._checkpoint is original
            finally:
                fail_at = None
                if entry == "new-put":
                    if blob.exists():
                        assert blob.read_bytes() == body
                        blob.unlink()
                    backup.rename(blob)
            assert not tuple(store._blobs.glob(".*.tmp"))


@pytest.mark.parametrize("at", [1, 2, 3])
@pytest.mark.parametrize("existing", [False, True])
def test_cas_io_error_in_internal_confirm_or_public_readback_is_not_control_marker(
    cas, tmp_path, monkeypatch, at, existing
):
    """尤其确认回读不再套 controlled_io，否则普通 CAS 错误会被误当控制放行。"""
    case = make_case(cas, tmp_path)
    owner = ProductGitDeliveryCoreStore(cas.store)
    if existing:
        owner.persist(case.core, checkpoint=lambda: None)
    real = cas.store._read_blob
    count = 0
    native_error = KernelError("time_budget_exceeded", "native-sensitive")

    def read(digest):
        nonlocal count
        if digest == case.core.fingerprint:
            count += 1
            if count == at:
                raise native_error
        return real(digest)

    monkeypatch.setattr(cas.store, "_read_blob", read)
    error = _reject(owner.persist, "git_delivery_core_write_failed", case.core)
    assert error is not native_error and count == at
