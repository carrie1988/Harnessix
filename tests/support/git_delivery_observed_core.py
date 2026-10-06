"""Core2 测试的纯声明与真实 CAS 材料；不认证原 Session，不签发批准。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass, replace
from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel

from harnessix.product_config.git_delivery_observed_contracts import (
    ProductGitDeliveryCoreV2,
    ProductGitDeliveryPlanV2,
)
from harnessix.product_config.git_user_observation_contracts import ProductGitUserObservation
from harnessix.trusted_actions.planning import external_action_identity
from tests.product_config.git_delivery_plan_support import make_case, route_for


def json_facts(value):
    """独立 Oracle 只读取字段，不调用受测 Codec 或模型序列化器。"""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, BaseModel):
        return {name: json_facts(getattr(value, name)) for name in type(value).model_fields}
    if is_dataclass(value):
        return {field.name: json_facts(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (tuple, list)):
        return [json_facts(item) for item in value]
    if isinstance(value, dict):
        return {key: json_facts(item) for key, item in value.items()}
    return value


def canonical(payload):
    """测试独立定义规范 UTF-8 字节，禁止复用产品的编码器或摘要函数。"""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def rehash(payload, field="fingerprint"):
    """只封签这一层，故意保留嵌套错误，便于模拟恶意外层重封签。"""
    payload[field] = hashlib.sha256(
        canonical({key: value for key, value in payload.items() if key != field})
    ).hexdigest()
    return payload


def model_fields(value, field="fingerprint"):
    return {name: getattr(value, name) for name in type(value).model_fields if name != field}


def seal(kind, payload, field="fingerprint"):
    """独立封签普通声明，然后使用正式合同核验；没有 Session 或批准含义。"""
    candidate = kind.model_construct(**payload, **{field: "0" * 64})
    return kind.model_validate_json(canonical(rehash(json_facts(candidate), field)), strict=True)


def core_payload(core):
    """逐字段定义 Core2 预期，不从受测序列化输出推导字段或旧别名。"""
    observation = core.user_observation
    return json_facts(
        {
            "spec_version": "harnessix.product-git-delivery-core/v2",
            "delivery_id": core.delivery_id,
            "store_id": core.store_id,
            "key_id": core.key_id,
            "thread_id": core.thread_id,
            "turn_id": core.turn_id,
            "call": core.call,
            "user_observation": {
                "spec_version": "harnessix.product-git-user-observation/v1",
                "store_id": observation.store_id,
                "key_id": observation.key_id,
                "baseline": observation.baseline,
                "common_directory_path_sha256": observation.common_directory_path_sha256,
                "common_directory_identity": observation.common_directory_identity,
                "git_directory_path_sha256": observation.git_directory_path_sha256,
                "git_directory_identity": observation.git_directory_identity,
                "index_file_observation": observation.index_file_observation,
                "config_sha256": observation.config_sha256,
                "implementation_digest": observation.implementation_digest,
                "fingerprint": observation.fingerprint,
            },
            "anchor_intent": core.anchor_intent,
            "worktree_intent": core.worktree_intent,
            "checkpoint_delivery_id": core.checkpoint_delivery_id,
            "object_scope": core.object_scope,
            "diff_sha256": core.diff_sha256,
            "diff_bytes": core.diff_bytes,
            "commit_spec": core.commit_spec,
            "implementation_digest": core.implementation_digest,
        }
    )


def core_bytes(core):
    return canonical(core_payload(core))


def plan_payload(plan):
    return {
        "spec_version": "harnessix.product-git-delivery-plan/v2",
        "core": {**core_payload(plan.core), "fingerprint": plan.core.fingerprint},
        "route": json_facts(plan.route),
        "review_artifact": json_facts(plan.review_artifact),
        "fingerprint": plan.fingerprint,
    }


def at(payload, path):
    """定位测试声明中的字段；只供显式负例修改，不代替产品验证。"""
    for name in path:
        payload = payload[name]
    return payload


def reseal_core_payload(payload):
    """故意封签所有声明层；合法摘要仍不能证明材料、身份归属或批准。"""
    observation = payload["user_observation"]
    rehash(observation["baseline"]["source"], "digest")
    rehash(observation["baseline"], "digest")
    rehash(observation)
    return rehash(payload)


def plan_for(core, artifact):
    return seal(
        ProductGitDeliveryPlanV2,
        {"core": core, "route": route_for(core), "review_artifact": artifact},
    )


def rebind_identity(core):
    """沿原唯一身份算法重绑声明，不伪造原 Router 认证或执行成功。"""
    preliminary = route_for(core)
    delivery_id = external_action_identity(preliminary.invocation, preliminary.binding)
    payload = {**model_fields(core), "delivery_id": delivery_id}
    if core.commit_spec is not None:
        payload["commit_spec"] = seal(
            type(core.commit_spec), {**model_fields(core.commit_spec), "commit_id": delivery_id}
        )
    return seal(ProductGitDeliveryCoreV2, payload)


def make_observed_case(cas, path, **options):
    """复用原全树/全父历史 CAS，不声称 fixture 的身份来自认证原 Session。"""
    provider_call_id = options.pop("provider_call_id", None)
    legacy = make_case(cas, path, **options)
    old = legacy.core
    observation = seal(
        ProductGitUserObservation,
        {
            "store_id": old.store_id,
            "key_id": old.key_id,
            "baseline": old.baseline,
            "common_directory_path_sha256": old.common_directory_path_sha256,
            "common_directory_identity": old.common_directory_identity,
            "git_directory_path_sha256": "a" * 64,
            "git_directory_identity": "b" * 64,
            "index_file_observation": old.index_file_observation,
            "config_sha256": "c" * 64,
            "implementation_digest": old.implementation_digest,
        },
    )
    payload = model_fields(old)
    for name in (
        "baseline",
        "common_directory_path_sha256",
        "common_directory_identity",
        "index_file_observation",
    ):
        del payload[name]
    payload.update(
        spec_version="harnessix.product-git-delivery-core/v2", user_observation=observation
    )
    if provider_call_id is not None:
        payload["call"] = old.call.model_copy(update={"provider_call_id": provider_call_id})
    core = seal(ProductGitDeliveryCoreV2, payload)
    core = rebind_identity(core)
    plan = plan_for(core, legacy.plan.review_artifact)
    return replace(legacy, core=core, plan=plan), legacy
