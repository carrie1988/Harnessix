"""完整原 CAS 与纯计算段的边界负控；不替代 SDK 或真实编码质量验收。"""

from __future__ import annotations

import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery import git_inventory_materials as materials
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from tests.delivery.test_git_inventory_materials import _case, _seed, _state


@pytest.fixture(params=["sha1", "sha256"])
def case(tmp_path, request):
    declared, bodies = _case(request.param, action="commit")
    with SQLiteWorkspaceTransactionStore(tmp_path / "cas") as store:
        cas = GitMaterialCAS(store)
        _seed(cas, bodies)
        yield cas, declared


def _node(declared, kind):
    return next(node for node in declared.objects if node.material.object_type == kind)


@pytest.mark.parametrize("kind", ["blob", "tree", "commit"])
def test_real_cas_read_stays_outside_pure_parse(case, monkeypatch, kind):
    cas, declared = case
    trace, saved = [], []
    control = GitAuthenticationControl(lambda: trace.append("local"), lambda: trace.append("full"))
    node = _node(declared, kind)
    original_read = GitMaterialCAS.read
    original_parse = getattr(materials, "parse_git_" + ("commit" if kind == "commit" else "tree"))

    def read(actual, reference):
        assert control._segment is None
        trace.append("CAS")
        return original_read(actual, reference)

    def parse(material, **kwargs):
        assert control._segment is not None
        saved.append(kwargs["checkpoint"])
        return original_parse(material, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(GitMaterialCAS, "read", read)
        if kind != "blob":
            patch.setattr(materials, "parse_git_" + kind, parse)
        result = materials._read_object(cas, node, declared, checkpoint=control)
    assert result == node and result is not node
    assert trace[:3] == ["full", "CAS", "full"] and trace[-1] == "full"
    assert set(trace[3:-1]) == {"local"} and control._segment is None
    trace.clear()
    for check in saved:
        check()
    assert trace == ["full"] * len(saved)


@pytest.mark.parametrize("kind", ["blob", "tree", "commit"])
@pytest.mark.parametrize("callback_kind", ["function", "proxy", "subclass"])
def test_unknown_read_object_keeps_original_checkpoint_trace(case, kind, callback_kind):
    cas, declared = case
    node, trace = _node(declared, kind), []

    def full():
        trace.append("full")

    class Proxy:
        def __call__(self):
            full()

    class Derived(GitAuthenticationControl):
        pass

    check = {"function": full, "proxy": Proxy(), "subclass": Derived(lambda: None, full)}[
        callback_kind
    ]
    result = materials._read_object(cas, node, declared, checkpoint=check)
    assert result == node
    parsing = (
        0
        if kind == "blob"
        else len(node.tree_entries) + 2
        if kind == "tree"
        else 2 * len(node.commit_references.parents) + 5
    )
    assert trace == ["full"] * (4 + parsing)


@pytest.mark.parametrize("phase", ["entry", "local", "exit"])
@pytest.mark.parametrize(
    "failure",
    [
        KernelError("git_inventory_materials_mismatch", "原同码控制错误"),
        OSError("原控制 IO 错误"),
        TurnCancelled(),
        asyncio.CancelledError(),
        UpstreamCheckpointError(UpstreamCheckpointError(ValueError("原嵌套标记"))),
    ],
)
def test_first_control_failure_keeps_identity_and_no_late_exit(case, phase, failure):
    cas, declared = case
    trace = []

    def full():
        trace.append("full")
        if (phase == "entry" and len(trace) == 2) or phase == "exit" and "local" in trace:
            raise failure

    def local():
        trace.append("local")
        if phase == "local":
            raise failure

    control = GitAuthenticationControl(local, full)
    with pytest.raises(BaseException) as caught:
        materials._read_object(cas, _node(declared, "tree"), declared, checkpoint=control)
    assert caught.value is failure and control._segment is None
    if phase == "local":
        assert trace == ["full", "full", "local"]
    else:
        assert trace[-1] == "full"


def test_complete_inventory_preserves_all_reads_facts_and_store_state(case, monkeypatch):
    cas, declared = case
    before, original, reads = _state(cas), GitMaterialCAS.read, []

    def read(actual, reference):
        assert control._segment is None
        reads.append(reference.object_id)
        return original(actual, reference)

    control = GitAuthenticationControl(lambda: None, lambda: None)
    with monkeypatch.context() as patch:
        patch.setattr(GitMaterialCAS, "read", read)
        result = materials.verify_git_inventory_materials(cas, declared, checkpoint=control)
        typed_reads = reads.copy()
        reads.clear()
        expected = materials.verify_git_inventory_materials(cas, declared, checkpoint=lambda: None)
    assert result == expected == declared and typed_reads == reads
    assert set(reads) == {node.material.object_id for node in declared.objects}
    assert max(Counter(reads).values()) >= 3 and _state(cas) == before


def test_pure_reference_totals_and_union_have_no_cas_read(case, monkeypatch):
    cas, declared = case
    catalog = {node.material.object_id: node for node in declared.objects}
    trace = []
    control = GitAuthenticationControl(lambda: trace.append("local"), lambda: trace.append("full"))
    base = materials._actual_closure(
        cas,
        declared,
        declared.roots.base_tree,
        catalog,
        declared.metrics.base_expanded_entries,
        declared.metrics.base_tree_depth,
        checkpoint=control,
    )
    target = materials._actual_closure(
        cas,
        declared,
        declared.roots.target_tree,
        catalog,
        declared.metrics.target_expanded_entries,
        declared.metrics.target_tree_depth,
        checkpoint=control,
    )
    trace.clear()

    def forbidden(*_args):
        pytest.fail("纯并集不得读取 CAS")

    monkeypatch.setattr(GitMaterialCAS, "read", forbidden)
    materials._complete_union(catalog, declared.roots, base, target, checkpoint=control)
    assert trace[0] == trace[-1] == "full" and set(trace[1:-1]) == {"local"}
    with pytest.raises(KernelError) as caught:
        materials._complete_union(
            catalog, declared.roots, base, replace(target, objects=()), checkpoint=control
        )
    assert caught.value.code == "git_inventory_materials_mismatch"


@pytest.mark.parametrize("failure_at", range(1, 7))
async def test_foreign_task_keeps_original_full_count_and_failure_position(case, failure_at):
    cas, declared = case
    trace, failure = [], ValueError("原计数敏感控制")

    def full():
        trace.append("full")
        if len(trace) == failure_at:
            raise failure

    def forbidden():
        pytest.fail("子 Task 不得借用父 local")

    control = GitAuthenticationControl(forbidden, full)

    async def child():
        if failure_at <= 4:
            with pytest.raises(ValueError) as caught:
                materials._read_object(cas, _node(declared, "blob"), declared, checkpoint=control)
            assert caught.value is failure
        else:
            assert materials._read_object(
                cas, _node(declared, "blob"), declared, checkpoint=control
            ) == _node(declared, "blob")

    await asyncio.create_task(child())
    assert trace == ["full"] * min(failure_at, 4) and control._segment is None


def test_foreign_thread_keeps_pure_union_original_trace(case):
    cas, declared = case
    catalog = {node.material.object_id: node for node in declared.objects}
    base = materials._actual_closure(
        cas,
        declared,
        declared.roots.base_tree,
        catalog,
        declared.metrics.base_expanded_entries,
        declared.metrics.base_tree_depth,
        checkpoint=lambda: None,
    )
    target = materials._actual_closure(
        cas,
        declared,
        declared.roots.target_tree,
        catalog,
        declared.metrics.target_expanded_entries,
        declared.metrics.target_tree_depth,
        checkpoint=lambda: None,
    )
    trace = []

    def forbidden():
        pytest.fail("另一线程不得借用原 local")

    control = GitAuthenticationControl(forbidden, lambda: trace.append("full"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(
            materials._complete_union, catalog, declared.roots, base, target, checkpoint=control
        ).result()
    assert trace == ["full"] * (len(base.objects) + len(target.objects) + 1)
    assert control._segment is None
