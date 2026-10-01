"""完整目录规范字节、双摘要、严格 JSON 与无效果取消的有限回归。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from uuid import UUID

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_inventory_contracts import GitInventoryPrefixProjection
from harnessix.delivery.git_inventory_wire import (
    decode_git_inventory_prefix_projection,
    decode_git_object_inventory,
    encode_git_inventory_prefix_projection,
    encode_git_object_inventory,
    git_inventory_scope_digest,
    git_object_inventory_digest,
)
from harnessix.execution.contracts import canonical_digest
from tests.delivery.test_git_inventory_contracts import _check, _inventory


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("action", ["checkpoint", "commit"])
@pytest.mark.parametrize("phase", ["materials_ready", "effect_closed"])
def test_complete_body_roundtrip_and_original_canonical_semantics(object_format, action, phase):
    """完整规范字节往返与原摘要算法一致，各原字段没有省略。"""
    value = _inventory(object_format, action, phase)
    body = encode_git_object_inventory(value, checkpoint=_check)
    data = json.loads(body)
    assert (
        body
        == json.dumps(
            data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    )
    assert decode_git_object_inventory(body, checkpoint=_check) == value
    assert set(data["roots"]["base_commit"]) == {"object_type", "object_id", "object_format"}
    assert set(data["objects"][0]["material"]) == {
        "version",
        "object_type",
        "object_id",
        "object_format",
        "body_sha256",
        "body_bytes",
        "cas_digest",
    }
    assert all(
        "name_hex" in e and "name" not in e for o in data["objects"] for e in o["tree_entries"]
    )
    assert data["inventory_sha256"] == canonical_digest(
        {k: v for k, v in data.items() if k != "inventory_sha256"}
    )
    assert value.inventory_sha256 != hashlib.sha256(body).hexdigest()
    scope = {
        key: data[key]
        for key in (
            "platform",
            "roots",
            "objects",
            "external_history",
            "limits",
            "max_parents",
            "metrics",
        )
    }
    scope["spec_version"] = "harnessix.git-inventory-scope/v1"
    assert canonical_digest(scope) == value.binding.object_scope_digest


def test_scope_excludes_stage_and_identity_without_dropping_full_inventory_binding():
    """scope 可独立于身份，目录自身摘要仍覆盖完整绑定。"""
    value = _inventory()
    changed = replace(
        value,
        inventory_id=UUID(int=50),
        binding=replace(
            value.binding,
            route_fingerprint="2" * 64,
        ),
    )
    assert (
        git_inventory_scope_digest(changed, checkpoint=_check) == value.binding.object_scope_digest
    )
    assert git_object_inventory_digest(changed, checkpoint=_check) != value.inventory_sha256


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b + b"\n",
        lambda b: b" " + b,
        lambda b: b.replace(b'"domain_sequence":0', b'"domain_sequence":0.0'),
        lambda b: b.replace(b'"domain_sequence":0', b'"domain_sequence":false'),
        lambda b: b.replace(b'"domain_sequence":0', b'"domain_sequence":NaN'),
        lambda b: b.replace(b'"domain_sequence":0', b'"domain_sequence":Infinity'),
        lambda b: b.replace(b'"domain_sequence":0', b'"domain_sequence":0,"domain_sequence":0'),
        lambda b: b.replace(b'"phase":"materials_ready"', b'"phase":"materials_ready","extra":1'),
        lambda b: b.replace(b'"phase":"materials_ready",', b""),
        lambda b: b.replace(
            b'"kind":"base_commit_parent_edges"',
            b'"kind":"base_commit_parent_edges","kind":"base_commit_parent_edges"',
        ),
        lambda b: b.replace(b'"name_hex":"61"', b'"name_hex":"6A"'),
        lambda b: b.replace(b'"name_hex":"61"', b'"name_hex":"6"'),
        lambda b: b.replace(b'"name_hex":"61"', b'"name_hex":"zz"'),
        lambda b: b.replace(b'"name_hex":"61"', b'"name_hex":null'),
        lambda b: b.replace(b',"spec_version":"harnessix.git-object-inventory/v1"', b""),
        lambda b: b + b"{}",
        lambda b: b"\xff" + b,
    ],
)
def test_wire_rejects_noncanonical_missing_extra_or_duplicate_fields(mutation):
    """拒绝缺键、额外键、重复键和等义但非规范字节。"""
    body = encode_git_object_inventory(_inventory(), checkpoint=_check)
    assert mutation(body) != body
    with pytest.raises(KernelError):
        decode_git_object_inventory(mutation(body), checkpoint=_check)


@pytest.mark.parametrize(
    "operation",
    [encode_git_object_inventory, git_inventory_scope_digest, git_object_inventory_digest],
)
def test_each_wire_operation_has_explicit_original_exception_checkpoint(operation):
    """摘要和编码入口保留调用方检查点异常的原身份。"""
    value = _inventory()
    exception = ValueError("fixed checkpoint")

    def check():
        """在原计数检查点抛出指定异常，不替换异常对象。"""
        raise exception

    with pytest.raises(ValueError) as error:
        operation(value, checkpoint=check)
    assert error.value is exception


@pytest.mark.parametrize(
    "exception",
    [
        json.JSONDecodeError("fixed checkpoint", "", 0),
        ValueError("fixed checkpoint"),
        OverflowError("fixed checkpoint"),
        RecursionError("fixed checkpoint"),
        TurnCancelled(),
        asyncio.CancelledError(),
        KernelError("fixture", "固定检查点"),
    ],
)
def test_json_pairs_hook_preserves_original_exception_from_callback(exception):
    """解析钩子里的取消、数值及解析类回调异常都原对象传播。"""
    body = encode_git_object_inventory(_inventory(), checkpoint=_check)
    count = 0

    def check():
        """在原计数检查点抛出指定异常，不替换异常对象。"""
        nonlocal count
        count += 1
        if count == 3:
            raise exception

    with pytest.raises(type(exception)) as error:
        decode_git_object_inventory(body, checkpoint=check)
    assert error.value is exception


def test_json_long_integer_is_fixed_domain_error():
    """超过原解析器数字长度的真实输入统一为固定领域错误。"""
    body = b'{"domain_sequence":' + b"9" * 4301 + b"}"
    with pytest.raises(KernelError) as error:
        decode_git_object_inventory(body, checkpoint=_check)
    assert error.value.code == "git_inventory_invalid"


@pytest.mark.parametrize("bad", [bytearray(b"{}"), "{}", None, 1])
def test_wire_requires_actual_bytes(bad):
    """完整持久字节不接受 bytearray 或隐式文本转换。"""
    with pytest.raises(KernelError) as error:
        decode_git_object_inventory(bad, checkpoint=_check)
    assert error.value.code == "git_inventory_invalid"


def test_original_64mib_record_boundary_is_not_relaxed():
    """读端完整正文超过原 64MiB 时在解析前拒绝。"""
    with pytest.raises(KernelError) as error:
        decode_git_object_inventory(b" " * (64 * 1024 * 1024 + 1), checkpoint=_check)
    assert error.value.code == "git_inventory_record_limit"


def test_declared_digests_cannot_be_trusted_or_repaired_by_encoder():
    """编码不自动修复或相信调用方声明摘要。"""
    value = _inventory()
    object.__setattr__(value, "inventory_sha256", "f" * 64)
    with pytest.raises(KernelError) as error:
        encode_git_object_inventory(value, checkpoint=_check)
    assert error.value.code == "git_inventory_graph_mismatch"
    value = _inventory()
    object.__setattr__(value.binding, "object_scope_digest", "f" * 64)
    with pytest.raises(KernelError):
        encode_git_object_inventory(value, checkpoint=_check)


def test_prefix_projection_canonical_complete_roundtrip():
    """普通子投影必须完整规范往返，尾部换行也拒绝。"""
    value = GitInventoryPrefixProjection(
        UUID(int=1), UUID(int=2), UUID(int=3), 1, 2, "1" * 64, "2" * 64, "3" * 64
    )
    body = encode_git_inventory_prefix_projection(value, checkpoint=_check)
    assert decode_git_inventory_prefix_projection(body, checkpoint=_check) == value
    with pytest.raises(KernelError):
        decode_git_inventory_prefix_projection(body + b"\n", checkpoint=_check)


@pytest.mark.parametrize("delta", [0, 1])
def test_canonical_encoder_exact_64mib_and_plus_one(delta):
    """规范编码内核确实收集完整 64MiB；超限不能返回半正文。"""
    from harnessix.delivery.git_inventory_wire import _encode

    capacity = 64 * 1024 * 1024
    payload = {"x": "a" * (capacity - len(b'{"x":""}') + delta)}
    if delta:
        with pytest.raises(KernelError) as error:
            _encode(payload, _check)
        assert error.value.code == "git_inventory_record_limit"
    else:
        body = _encode(payload, _check)
        assert len(body) == capacity
        assert body.startswith(b'{"x":"') and body.endswith(b'"}')
        assert (
            hashlib.sha256(body).digest()
            == hashlib.sha256(
                json.dumps(
                    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            ).digest()
        )


@pytest.mark.parametrize("exception", [ValueError(), OverflowError()])
def test_parser_numeric_errors_are_normalized_without_global_change(monkeypatch, exception):
    """直接解析器数值错误转固定域码，不修改进程数字长度准入。"""
    import sys

    before = sys.get_int_max_str_digits()

    def loads(_text, **_kwargs):
        """仅注入解析器数值错误，验证域映射且不改全局限制。"""
        raise exception

    monkeypatch.setattr(json, "loads", loads)
    with pytest.raises(KernelError) as error:
        decode_git_object_inventory(b"{}", checkpoint=_check)
    assert error.value.code == "git_inventory_invalid"
    assert sys.get_int_max_str_digits() == before
