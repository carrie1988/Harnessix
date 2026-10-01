"""原 SQLite Workspace CAS 的类型材料绑定；不证明对象图或产品发布闭包。"""

from __future__ import annotations

import hashlib
import traceback
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import MAX_TRANSACTION_FILE_BYTES
from harnessix.delivery.git_material_cas import GitMaterialCAS, GitObjectMaterialReference
from harnessix.delivery.git_object_material import GitObjectMaterial
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore

_LIMIT = MAX_TRANSACTION_FILE_BYTES
_VERSION = "harnessix.git-object-material-reference/v1"
_MARKER = b"private-material-value\0binary"


def _oid(object_format, object_type, body):
    return hashlib.new(
        object_format, f"{object_type} {len(body)}\0".encode("ascii") + body
    ).hexdigest()


def _material(object_type="blob", object_format="sha1", body=_MARKER):
    return GitObjectMaterial(
        object_type, _oid(object_format, object_type, body), object_format, body
    )


def _reference(material=None):
    material = material or _material()
    return GitObjectMaterialReference(
        material.object_type,
        material.object_id,
        material.object_format,
        material.body_sha256,
        material.body_bytes,
        material.body_sha256,
    )


def _body(object_type, object_format, size):
    if object_type == "blob":
        return (bytes(range(256)) * ((size + 255) // 256))[:size]
    if object_type == "commit":
        tree = _oid(object_format, "tree", b"")
        header = (
            f"tree {tree}\n"
            "author Harnessix Test <test@harnessix.invalid> 0 +0000\n"
            "committer Harnessix Test <test@harnessix.invalid> 0 +0000\n\n"
        ).encode("ascii")
        return header + b"x" * (max(size, len(header)) - len(header))
    if size == 0:
        return b""
    # 有序普通文件条目；只造合法测试材料，不遍历或声明子对象业务归属。
    child = bytes.fromhex(_oid(object_format, "blob", b"x"))
    entry_size = len(b"100644 f00000000\0") + len(child)
    count = size // entry_size - 1
    body = b"".join(b"100644 f%08d\0" % index + child for index in range(count))
    remaining = size - len(body)
    return body + b"100644 " + b"z" * (remaining - 8 - len(child)) + b"\0" + child


@pytest.fixture
def actual_store(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        yield store


def _seed_actual_store(store, tmp_path, body):
    # 仅使用原公开事务保存准备读取夹具；不以私有写方法替代待验证的persist。
    workspace = tmp_path / "fixture-workspace"
    workspace.mkdir()
    prepared = prepare_workspace_transaction(
        workspace,
        {"fixture.bin": DesiredWorkspaceFile(body, 0o644)},
        request_id="cas-read-fixture",
        now=datetime(2026, 10, 1, tzinfo=UTC),
    )
    store.save(prepared)


def _assert_fixed(error, code):
    assert isinstance(error, KernelError)
    assert error.code == code
    assert _MARKER.decode("ascii") not in str(error)
    assert "native-sensitive-error" not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None


def test_reference_is_frozen_strict_and_has_only_canonical_fields():
    material = _material()
    reference = _reference(material)
    value = reference.binding()
    assert set(value) == {
        "version",
        "object_type",
        "object_id",
        "object_format",
        "body_sha256",
        "body_bytes",
        "cas_digest",
    }
    assert value["version"] == _VERSION
    assert reference.cas_digest == reference.body_sha256 == material.body_sha256
    assert GitObjectMaterialReference.from_binding(value) == reference
    with pytest.raises(FrozenInstanceError):
        reference.body_bytes = 0
    assert _MARKER.decode("ascii") not in repr(material)
    assert _MARKER.decode("ascii") not in repr(reference)
    assert _MARKER.decode("ascii") not in repr(value)
    assert "body=" not in repr(reference)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "git-object/v2"),
        ("version", True),
        ("object_type", "tag"),
        ("object_type", True),
        ("object_type", b"blob"),
        ("object_format", "SHA1"),
        ("object_format", "sha512"),
        ("object_format", False),
        ("object_id", "f" * 64),
        ("object_id", "F" * 40),
        ("object_id", "0" * 39),
        ("object_id", "0" * 40 + "\n"),
        ("object_id", True),
        ("body_sha256", "a" * 63),
        ("body_sha256", "A" * 64),
        ("body_sha256", False),
        ("body_bytes", True),
        ("body_bytes", False),
        ("body_bytes", "0"),
        ("body_bytes", 0.0),
        ("body_bytes", -1),
        ("body_bytes", _LIMIT + 1),
        ("cas_digest", "a" * 64),
        ("cas_digest", True),
    ],
)
def test_reference_rejects_invalid_field_types_and_bindings(field, value):
    binding = _reference().binding()
    binding[field] = value
    with pytest.raises(KernelError) as rejected:
        GitObjectMaterialReference.from_binding(binding)
    _assert_fixed(rejected.value, "git_material_cas_reference_invalid")


