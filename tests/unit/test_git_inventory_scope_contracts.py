"""无绑定 scope 的固定字段、深层反例、完整图和原 inventory 字节回归。"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import FrozenInstanceError, fields, replace

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery import git_inventory_contracts as contracts
from harnessix.delivery.git_inventory_contracts import (
    GitInventoryScope,
    snapshot_git_inventory_scope,
    snapshot_git_object_inventory,
)
from harnessix.delivery.git_inventory_wire import (
    decode_git_object_inventory,
    encode_git_object_inventory,
    git_inventory_scope_digest,
)
from tests.delivery.test_git_inventory_contracts import _check, _inventory
from tests.support.git_inventory_scope_cases import (
    SCOPE_FIELDS,
    from_inventory,
    inject_model,
    material_scope,
    metadata_scope,
    model_fields,
    models,
)

# 修改前原夹具的完整规范字节长度和 SHA256；并非 scope 的新摘要合同。
_LEGACY_WIRE = {
    ("sha1", "checkpoint", "materials_ready", "posix"): (
        5272,
        "2968563f85803698c1b2418998c66a11053efe2ceb30e6b65ef76e31bbc9a088",
    ),
    ("sha1", "checkpoint", "materials_ready", "windows"): (
        5274,
        "7424f20534d08dc87536f577d10c73198b1a789e53eead5acf44e69a8c51439c",
    ),
    ("sha1", "checkpoint", "effect_closed", "posix"): (
        5270,
        "7d0bfd6491655e935996d93e1984087cf38fe25baaf7a49900465418a287ae51",
    ),
    ("sha1", "checkpoint", "effect_closed", "windows"): (
        5272,
        "e54321aec02aa29e1c02fed7c88c87167a2c0ca5bc9ebf373538ac6774493525",
    ),
    ("sha1", "commit", "materials_ready", "posix"): (
        6006,
        "04cee164d03c22629a330c42af40088f75672937fc99c4381495afba22faa146",
    ),
    ("sha1", "commit", "materials_ready", "windows"): (
        6008,
        "7a375412c853eac48746ba1db3564d31b0230531913598e29cd70e0a93b65025",
    ),
    ("sha1", "commit", "effect_closed", "posix"): (
        6004,
        "21e4fc7c4128b95fe688c4cb9d7ff870854952fb6ef455e5e95d01aaa852b827",
    ),
    ("sha1", "commit", "effect_closed", "windows"): (
        6006,
        "6d9f7ec422a293c862e6992a130cd155219f180db0f4a61508f3a776eee79a8c",
    ),
    ("sha256", "checkpoint", "materials_ready", "posix"): (
        5788,
        "27dd0a40d002a5a8c4c8bd9bf68743bbb48cf37fdff2b9ce5e30ac715d356241",
    ),
    ("sha256", "checkpoint", "materials_ready", "windows"): (
        5790,
        "820c932e201378a741023908d6c58c36926bc47ddb08f375d7370d5dd0aee0b5",
    ),
    ("sha256", "checkpoint", "effect_closed", "posix"): (
        5786,
        "806f77f923d6bf698c1f07651cf1ea88b6c0d63ee65cbdf7fb6dec258bdb6536",
    ),
    ("sha256", "checkpoint", "effect_closed", "windows"): (
        5788,
        "a2578bc384ae091b16915ae0619f8426b3dc96bfe23e871ae52a03ff5b1b6eac",
    ),
    ("sha256", "commit", "materials_ready", "posix"): (
        6626,
        "953b9b22d7ac7638ed0f1af1efaa34ccf99a13d26badf3f40147bfea83f56d44",
    ),
    ("sha256", "commit", "materials_ready", "windows"): (
        6628,
        "829193902039ac036f7dee1245b25b4b5a1e03514822030a226d7e0cf34fb3d2",
    ),
    ("sha256", "commit", "effect_closed", "posix"): (
        6624,
        "04fb209edfe0fdece2adae9ad176eebecb4ea5b146f2e537e7ad2890b6559300",
    ),
    ("sha256", "commit", "effect_closed", "windows"): (
        6626,
        "7c62fdec889419d10f6211522899c5ab0d3ec0eabcbe1f6c25fd1feb7763f5a4",
    ),
}


def _reject(scope, code="git_inventory_invalid"):
    """要求深层或完整图拒绝使用原固定领域错误，不返回部分结果。"""
    with pytest.raises(KernelError) as error:
        snapshot_git_inventory_scope(scope, checkpoint=_check)
    assert error.value.code == code


@pytest.mark.parametrize("object_format", ("sha1", "sha256"))
@pytest.mark.parametrize("action", ("checkpoint", "commit"))
@pytest.mark.parametrize("platform", ("posix", "windows"))
def test_scope_complete_eight_fields_and_deep_frozen_snapshot(object_format, action, platform):
    """两格式、两动作、两逻辑平台都保留完整图和重复父边，且深层重建。"""
    value = metadata_scope(object_format, action, platform)
    result = snapshot_git_inventory_scope(value, checkpoint=_check)
    assert type(result) is GitInventoryScope and result == value and result is not value
    assert tuple(member.name for member in fields(result)) == SCOPE_FIELDS
    assert GitInventoryScope.__slots__ == SCOPE_FIELDS
    for before, after in zip(models(value), models(result), strict=True):
        assert before == after and before is not after
    assert result.external_history.parents[0] == result.external_history.parents[2]
    assert len(result.external_history.parents) == 3
    assert all(
        before is not after
        for before, after in zip(
            value.external_history.parents, result.external_history.parents, strict=True
        )
    )
    assert ", objects=" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.action_kind = "commit"


def test_scope_has_no_identity_stage_authority_or_digest():
    """新 scope 不宣称发布、验真、批准、版本封套或业务身份。"""
    scope = metadata_scope()
    for name in (
        "binding",
        "phase",
        "spec_version",
        "domain_sequence",
        "inventory_id",
        "authorize",
        "mac",
        "route_fingerprint",
        "product_plan_fingerprint",
        "inventory_sha256",
    ):
        assert not hasattr(scope, name)
    assert GitInventoryScope in contracts._TYPES
    assert not hasattr(scope, "__dict__")
    with pytest.raises(AttributeError):
        object.__setattr__(scope, "extra", "untrusted")
    with pytest.raises(TypeError):
        GitInventoryScope(**{name: getattr(scope, name) for name in SCOPE_FIELDS}, extra=True)


@pytest.mark.parametrize("name", SCOPE_FIELDS)
def test_scope_requires_all_fields_without_defaults(name):
    """八个字段均须显式传入，快照不补默认值或空图。"""
    value = metadata_scope()
    payload = {field: getattr(value, field) for field in SCOPE_FIELDS if field != name}
    with pytest.raises(TypeError):
        GitInventoryScope(**payload)
    object.__delattr__(value, name)
    _reject(value)


@pytest.mark.parametrize("index,name", model_fields(metadata_scope()))
def test_missing_nested_fields_are_rejected(index, name):
    """删除任意原嵌套字段均拒绝，原限额字段仍使用专用错误码。"""
    scope = metadata_scope()
    object.__delattr__(models(scope)[index], name)
    _reject(scope, "git_inventory_limit_invalid" if index == 8 else "git_inventory_invalid")


@pytest.mark.parametrize("index", range(10))
def test_nested_slots_reject_extras(index):
    """全部嵌套模型仍为有限 slots，不接受额外字段或旁路字典。"""
    model = models(metadata_scope())[index]
    assert not hasattr(model, "__dict__")
    with pytest.raises(AttributeError):
        object.__setattr__(model, "extra", "untrusted")


@pytest.mark.parametrize("index", range(10))
def test_nested_malicious_subclass_rejected_before_access(index):
    """伪造子类的属性不得被调用；类型拒绝先于属性访问。"""
    value = metadata_scope()
    accessed = []

    def getattribute(_self, name):
        """若拒绝晚于读取，显式暴露非法访问。"""
        accessed.append(name)
        raise AssertionError("malicious attribute")

    subclass = type("Injected", (type(models(value)[index]),), {"__getattribute__": getattribute})
    forged = object.__new__(subclass)
    value = inject_model(value, index, forged)
    _reject(value, "git_inventory_limit_invalid" if index == 8 else "git_inventory_invalid")
    assert not accessed


@pytest.mark.parametrize("name", SCOPE_FIELDS)
@pytest.mark.parametrize("bad", (None, True, 1.0, {}, [], "untrusted"))
def test_scope_actual_outer_type_not_coerced(name, bad):
    """八字段保持原实际类型，字典和列表不能代替固定模型与元组。"""
    scope = metadata_scope()
    object.__setattr__(scope, name, bad)
    _reject(
        scope,
        "git_inventory_limit_invalid"
        if name in {"limits", "max_parents"}
        else "git_inventory_invalid",
    )


@pytest.mark.parametrize("name", ("max_objects", "max_body_bytes", "max_entries", "max_depth"))
@pytest.mark.parametrize("bad", (True, -1, 1.0, "1"))
def test_original_explicit_limits_are_deep_strict(name, bad):
    """原限制拒绝布尔、负数、隐式整数转换，不加默认预算。"""
    scope = metadata_scope()
    object.__setattr__(scope.limits, name, bad)
    _reject(scope, "git_inventory_limit_invalid")


@pytest.mark.parametrize("bad", (True, -1, 1.0, "1"))
def test_max_parents_remains_strict_explicit_integer(bad):
    """父边预算同样拒绝布尔、负值及隐式数值转换。"""
    scope = metadata_scope()
    object.__setattr__(scope, "max_parents", bad)
    _reject(scope, "git_inventory_limit_invalid")


@pytest.mark.parametrize("name", ("body_bytes", "object_id", "cas_digest", "version"))
def test_deep_reference_actual_fields_cannot_be_forged(name):
    """原七字段引用在深层仍严格重建，布尔长度和非法正文绑定均拒绝。"""
    scope = metadata_scope()
    reference = scope.objects[0].material
    value = {
        "body_bytes": True,
        "object_id": "0" * len(reference.object_id),
        "cas_digest": "f" * 64,
        "version": "unknown",
    }[name]
    object.__setattr__(reference, name, value)
    _reject(scope, "git_inventory_missing" if name == "object_id" else "git_inventory_invalid")


@pytest.mark.parametrize("name", [member.name for member in fields(contracts.GitInventoryMetrics)])
@pytest.mark.parametrize("bad", (True, -1, 1.0, "1"))
def test_all_metrics_are_deep_strict_actual_integers(name, bad):
    """全部指标必须是实际非负整数，不隐式转换。"""
    scope = metadata_scope()
    object.__setattr__(scope.metrics, name, bad)
    _reject(scope)


@pytest.mark.parametrize("name", [member.name for member in fields(contracts.GitInventoryMetrics)])
def test_complete_graph_derives_every_metric(name):
    """每个计数均与完整两树和对象并集重算一致，不接受填充数字。"""
    scope = metadata_scope()
    scope = replace(
        scope, metrics=replace(scope.metrics, **{name: getattr(scope.metrics, name) + 1})
    )
    _reject(scope, "git_inventory_graph_mismatch")


@pytest.mark.parametrize(
    "kind", ("reversed", "duplicate", "missing", "roles", "parents", "boundary", "delivery")
)
def test_scope_reuses_complete_graph_and_role_boundary_rejections(kind):
    """原全图校验仍拒绝乱序、缺对象、错误角色和外部父边降级。"""
    scope = metadata_scope()
    code = "git_inventory_graph_mismatch"
    if kind == "reversed":
        scope = replace(scope, objects=tuple(reversed(scope.objects)))
        code = "git_inventory_invalid"
    elif kind == "duplicate":
        scope = replace(scope, objects=(*scope.objects, scope.objects[-1]))
        code = "git_inventory_invalid"
    elif kind == "missing":
        scope = replace(
            scope, objects=tuple(n for n in scope.objects if "tree_member" not in n.roles)
        )
        code = "git_inventory_missing"
    elif kind == "roles":
        object.__setattr__(scope.objects[0], "roles", ("delivery_commit",))
    elif kind == "parents":
        object.__setattr__(scope.external_history, "parents", scope.external_history.parents[:2])
    elif kind == "boundary":
        object.__setattr__(scope.external_history, "unique_parent_ids", ())
    else:
        scope = replace(scope, action_kind="commit")
    _reject(scope, code)


@pytest.mark.parametrize(
    "name", ("max_objects", "max_body_bytes", "max_entries", "max_depth", "max_parents")
)
def test_scope_limits_exact_and_one_below(name):
    """共享子树仍逐路径展开；任一显式预算低一单位都拒绝，不提高限额。"""
    scope, _ = material_scope()
    assert snapshot_git_inventory_scope(scope, checkpoint=_check) == scope
    if name == "max_parents":
        scope = replace(scope, max_parents=scope.max_parents - 1)
    else:
        scope = replace(
            scope, limits=replace(scope.limits, **{name: getattr(scope.limits, name) - 1})
        )
    _reject(scope, "git_inventory_limit")


def test_scope_reuses_original_snapshot_and_graph_without_digest_checks(monkeypatch):
    """统计包装调用原算法；新入口不能依赖业务绑定摘要。"""
    value = metadata_scope()
    calls = []
    original = contracts._inventory_shape

    def observed(scope, checkpoint):
        """保持原图算法与原检查点，不替代其验证结果。"""
        assert checkpoint is _check
        calls.append(scope)
        return original(scope, checkpoint)

    monkeypatch.setattr(contracts, "_inventory_shape", observed)
    result = snapshot_git_inventory_scope(value, checkpoint=_check)
    assert calls == [result] and calls[0] is result


@pytest.mark.parametrize("kind", ("turn", "task", "deadline", "callback"))
@pytest.mark.parametrize("position", (1, 2, 50, "last"))
def test_scope_checkpoint_cancellation_returns_original_exception(kind, position):
    """深层重建到最终检查点均传播原异常对象，不捕获或返回部分 scope。"""
    scope = metadata_scope()
    count = [0]

    def count_check():
        """记录成功调用的所有检查点，以定位最终检查点。"""
        count[0] += 1

    snapshot_git_inventory_scope(scope, checkpoint=count_check)
    target = count[0] if position == "last" else position
    count[0] = 0
    error = {
        "turn": TurnCancelled(),
        "task": asyncio.CancelledError(),
        "deadline": KernelError("original_deadline", "原固定期限"),
        "callback": ValueError("fixed"),
    }[kind]

    def check():
        """只在原宿主指定位置抛原对象，不建立独立期限。"""
        count[0] += 1
        if count[0] == target:
            raise error

    with pytest.raises(type(error)) as raised:
        snapshot_git_inventory_scope(scope, checkpoint=check)
    assert raised.value is error and count[0] == target


@pytest.mark.parametrize("shape", _LEGACY_WIRE)
def test_original_inventory_wire_type_and_all_digest_rules_unchanged(shape):
    """原十六组合的完整字节指纹固定，解码类型和原两种摘要仍一致。"""
    original = _inventory(*shape)
    body = encode_git_object_inventory(original, checkpoint=_check)
    assert (len(body), hashlib.sha256(body).hexdigest()) == _LEGACY_WIRE[shape]
    decoded = decode_git_object_inventory(body, checkpoint=_check)
    assert type(decoded) is contracts.GitObjectInventory and decoded == original
    assert (
        git_inventory_scope_digest(decoded, checkpoint=_check)
        == original.binding.object_scope_digest
    )
    assert encode_git_object_inventory(decoded, checkpoint=_check) == body
    assert from_inventory(decoded) == snapshot_git_inventory_scope(
        from_inventory(original), checkpoint=_check
    )


def test_scope_cannot_replace_persistent_inventory_or_wire():
    """scope 不取得原持久模型、阶段摘要或序列化发布权限。"""
    value = metadata_scope()
    for operation in (
        snapshot_git_object_inventory,
        encode_git_object_inventory,
        git_inventory_scope_digest,
    ):
        with pytest.raises(KernelError) as raised:
            operation(value, checkpoint=_check)
        assert raised.value.code == "git_inventory_invalid"
