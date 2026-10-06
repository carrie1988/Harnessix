"""复用原完整 inventory 夹具；scope 投影不造绑定、阶段或批准身份。"""

from __future__ import annotations

from dataclasses import fields

from harnessix.delivery.git_inventory_contracts import GitInventoryScope
from harnessix.delivery.git_inventory_materials import verify_git_inventory_scope_materials
from tests.delivery.test_git_inventory_contracts import _check, _inventory
from tests.delivery.test_git_inventory_materials import _case, _state

SCOPE_FIELDS = (
    "action_kind",
    "platform",
    "roots",
    "objects",
    "external_history",
    "limits",
    "max_parents",
    "metrics",
)


def from_inventory(inventory):
    """只取原模型已有的完整图字段，不补业务绑定或零摘要。"""
    return GitInventoryScope(**{name: getattr(inventory, name) for name in SCOPE_FIELDS})


def metadata_scope(object_format="sha256", action="checkpoint", platform="posix"):
    """取得原严格元数据夹具的无绑定完整投影。"""
    return from_inventory(_inventory(object_format, action, platform=platform))


def material_scope(object_format="sha256", action="checkpoint", platform="posix", **kwargs):
    """复用原真实完整对象图，保留不同两根、共享子树和有序重复父边。"""
    inventory, bodies = _case(object_format, action, platform=platform, **kwargs)
    return from_inventory(inventory), bodies


def models(scope):
    """列出全部嵌套模型形状，分别验证 slots、缺字段和恶意子类。"""
    base = next(node for node in scope.objects if "base_commit" in node.roles)
    tree = next(node for node in scope.objects if node.tree_entries)
    return (
        scope,
        scope.roots,
        base,
        base.material,
        base.commit_references,
        base.commit_references.tree,
        tree.tree_entries[0],
        scope.external_history,
        scope.limits,
        scope.metrics,
    )


def inject_model(scope, index, model):
    """在一个固定位置注入反例，不扩大有限模型边界。"""
    if index == 0:
        return model
    parent = {
        1: (scope, "roots"),
        2: (scope, "objects"),
        3: (models(scope)[2], "material"),
        4: (models(scope)[2], "commit_references"),
        5: (models(scope)[4], "tree"),
        6: (next(node for node in scope.objects if node.tree_entries), "tree_entries"),
        7: (scope, "external_history"),
        8: (scope, "limits"),
        9: (scope, "metrics"),
    }[index]
    owner, name = parent
    original = getattr(owner, name)
    if type(original) is tuple:
        replaced = models(scope)[index]
        model = tuple(model if item is replaced else item for item in original)
    object.__setattr__(owner, name, model)
    return scope


def verify_unchanged(cas, scope, checkpoint=_check):
    """成功或失败均比较真实 CAS/SQLite 文件的字节、身份、模式及修改时间。"""
    before = _state(cas)
    try:
        return verify_git_inventory_scope_materials(cas, scope, checkpoint=checkpoint)
    finally:
        assert _state(cas) == before


def model_fields(scope):
    """产出所有固定嵌套字段，不含假身份或阶段的测试组合。"""
    return [
        (index, member.name)
        for index, model in enumerate(models(scope))
        for member in fields(model)
    ]
