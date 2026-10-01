"""原材料纯工厂：完整真实类型、正文容量与原 Git 对象头认证，不做语义 fsck。"""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import MAX_TRANSACTION_FILE_BYTES
from harnessix.delivery.git_object_material import GitObjectMaterial


class _Text(str):
    pass


class _Bytes(bytes):
    pass


class _Unexpected:
    def __eq__(self, other):
        raise RuntimeError("private-native-error")


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("kind", ["blob", "tree", "commit"])
@pytest.mark.parametrize("size", [0, 1, MAX_TRANSACTION_FILE_BYTES], ids=["empty", "one", "8MiB"])
def test_factory_full_body_original_header_and_oid(object_format, kind, size):
    body = (bytes(range(256)) * ((size + 255) // 256))[:size]
    expected = hashlib.new(object_format, kind.encode() + b" " + str(size).encode() + b"\0" + body)
    value = GitObjectMaterial.from_body(kind, object_format, body)
    assert type(value) is GitObjectMaterial
    assert value.object_type == kind and value.object_format == object_format
    assert value.object_id == expected.hexdigest()
    assert value.body is body and value.body_bytes == size
    assert value.body_sha256 == hashlib.sha256(body).hexdigest()
    assert "body=" not in repr(value)
    with pytest.raises(FrozenInstanceError):
        value.body = b"replaced"


@pytest.mark.parametrize(
    "field,value",
    [
        ("object_type", "tag"),
        ("object_type", "Blob"),
        ("object_type", b"blob"),
        ("object_type", True),
        ("object_type", _Text("blob")),
        ("object_format", "sha512"),
        ("object_format", "SHA1"),
        ("object_format", b"sha1"),
        ("object_format", False),
        ("object_format", _Text("sha1")),
        ("object_format", _Unexpected()),
        ("body", "private-value"),
        ("body", bytearray(b"private-value")),
        ("body", memoryview(b"private-value")),
        ("body", True),
        ("body", _Bytes(b"private-value")),
    ],
)
def test_factory_rejects_non_actual_field_types_without_sensitive_error(field, value):
    kwargs = {"object_type": "blob", "object_format": "sha1", "body": b"private-value"}
    kwargs[field] = value
    with pytest.raises(KernelError) as error:
        GitObjectMaterial.from_body(**kwargs)
    assert "private-value" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("kind", ["blob", "tree", "commit"])
def test_factory_one_byte_over_original_limit_is_fixed_error(object_format, kind):
    with pytest.raises(KernelError) as error:
        GitObjectMaterial.from_body(kind, object_format, b"x" * (MAX_TRANSACTION_FILE_BYTES + 1))
    assert error.value.code == "git_material_limit"


def test_factory_rejects_subclass_and_reuses_construct_validation(monkeypatch):
    class Other(GitObjectMaterial):
        pass

    with pytest.raises(KernelError):
        Other.from_body("blob", "sha1", b"x")
    original = GitObjectMaterial.__post_init__
    observed = []

    def validated(self):
        observed.append(self)
        original(self)

    monkeypatch.setattr(GitObjectMaterial, "__post_init__", validated)
    material = GitObjectMaterial.from_body("blob", "sha256", b"x\0\xff")
    assert observed == [material]
    object.__setattr__(material, "body", b"changed")
    with pytest.raises(KernelError) as error:
        GitObjectMaterial(
            material.object_type, material.object_id, material.object_format, material.body
        )
    assert error.value.code == "git_material_oid_mismatch"