@pytest.mark.parametrize(
    "extra", ["body", "role", "references", "ancestor", "approval", "author", "ref", "mac"]
)
def test_reference_rejects_noncanonical_extra_fields(extra):
    binding = _reference().binding()
    binding[extra] = _MARKER
    with pytest.raises(KernelError) as rejected:
        GitObjectMaterialReference.from_binding(binding)
    _assert_fixed(rejected.value, "git_material_cas_reference_invalid")


@pytest.mark.parametrize("missing", sorted(_reference().binding()))
def test_reference_requires_every_canonical_field(missing):
    binding = _reference().binding()
    del binding[missing]
    with pytest.raises(KernelError) as rejected:
        GitObjectMaterialReference.from_binding(binding)
    _assert_fixed(rejected.value, "git_material_cas_reference_invalid")


@pytest.mark.parametrize("value", [None, [], b"{}", "{}", True])
def test_reference_rejects_non_dictionary_binding(value):
    with pytest.raises(KernelError) as rejected:
        GitObjectMaterialReference.from_binding(value)
    _assert_fixed(rejected.value, "git_material_cas_reference_invalid")


def test_reference_rejects_noncanonical_key_class():
    class TextKey(str):
        pass

    binding = {TextKey(key): value for key, value in _reference().binding().items()}
    with pytest.raises(KernelError) as rejected:
        GitObjectMaterialReference.from_binding(binding)
    _assert_fixed(rejected.value, "git_material_cas_reference_invalid")


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("object_type", ["blob", "tree", "commit"])
@pytest.mark.parametrize("size", [0, _LIMIT], ids=["legal-minimum", "8MiB"])
def test_read_actual_sqlite_cas_all_types_formats_and_complete_capacity(
    actual_store,
    tmp_path,
    object_format,
    object_type,
    size,
):
    body = _body(object_type, object_format, size)
    material = _material(object_type, object_format, body)
    _seed_actual_store(actual_store, tmp_path, body)
    result = GitMaterialCAS(actual_store).read(_reference(material))
    assert type(result) is GitObjectMaterial
    assert result == material
    assert result.body == body
    assert result.body_bytes == (_LIMIT if size else len(body))
    assert result.body_sha256 == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize("damage", ["missing", "same-length-corrupt", "truncated", "oversize"])
