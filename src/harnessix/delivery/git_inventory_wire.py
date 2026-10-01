"""Git 对象目录的完整规范持久字节与摘要；不是 CAS、图或业务认证端口。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from types import UnionType
from typing import Literal, NoReturn, cast, get_args, get_origin
from uuid import UUID

from pydantic import JsonValue

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_inventory_contracts import (
    _TYPES,
    GitInventoryPrefixProjection,
    GitObjectInventory,
    _annotations,
    _invalid,
    _KnownModel,
    _snapshot_inventory_shape,
    snapshot_git_inventory_prefix_projection,
    snapshot_git_object_inventory,
)
from harnessix.delivery.git_object_references import GitTreeEntry
from harnessix.delivery.git_tree_closure import GitTreeClosureLimits

# 完整目录的固定规范字节上限，不是宿主执行预算或认证发布额度。
_MAX_GIT_INVENTORY_RECORD_BYTES = 64 * 1024 * 1024


def _wire(value: object, checkpoint: Callable[[], None]) -> JsonValue:
    """投影有限模型的完整字段，不输出对象正文或添加任何默认字段。"""
    checkpoint()
    if type(value) is UUID:
        return str(value)
    if type(value) is bytes:
        return value.hex()
    if type(value) is tuple:
        return [_wire(item, checkpoint) for item in cast(tuple[object, ...], value)]
    if type(value) in {str, int, type(None)}:
        return cast(str | int | None, value)
    kind = type(value)
    if kind not in _TYPES:
        raise _invalid()
    result: dict[str, JsonValue] = {}
    for name in _annotations(kind):
        checkpoint()
        key = "name_hex" if kind is GitTreeEntry and name == "name" else name
        result[key] = _wire(getattr(value, name), checkpoint)
    return result


def _chunks(payload: JsonValue, checkpoint: Callable[[], None]) -> Iterator[bytes]:
    """按原规范 JSON 产出有检查点字节块，完整编码不得超过原 64MiB。"""
    encoder = json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    iterator = encoder.iterencode(payload)
    size = 0
    while True:
        checkpoint()
        try:
            part = next(iterator)
        except StopIteration:
            break
        except (ValueError, TypeError, RecursionError):
            raise _invalid() from None
        for offset in range(0, len(part), 16384):
            checkpoint()
            try:
                piece = part[offset : offset + 16384].encode("utf-8", "strict")
            except UnicodeError:
                raise _invalid() from None
            size += len(piece)
            if size > _MAX_GIT_INVENTORY_RECORD_BYTES:
                raise _invalid("git_inventory_record_limit")
            yield piece
    checkpoint()


def _digest(payload: JsonValue, checkpoint: Callable[[], None]) -> str:
    """对全部规范字节增量求 SHA256，摘要不具有 MAC 或签发语义。"""
    digest = hashlib.sha256()
    for part in _chunks(payload, checkpoint):
        digest.update(part)
    checkpoint()
    return digest.hexdigest()


def _encode(payload: JsonValue, checkpoint: Callable[[], None]) -> bytes:
    """收集完整有界规范字节，取消或超限时不返回部分结果。"""
    body = bytearray()
    for part in _chunks(payload, checkpoint):
        body.extend(part)
    checkpoint()
    return bytes(body)


def _scope_payload(
    value: GitObjectInventory, checkpoint: Callable[[], None]
) -> dict[str, JsonValue]:
    """仅排除身份及阶段，保留对象范围、引用、根、限制和计数的完整声明。"""
    result: dict[str, JsonValue] = {
        name: _wire(getattr(value, name), checkpoint)
        for name in (
            "platform",
            "roots",
            "objects",
            "external_history",
            "limits",
            "max_parents",
            "metrics",
        )
    }
    result["spec_version"] = "harnessix.git-inventory-scope/v1"
    return result


def _inventory_digest(value: GitObjectInventory, checkpoint: Callable[[], None]) -> str:
    """仅排除自身摘要字段，其余身份和阶段字段全部进入目录摘要。"""
    payload = cast(dict[str, JsonValue], _wire(value, checkpoint))
    del payload["inventory_sha256"]
    return _digest(payload, checkpoint)


def _check_inventory_digests(value: GitObjectInventory, checkpoint: Callable[[], None]) -> None:
    """比较两种声明摘要及同目录前阶段摘要，不验证认证发布前缀。"""
    scope = _digest(_scope_payload(value, checkpoint), checkpoint)
    if scope != value.binding.object_scope_digest or _inventory_digest(value, checkpoint) != (
        value.inventory_sha256
    ):
        raise _invalid("git_inventory_graph_mismatch")
    if value.domain_sequence == 1:
        ready = replace(
            value,
            phase="materials_ready",
            domain_sequence=0,
            previous_inventory_sha256="0" * 64,
            inventory_sha256="0" * 64,
        )
        if value.previous_inventory_sha256 != _inventory_digest(ready, checkpoint):
            raise _invalid("git_inventory_stage_mismatch")
    checkpoint()


def git_inventory_scope_digest(value: object, *, checkpoint: Callable[[], None]) -> str:
    """从完整严格声明计算 scope；不信任自身SHA，也不声称已验实际对象图。"""
    snapshot = _snapshot_inventory_shape(value, checkpoint)
    return _digest(_scope_payload(snapshot, checkpoint), checkpoint)


def git_object_inventory_digest(value: object, *, checkpoint: Callable[[], None]) -> str:
    """计算除自身摘要外的完整规范字段；不签发或读取业务记录。"""
    return _inventory_digest(_snapshot_inventory_shape(value, checkpoint), checkpoint)


def encode_git_object_inventory(value: object, *, checkpoint: Callable[[], None]) -> bytes:
    """拒绝错误声明摘要，输出含自身摘要的完整body，而非64字节摘要替代品。"""
    return _encode(
        _wire(snapshot_git_object_inventory(value, checkpoint=checkpoint), checkpoint), checkpoint
    )


def _json(body: object, checkpoint: Callable[[], None]) -> dict[str, JsonValue]:
    """读取有界 UTF8 JSON，拒绝重复键并区分解析器错误与原回调异常。"""
    checkpoint()
    if type(body) is not bytes:
        raise _invalid()
    if len(body) > _MAX_GIT_INVENTORY_RECORD_BYTES:
        raise _invalid("git_inventory_record_limit")
    try:
        text = body.decode("utf-8", "strict")
    except UnicodeError:
        raise _invalid() from None
    callback_error: BaseException | None = None

    def check() -> None:
        """记录原回调异常身份，避免解析器错误映射覆盖调用方异常。"""
        nonlocal callback_error
        try:
            checkpoint()
        except BaseException as error:
            callback_error = error
            raise

    def pairs(items: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        """逐键检查取消与重复键，不采用后值覆盖。"""
        result: dict[str, JsonValue] = {}
        for key, value in items:
            check()
            if key in result:
                raise _invalid()
            result[key] = value
        return result

    def constant(_value: str) -> NoReturn:
        """拒绝 JSON 非有限常量，不改变进程全局数字限制。"""
        raise _invalid()

    check()
    try:
        result: object = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, OverflowError, RecursionError):
        if callback_error is not None:
            raise callback_error from None
        raise _invalid() from None
    check()
    if type(result) is not dict:
        raise _invalid()
    return cast(dict[str, JsonValue], result)


def _uuid_from_wire(value: object) -> UUID:
    """读取唯一规范 UUID 字符串，不接受实际类型或表示转换。"""
    if type(value) is not str:
        raise _invalid()
    try:
        result = UUID(value)
    except ValueError:
        raise _invalid() from None
    if str(result) != value:
        raise _invalid()
    return result


def _bytes_from_wire(value: object) -> bytes:
    """读取偶数长度的小写十六进制字节，不接受其他同义编码。"""
    if type(value) is not str or len(value) % 2 or any(c not in "0123456789abcdef" for c in value):
        raise _invalid()
    return bytes.fromhex(value)


def _from_wire(value: object, annotation: object, checkpoint: Callable[[], None]) -> object:
    """分派有限完整字段读取，保留逐字段检查点与原严格表达。"""
    checkpoint()
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is UnionType:
        return (
            None
            if value is None and type(None) in args
            else _from_wire(value, next(a for a in args if a is not type(None)), checkpoint)
        )
    if origin is tuple:
        if type(value) is not list:
            raise _invalid()
        return tuple(_from_wire(item, args[0], checkpoint) for item in cast(list[object], value))
    if annotation is UUID:
        return _uuid_from_wire(value)
    if annotation is bytes:
        return _bytes_from_wire(value)
    if origin is Literal or annotation in {str, int, type(None)}:
        # 实际标量类型在唯一深层snapshot再验，不作隐式转换。
        return value
    if annotation not in _TYPES:
        raise _invalid()
    return _model_from_wire(value, annotation, checkpoint)


def _model_from_wire[T: _KnownModel](
    value: object, kind: type[T], checkpoint: Callable[[], None]
) -> T:
    """要求有限模型的键集合完全相同，再逐字段交给原构造器验证。"""
    if type(value) is not dict:
        raise _invalid()
    annotations = _annotations(kind)
    keys = {"name_hex" if kind is GitTreeEntry and name == "name" else name for name in annotations}
    if set(value) != keys:
        raise _invalid()
    data = cast(dict[str, object], value)
    members: dict[str, object] = {}
    for name, annotation in annotations.items():
        checkpoint()
        key = "name_hex" if kind is GitTreeEntry and name == "name" else name
        members[name] = _from_wire(data[key], annotation, checkpoint)
    checkpoint()
    try:
        return cast(Callable[..., T], kind)(**members)
    except KernelError as error:
        if kind is GitTreeClosureLimits:
            raise _invalid("git_inventory_limit_invalid") from None
        if error.code.startswith("git_inventory_"):
            raise
        raise _invalid() from None


def decode_git_object_inventory(
    body: object, *, checkpoint: Callable[[], None]
) -> GitObjectInventory:
    """严格完整解析，拒绝重复键、非规范编码及自动补字段；不补正文或认证。"""
    value = _model_from_wire(_json(body, checkpoint), GitObjectInventory, checkpoint)
    snapshot = snapshot_git_object_inventory(value, checkpoint=checkpoint)
    if _encode(_wire(snapshot, checkpoint), checkpoint) != body:
        raise _invalid()
    checkpoint()
    return snapshot


def encode_git_inventory_prefix_projection(
    value: object, *, checkpoint: Callable[[], None]
) -> bytes:
    """编码普通尾锚子投影的完整字段，不签发或更新实际尾锚。"""
    snapshot = snapshot_git_inventory_prefix_projection(value, checkpoint=checkpoint)
    return _encode(_wire(snapshot, checkpoint), checkpoint)


def decode_git_inventory_prefix_projection(
    body: object, *, checkpoint: Callable[[], None]
) -> GitInventoryPrefixProjection:
    """严格读取规范子投影，普通摘要不能充当完整认证前缀。"""
    value = _model_from_wire(_json(body, checkpoint), GitInventoryPrefixProjection, checkpoint)
    snapshot = snapshot_git_inventory_prefix_projection(value, checkpoint=checkpoint)
    if _encode(_wire(snapshot, checkpoint), checkpoint) != body:
        raise _invalid()
    checkpoint()
    return snapshot
