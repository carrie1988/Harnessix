"""真实原CAS完整8MiB tree的目录验真；不代表产品默认目录容量或原生平台验收。"""

from dataclasses import replace

import pytest

from harnessix.delivery.contracts import MAX_TRANSACTION_FILE_BYTES
from harnessix.delivery.git_inventory_materials import verify_git_inventory_materials
from harnessix.delivery.git_object_material import GitObjectMaterial
from harnessix.delivery.git_object_references import parse_git_tree
from tests.delivery.test_git_inventory_materials import (
    _case,
    _check,
    _read,
    _reference,
    _seal_shape,
    _seed,
    _state,
)
from tests.delivery.test_git_inventory_materials import (
    actual_cas as actual_cas,
)


@pytest.mark.parametrize("object_format", ("sha1", "sha256"))
@pytest.mark.parametrize("platform", ("posix", "windows"))
def test_actual_complete_8mib_tree_inventory(actual_cas, object_format, platform):
    """真实完整正文、全部叶路径与两平台路径合同均过端口，不以摘要占位。"""
    value, bodies = _case(object_format, platform=platform)
    old = value.roots.target_tree.object_id
    target_leaf = next(m for m in bodies if m.object_type == "blob" and m.body == b"target-only")
    child = bytes.fromhex(target_leaf.object_id)
    name_length = 272 - 8 - len(child)
    count, remainder = divmod(MAX_TRANSACTION_FILE_BYTES, 272)
    rows = [
        b"100644 " + f"n{n:08x}".encode() + b"x" * (name_length - 9) + b"\0" + child
        for n in range(count)
    ]
    rows.append(b"100644 " + b"z" * (remainder - 8 - len(child)) + b"\0" + child)
    material = GitObjectMaterial.from_body("tree", object_format, b"".join(rows))
    assert material.body_bytes == MAX_TRANSACTION_FILE_BYTES
    entries = parse_git_tree(material, max_entries=len(rows), checkpoint=_check)
    assert len(entries) == len(rows)
    nodes = tuple(
        replace(n, material=_reference(material), tree_entries=entries)
        if n.material.object_id == old
        else n
        for n in value.objects
    )
    bodies = tuple(material if m.object_id == old else m for m in bodies)
    total = sum(m.body_bytes for m in bodies)
    value = _seal_shape(
        replace(
            value,
            roots=replace(value.roots, target_tree=_read(material)),
            objects=tuple(sorted(nodes, key=lambda n: n.material.object_id)),
            limits=replace(value.limits, max_body_bytes=total, max_entries=len(rows)),
            metrics=replace(
                value.metrics,
                unique_body_bytes=total,
                direct_tree_edges=len(rows) + 2,
                target_expanded_entries=len(rows),
                target_tree_depth=0,
            ),
        )
    )
    _seed(actual_cas, bodies)
    before = _state(actual_cas)
    result = verify_git_inventory_materials(actual_cas, value, checkpoint=_check)
    assert result == value and result is not value
    assert _state(actual_cas) == before