def test_read_rejects_actual_missing_or_corrupt_cas(actual_store, tmp_path, damage):
    material = _material()
    reference = _reference(material)
    _seed_actual_store(actual_store, tmp_path, material.body)
    path = tmp_path / "state/blobs" / reference.cas_digest
    if damage == "missing":
        path.unlink()
    else:
        body = (
            b"x" * len(material.body)
            if damage == "same-length-corrupt"
            else (material.body[:-1] if damage == "truncated" else b"x" * (_LIMIT + 1))
        )
        path.write_bytes(body)
    with pytest.raises(KernelError) as rejected:
        GitMaterialCAS(actual_store).read(reference)
    _assert_fixed(rejected.value, "git_material_cas_read_failed")


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("change", ["object-id", "type", "length"])
def test_read_checks_git_header_oid_and_length_not_only_body_sha(
    actual_store,
    tmp_path,
    object_format,
    change,
):
    material = _material("blob", object_format)
    reference = _reference(material)
    _seed_actual_store(actual_store, tmp_path, material.body)
    changes = {
        "object-id": {"object_id": "0" * len(material.object_id)},
        "type": {"object_type": "tree"},
        "length": {"body_bytes": material.body_bytes + 1},
    }
    wrong = replace(reference, **changes[change])
    with pytest.raises(KernelError) as rejected:
        GitMaterialCAS(actual_store).read(wrong)
    _assert_fixed(rejected.value, "git_material_cas_read_failed")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("object_format", "sha256"),
        ("object_type", "tag"),
        ("body_bytes", True),
        ("body_bytes", _LIMIT + 1),
        ("body_sha256", "a" * 64),
        ("cas_digest", False),
        ("version", "other/v1"),
    ],
)
def test_read_revalidates_reference_bypassed_frozen_construction(actual_store, field, value):
    reference = _reference()
    object.__setattr__(reference, field, value)
    with pytest.raises(KernelError) as rejected:
        GitMaterialCAS(actual_store).read(reference)
    _assert_fixed(rejected.value, "git_material_cas_reference_invalid")
    with pytest.raises(KernelError):
        reference.binding()


@pytest.mark.parametrize("reference", [None, {}, True, object()])
def test_read_rejects_wrong_actual_reference_class(actual_store, reference):
    with pytest.raises(KernelError) as rejected:
        GitMaterialCAS(actual_store).read(reference)
    _assert_fixed(rejected.value, "git_material_cas_reference_invalid")


def test_read_translates_native_store_exception_without_original_text(actual_store, monkeypatch):
    def failed_read(digest):
        raise OSError("native-sensitive-error " + _MARKER.decode("ascii"))

    monkeypatch.setattr(actual_store, "blob", failed_read)
    with pytest.raises(KernelError) as rejected:
        GitMaterialCAS(actual_store).read(_reference())
    _assert_fixed(rejected.value, "git_material_cas_read_failed")


def test_read_rechecks_body_sha_even_if_store_reader_returns_wrong_digest_body(
    actual_store,
    monkeypatch,
):
    material = _material()
    reference = replace(_reference(material), body_sha256="0" * 64, cas_digest="0" * 64)
    monkeypatch.setattr(actual_store, "blob", lambda digest: material.body)
    with pytest.raises(KernelError) as rejected:
        GitMaterialCAS(actual_store).read(reference)
    _assert_fixed(rejected.value, "git_material_cas_read_failed")


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("object_type", ["blob", "tree", "commit"])
@pytest.mark.parametrize("size", [0, _LIMIT], ids=["legal-minimum", "8MiB"])
def test_persist_actual_sqlite_cas_all_types_formats_and_complete_capacity(
    actual_store,
    tmp_path,
    object_format,
    object_type,
    size,
):
    material = _material(object_type, object_format, _body(object_type, object_format, size))
    adapter = GitMaterialCAS(actual_store)
    reference = adapter.persist(material)
    assert reference == _reference(material)
    assert adapter.read(reference) == material
    assert actual_store.blob(reference.cas_digest) == material.body
    assert sorted(p.name for p in (tmp_path / "state/blobs").iterdir()) == [reference.cas_digest]
    assert not list((tmp_path / "state").rglob("git-delivery.db"))
    assert "state" not in repr(adapter)


def test_persist_deduplicates_same_body_without_confusing_type_or_format(actual_store, tmp_path):
    adapter = GitMaterialCAS(actual_store)
    # 空blob/tree共用合法正文；有类型引用及Git OID仍独立，不能由CAS摘要代替。
    materials = [
        _material(kind, fmt, b"") for kind in ("blob", "tree") for fmt in ("sha1", "sha256")
    ]
    references = [adapter.persist(material) for material in materials]
    assert len(set(references)) == 4
    assert len({r.object_id for r in references}) == 4
    assert len({r.cas_digest for r in references}) == 1
    assert len(list((tmp_path / "state/blobs").iterdir())) == 1
    assert [adapter.read(r) for r in references] == materials
    assert adapter.persist(materials[0]) == references[0]


