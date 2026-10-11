"""Git 业务快照的唯一规范 JSON；嵌套记录复用原正式 wire codec。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import NoReturn
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.product_config.git_business_snapshot_contracts import (
    GitBusinessLinkSnapshot,
    GitDeliveryBusinessSnapshot,
)
from harnessix.product_config.git_decision_link_wire import (
    decode_product_git_decision_link,
    encode_product_git_decision_link,
)
from harnessix.product_config.git_prefix_catalog import (
    decode_git_prefix_catalog,
    encode_git_prefix_catalog,
)
from harnessix.product_config.git_prepared_link_wire import (
    decode_product_git_prepared_link,
    encode_product_git_prepared_link,
)
from harnessix.product_config.state_backup_contracts import MAX_STATE_FILE_BYTES


def invalid_git_business_snapshot() -> KernelError:
    return KernelError("git_business_snapshot_invalid", "Git业务快照无法核验")


def _canonical_json(value: object, checkpoint: Callable[[], None]) -> bytes:
    encoder = json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    result = bytearray()
    try:
        for part in encoder.iterencode(value):
            checkpoint()
            result.extend(part.encode("utf-8", "strict"))
            if len(result) > MAX_STATE_FILE_BYTES:
                raise invalid_git_business_snapshot()
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise invalid_git_business_snapshot() from None
    checkpoint()
    return bytes(result)


def _json_value(body: bytes) -> object:
    return json.loads(body.decode("utf-8", "strict"))


def snapshot_git_delivery_business_snapshot(
    value: object, *, checkpoint: Callable[[], None]
) -> GitDeliveryBusinessSnapshot:
    """深层重建全部嵌套声明，避免已建模型绕过严格 wire 校验。"""
    checkpoint()
    if type(value) is not GitDeliveryBusinessSnapshot:
        raise invalid_git_business_snapshot()
    try:
        prefix = decode_git_prefix_catalog(encode_git_prefix_catalog(value.prefix))
        links = []
        for link in value.links:
            checkpoint()
            if type(link) is not GitBusinessLinkSnapshot:
                raise invalid_git_business_snapshot()
            prepared = decode_product_git_prepared_link(
                encode_product_git_prepared_link(link.prepared, checkpoint=checkpoint),
                checkpoint=checkpoint,
            )
            decision = (
                None
                if link.decision is None
                else decode_product_git_decision_link(
                    encode_product_git_decision_link(link.decision, checkpoint=checkpoint),
                    checkpoint=checkpoint,
                )
            )
            links.append(GitBusinessLinkSnapshot(prepared=prepared, decision=decision))
        result = GitDeliveryBusinessSnapshot.model_validate(
            {
                "spec_version": value.spec_version,
                "capture_id": value.capture_id,
                "prefix": prefix,
                "links": tuple(links),
            },
            strict=True,
            context={"checkpoint": checkpoint},
        )
        checkpoint()
        return result
    except KernelError:
        raise
    except (ValidationError, ValueError, TypeError, AttributeError, RecursionError):
        raise invalid_git_business_snapshot() from None


def encode_git_delivery_business_snapshot(
    value: object, *, checkpoint: Callable[[], None]
) -> bytes:
    """输出唯一完整字节；不签名、不刷新历史，也不读取外部 Git。"""
    snapshot = snapshot_git_delivery_business_snapshot(value, checkpoint=checkpoint)
    payload = {
        "capture_id": str(snapshot.capture_id),
        "links": [
            {
                "decision": (
                    None
                    if link.decision is None
                    else _json_value(
                        encode_product_git_decision_link(link.decision, checkpoint=checkpoint)
                    )
                ),
                "prepared": _json_value(
                    encode_product_git_prepared_link(link.prepared, checkpoint=checkpoint)
                ),
            }
            for link in snapshot.links
        ],
        "prefix": _json_value(encode_git_prefix_catalog(snapshot.prefix)),
        "spec_version": snapshot.spec_version,
    }
    return _canonical_json(payload, checkpoint)


def decode_git_delivery_business_snapshot(
    body: object, *, checkpoint: Callable[[], None]
) -> GitDeliveryBusinessSnapshot:
    """拒绝重复键、非规范字节、未知字段及任何嵌套记录降级。"""
    checkpoint()
    if type(body) is not bytes or not 1 <= len(body) <= MAX_STATE_FILE_BYTES:
        raise invalid_git_business_snapshot()
    callback_error: BaseException | None = None

    def check() -> None:
        nonlocal callback_error
        try:
            checkpoint()
        except BaseException as error:
            callback_error = error
            raise

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            check()
            if key in result:
                raise invalid_git_business_snapshot()
            result[key] = value
        return result

    def constant(_value: str) -> NoReturn:
        raise invalid_git_business_snapshot()

    try:
        payload = json.loads(
            body.decode("utf-8", "strict"), object_pairs_hook=pairs, parse_constant=constant
        )
        if type(payload) is not dict or set(payload) != {
            "capture_id",
            "links",
            "prefix",
            "spec_version",
        }:
            raise invalid_git_business_snapshot()
        raw_links = payload["links"]
        raw_capture_id = payload["capture_id"]
        if type(raw_links) is not list or type(raw_capture_id) is not str:
            raise invalid_git_business_snapshot()
        capture_id = UUID(raw_capture_id)
        if str(capture_id) != raw_capture_id:
            raise invalid_git_business_snapshot()
        links = []
        for raw in raw_links:
            check()
            if type(raw) is not dict or set(raw) != {"prepared", "decision"}:
                raise invalid_git_business_snapshot()
            prepared = decode_product_git_prepared_link(
                _canonical_json(raw["prepared"], check), checkpoint=check
            )
            decision = (
                None
                if raw["decision"] is None
                else decode_product_git_decision_link(
                    _canonical_json(raw["decision"], check), checkpoint=check
                )
            )
            links.append(GitBusinessLinkSnapshot(prepared=prepared, decision=decision))
        candidate = GitDeliveryBusinessSnapshot.model_validate(
            {
                "spec_version": payload["spec_version"],
                "capture_id": capture_id,
                "prefix": decode_git_prefix_catalog(_canonical_json(payload["prefix"], check)),
                "links": tuple(links),
            },
            strict=True,
            context={"checkpoint": check},
        )
        result = snapshot_git_delivery_business_snapshot(candidate, checkpoint=check)
        if encode_git_delivery_business_snapshot(result, checkpoint=check) != body:
            raise invalid_git_business_snapshot()
        check()
        return result
    except (KernelError, ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        if callback_error is not None:
            raise callback_error from None
        raise invalid_git_business_snapshot() from None