def test_persist_reopens_and_reads_with_original_readonly_store(tmp_path):
    state = tmp_path / "state"
    material = _material()
    with SQLiteWorkspaceTransactionStore(state) as writer:
        reference = GitMaterialCAS(writer).persist(material)
    with SQLiteWorkspaceTransactionStore(state, read_only=True) as reader:
        adapter = GitMaterialCAS(reader)
        assert adapter.read(reference) == material
        with pytest.raises(KernelError) as rejected:
            adapter.persist(material)
        _assert_fixed(rejected.value, "git_material_cas_write_failed")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("object_id", "0" * 40),
        ("object_type", "tag"),
        ("object_format", "sha256"),
        ("body", _MARKER + b"changed"),
        ("body", bytearray(_MARKER)),
        ("body", b"x" * (_LIMIT + 1)),
    ],
    ids=["wrong-oid", "wrong-type", "wrong-format", "changed-body", "mutable-body", "oversize"],
)
def test_invalid_material_rejected_before_persist_creates_any_blob(
    actual_store,
    tmp_path,
    field,
    value,
):
    material = _material()
    object.__setattr__(material, field, value)
    with pytest.raises(KernelError) as rejected:
        GitMaterialCAS(actual_store).persist(material)
    _assert_fixed(rejected.value, "git_material_cas_invalid")
    assert not list((tmp_path / "state/blobs").iterdir())


@pytest.mark.parametrize("material", [None, {}, True, object()])
def test_wrong_actual_material_class_rejected_before_persist(actual_store, material):
    with pytest.raises(KernelError) as rejected:
        GitMaterialCAS(actual_store).persist(material)
    _assert_fixed(rejected.value, "git_material_cas_invalid")


def test_actual_reference_and_material_subclasses_are_not_silently_accepted(actual_store):
    class OtherReference(GitObjectMaterialReference):
        pass

    class OtherMaterial(GitObjectMaterial):
        pass

    reference = OtherReference(**_reference().binding())
    material = OtherMaterial("blob", _oid("sha1", "blob", _MARKER), "sha1", _MARKER)
    with pytest.raises(KernelError) as rejected:
        GitMaterialCAS(actual_store).read(reference)
    _assert_fixed(rejected.value, "git_material_cas_reference_invalid")
    with pytest.raises(KernelError) as rejected:
        GitMaterialCAS(actual_store).persist(material)
    _assert_fixed(rejected.value, "git_material_cas_invalid")


@pytest.mark.parametrize("phase", ["write", "readback"])
def test_persist_translates_store_failures_and_never_returns_reference(
    actual_store,
    monkeypatch,
    phase,
):
    def failed_io(*args):
        raise RuntimeError("native-sensitive-error " + _MARKER.decode("ascii"))

    monkeypatch.setattr(actual_store, "put_blob" if phase == "write" else "blob", failed_io)
    with pytest.raises(KernelError) as rejected:
        GitMaterialCAS(actual_store).persist(_material())
    _assert_fixed(rejected.value, "git_material_cas_write_failed")


def test_persist_verifies_actual_readback_after_public_write(actual_store, monkeypatch, tmp_path):
    material = _material()
    real_blob = actual_store.blob
    real_put = actual_store.put_blob

    def write_then_break_readback(digest, body):
        real_put(digest, body)
        monkeypatch.setattr(actual_store, "blob", lambda digest: b"corrupted-readback")

    monkeypatch.setattr(actual_store, "put_blob", write_then_break_readback)
    with pytest.raises(KernelError) as rejected:
        GitMaterialCAS(actual_store).persist(material)
    _assert_fixed(rejected.value, "git_material_cas_write_failed")
    monkeypatch.setattr(actual_store, "blob", real_blob)
    # 返回失败不删除已经完整写入的合法孤儿；本切片不建立业务目录或归属。
    assert real_blob(material.body_sha256) == material.body
    assert len(list((tmp_path / "state/blobs").iterdir())) == 1
