"""完整原 CAS 材料组装的纯数据测试；不是 Session 认证或受控 Git 采集阳性。"""

from __future__ import annotations

import asyncio
from dataclasses import fields, replace

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import MAX_WORKSPACE_DIFF_BYTES
from harnessix.delivery.diff_content import DiffContentLimitError
from harnessix.delivery.git_inventory_contracts import GitInventoryScope
from harnessix.delivery.git_inventory_materials import verify_git_inventory_scope_materials
from harnessix.delivery.git_material_cas import GitMaterialCAS, GitObjectMaterialReference
from harnessix.delivery.git_object_material import GitObjectMaterial
from harnessix.delivery.git_tree_closure import GitTreeClosureLimits
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config import git_checkpoint_scope as assembly
from harnessix.product_config.git_parent_contracts import ProductGitDeliveryBaselineV2
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from tests.support.git_delivery_observed_core import (
    canonical,
    json_facts,
    make_observed_case,
    rehash,
)


@pytest.fixture
def cas(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        yield GitMaterialCAS(store)


@pytest.fixture
def case(cas, tmp_path):
    return make_observed_case(cas, tmp_path)[0]


def inputs(case):
    """从夹具实际完整图独立遍历基线边，绝不向产品入口传入身份或批准。"""
    scope = case.core.object_scope
    nodes = {node.material.object_id: node for node in scope.objects}
    pending = [scope.roots.base_tree.object_id]
    base_ids = set()
    while pending:
        oid = pending.pop()
        if oid in base_ids:
            continue
        base_ids.add(oid)
        pending.extend(entry.child.object_id for entry in nodes[oid].tree_entries)
    baseline = case.core.user_observation.baseline
    after = {
        (mutation.after.sha256, mutation.after.size)
        for mutation in baseline.source.mutations
        if mutation.after.presence == "file"
    }
    return {
        "baseline": baseline,
        "base_commit": nodes[scope.roots.base_commit.object_id].material,
        "base_catalog": tuple(nodes[oid].material for oid in sorted(base_ids)),
        "after_catalog": tuple(
            node.material
            for node in scope.objects
            if node.material.object_type == "blob"
            and (node.material.body_sha256, node.material.body_bytes) in after
        ),
        "limits": scope.limits,
        "max_parents": scope.max_parents,
    }


def build(case, *, checkpoint=lambda: None, target_cas=None, **updates):
    return assembly.build_product_git_checkpoint_scope(
        case.cas if target_cas is None else target_cas,
        **{**inputs(case), **updates},
        checkpoint=checkpoint,
    )


def reject(case, code, **updates):
    with pytest.raises(KernelError) as caught:
        build(case, **updates)
    assert caught.value.code == code
    assert caught.value.__cause__ is None
    return caught.value


def baseline_with(baseline, **updates):
    """独立重封签纯声明，并经正式严格合同校验；不构造认证基线。"""
    payload = {**json_facts(baseline), **updates}
    return ProductGitDeliveryBaselineV2.model_validate_json(
        canonical(rehash(payload, "digest")), strict=True
    )


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
@pytest.mark.parametrize("platform", ["posix", "windows"])
@pytest.mark.parametrize("include_commit", [False, True])
def test_complete_scope_net_diff_external_history_and_original_tree_writes(
    cas, tmp_path, monkeypatch, fmt, platform, include_commit
):
    case = make_observed_case(cas, tmp_path, fmt=fmt, platform=platform)[0]
    args = inputs(case)
    expected = case.core.object_scope
    source_rows = tuple(cas.store._db.iterdump())
    leaf = case.workspace_root / args["baseline"].members[0].path
    leaf_before = (leaf.read_bytes(), leaf.stat().st_mtime_ns)
    with SQLiteWorkspaceTransactionStore(tmp_path / "assembly-cas") as store:
        destination = GitMaterialCAS(store)
        initial = (args["base_commit"], *args["base_catalog"], *args["after_catalog"])
        for reference in initial:
            destination.persist(cas.read(reference))
        initial_ids = {reference.object_id for reference in initial}
        assert expected.roots.target_tree.object_id not in initial_ids
        rows_before = tuple(store._db.iterdump())
        writes = []
        persist = GitMaterialCAS.persist

        def record_write(port, material):
            assert port is destination
            writes.append(material)
            return persist(port, material)

        monkeypatch.setattr(GitMaterialCAS, "persist", record_write)
        catalog = args["base_catalog"]
        if include_commit:
            catalog = (*catalog, args["base_commit"])
        scope, diff = build(case, target_cas=destination, base_catalog=catalog)
        assert scope == expected
        assert scope is not expected
        assert scope.limits == args["limits"] and scope.limits is not args["limits"]
        assert diff.content.text == case.diff_text
        assert (diff.content.sha256, diff.content.utf8_bytes) == (
            case.core.diff_sha256,
            case.core.diff_bytes,
        )
        assert [entry.path for entry in diff.content.entries] == [
            leaf.relative_to(case.workspace_root).as_posix()
        ]
        assert "keep" not in diff.content.text
        assert any(file.path == "keep" for file in diff.projection.files)
        assert writes == list(diff.projection.new_trees)
        assert {node.material.object_id for node in scope.objects} == initial_ids | {
            material.object_id for material in writes
        }
        assert scope.roots.delivery_commit is None
        assert scope.external_history.parents == expected.external_history.parents
        assert len(scope.external_history.parents) == 3
        assert len(scope.external_history.unique_parent_ids) == 2
        assert not set(scope.external_history.unique_parent_ids) & {
            node.material.object_id for node in scope.objects
        }
        assert scope.metrics.object_count == len(scope.objects)
        assert scope.metrics.unique_body_bytes == sum(
            node.material.body_bytes for node in scope.objects
        )
        assert scope.metrics.direct_tree_edges == sum(
            len(node.tree_entries) for node in scope.objects
        )
        assert scope.metrics.commit_parent_edges == 3
        assert scope.metrics.base_expanded_entries == scope.metrics.target_expanded_entries == 4
        assert scope.metrics.base_tree_depth == scope.metrics.target_tree_depth == 2
        assert {field.name for field in fields(scope)} == {
            "action_kind",
            "platform",
            "roots",
            "objects",
            "external_history",
            "limits",
            "max_parents",
            "metrics",
        }
        for node in scope.objects:
            assert destination.read(node.material) == next(
                material
                for material in case.bodies
                if material.object_id == node.material.object_id
            )
        assert (
            verify_git_inventory_scope_materials(destination, scope, checkpoint=lambda: None)
            == scope
        )
        assert tuple(store._db.iterdump()) == rows_before
    assert tuple(cas.store._db.iterdump()) == source_rows
    assert (leaf.read_bytes(), leaf.stat().st_mtime_ns) == leaf_before


@pytest.mark.parametrize("dimension", ["objects", "body_bytes", "entries", "depth", "parents"])
def test_exact_limits_succeed_and_one_less_never_truncates(case, monkeypatch, dimension):
    scope = case.core.object_scope
    exact = GitTreeClosureLimits(
        scope.metrics.object_count,
        scope.metrics.unique_body_bytes,
        scope.metrics.base_expanded_entries,
        scope.metrics.base_tree_depth,
    )
    observed, diff = build(case, limits=exact, max_parents=3)
    assert observed.limits == exact and observed.max_parents == 3
    assert len(diff.content.entries) == len(inputs(case)["baseline"].source.mutations)
    writes = []
    persist = GitMaterialCAS.persist

    def record_write(port, material):
        writes.append(material)
        return persist(port, material)

    monkeypatch.setattr(GitMaterialCAS, "persist", record_write)
    if dimension == "parents":
        reject(case, "git_object_references_limit", limits=exact, max_parents=2)
    else:
        name = "max_" + dimension
        reduced = replace(exact, **{name: getattr(exact, name) - 1})
        with pytest.raises(KernelError) as caught:
            build(case, limits=reduced)
        assert caught.value.code in {
            "git_inventory_limit",
            "git_tree_closure_limit",
            "git_tree_projection_limit",
            "git_object_references_limit",
        }
    assert not writes


def test_diff_capacity_uses_original_ceiling_and_error(case, monkeypatch):
    prepare = assembly.prepare_git_tree_diff

    def force_small_diff(*args, **kwargs):
        assert kwargs["max_diff_bytes"] == MAX_WORKSPACE_DIFF_BYTES
        return prepare(*args, **{**kwargs, "max_diff_bytes": 1})

    monkeypatch.setattr(assembly, "prepare_git_tree_diff", force_small_diff)
    reject(case, "git_tree_diff_limit")


@pytest.mark.parametrize("which", ["commit", "tree", "unchanged_blob", "after_blob"])
@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_missing_or_corrupt_actual_cas_never_returns_scope(case, which, failure):
    args = inputs(case)
    reference = {
        "commit": args["base_commit"],
        "tree": next(ref for ref in args["base_catalog"] if ref.object_type == "tree"),
        "unchanged_blob": next(
            ref for ref in args["base_catalog"] if case.cas.read(ref).body == b"unchanged\n"
        ),
        "after_blob": args["after_catalog"][0],
    }[which]
    path = case.cas.store._blobs / reference.cas_digest
    if failure == "missing":
        path.unlink()
    else:
        path.write_bytes(b"wrong material\n")
    reject(case, "git_material_cas_read_failed")


def test_wrong_cas_is_not_a_reference_or_authority_shortcut(case, tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "empty-cas") as store:
        with pytest.raises(KernelError) as caught:
            build(case, target_cas=GitMaterialCAS(store))
        assert caught.value.code == "git_material_cas_read_failed"


@pytest.mark.parametrize("which", ["head", "tree", "member"])
def test_baseline_roots_and_original_before_oid_must_match_actual_material(case, which):
    baseline = inputs(case)["baseline"]
    if which == "member":
        members = json_facts(baseline.members)
        members[0]["oid"] = "c" * len(baseline.head_oid)
        updated = baseline_with(baseline, members=members)
    else:
        name = "head_oid" if which == "head" else "head_tree_oid"
        updated = baseline_with(baseline, **{name: "c" * len(baseline.head_oid)})
    reject(case, "git_delivery_plan_invalid", baseline=updated)


def test_absent_base_tree_and_incomplete_tree_catalog_are_rejected(case):
    args = inputs(case)
    root_oid = args["baseline"].head_tree_oid
    reject(
        case,
        "git_delivery_plan_invalid",
        base_catalog=tuple(ref for ref in args["base_catalog"] if ref.object_id != root_oid),
    )
    blob = next(ref for ref in args["base_catalog"] if ref.object_type == "blob")
    reject(
        case,
        "git_tree_closure_missing",
        base_catalog=tuple(ref for ref in args["base_catalog"] if ref != blob),
    )


@pytest.mark.parametrize("kind", ["commit", "blob", "tree"])
def test_extra_objects_in_base_catalog_are_rejected_without_history_reads(case, monkeypatch, kind):
    args = inputs(case)
    material = GitObjectMaterial.from_body(
        kind, args["base_commit"].object_format, b"extra history\n"
    )
    reference = case.cas.persist(material)
    read = GitMaterialCAS.read
    observed = []

    def record_read(port, ref):
        observed.append(ref.object_id)
        return read(port, ref)

    monkeypatch.setattr(GitMaterialCAS, "read", record_read)
    reject(case, "git_delivery_plan_invalid", base_catalog=(*args["base_catalog"], reference))
    assert reference.object_id not in observed
    assert not set(case.core.object_scope.external_history.unique_parent_ids) & set(observed)


@pytest.mark.parametrize("failure", ["missing", "extra", "duplicate", "wrong_type", "wrong_oid"])
def test_after_catalog_must_be_exact_original_net_mutation_content(case, failure):
    args = inputs(case)
    after = args["after_catalog"]
    if failure == "missing":
        after = ()
    elif failure == "extra":
        after = (*after, next(ref for ref in args["base_catalog"] if ref.object_type == "blob"))
    elif failure == "duplicate":
        after = (*after, after[0])
    elif failure == "wrong_type":
        after = (args["base_commit"],)
    else:
        after = (replace(after[0], object_id="e" * len(after[0].object_id)),)
    reject(
        case,
        "git_material_cas_read_failed"
        if failure == "wrong_oid"
        else "git_tree_projection_after_mismatch",
        after_catalog=after,
    )


@pytest.mark.parametrize(
    "field",
    ["baseline", "source", "mutation", "before", "base_commit", "catalog", "limits", "max_parents"],
)
def test_strict_actual_type_deep_snapshot_rejects_tampered_inputs_before_cas(
    case, monkeypatch, field
):
    args = inputs(case)
    baseline = args["baseline"]
    updates = {}
    if field in {"baseline", "source", "mutation", "before", "base_commit", "limits"}:
        value, name, changed = {
            "baseline": (baseline, "index_observation_bytes", True),
            "source": (baseline.source, "mutations", list(baseline.source.mutations)),
            "mutation": (baseline.source.mutations[0], "path", b"src/d1/file.py"),
            "before": (baseline.source.mutations[0].before, "size", True),
            "base_commit": (args["base_commit"], "body_bytes", True),
            "limits": (args["limits"], "max_depth", True),
        }[field]
        object.__setattr__(value, name, changed)
    elif field == "catalog":
        updates["base_catalog"] = list(args["base_catalog"])
    else:
        updates["max_parents"] = True

    def forbidden_read(*_):
        pytest.fail("严格类型拒绝前不应读取 CAS")

    monkeypatch.setattr(GitMaterialCAS, "read", forbidden_read)
    with pytest.raises(KernelError):
        assembly.build_product_git_checkpoint_scope(
            case.cas, **{**args, **updates}, checkpoint=lambda: None
        )


@pytest.mark.parametrize(
    "kind", [ProductGitDeliveryBaselineV2, GitObjectMaterialReference, GitTreeClosureLimits]
)
def test_input_subclasses_are_not_trusted(case, kind):
    args = inputs(case)
    name = {
        ProductGitDeliveryBaselineV2: "baseline",
        GitObjectMaterialReference: "base_commit",
        GitTreeClosureLimits: "limits",
    }[kind]
    value = args[name]
    subclass = type("UntrustedSubclass", (kind,), {})
    if kind is ProductGitDeliveryBaselineV2:
        replacement = subclass.model_validate_json(canonical(json_facts(value)), strict=True)
    else:
        replacement = subclass(
            **{field.name: getattr(value, field.name) for field in fields(value)}
        )
    with pytest.raises(KernelError):
        build(case, **{name: replacement})


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
def test_explicit_parent_capacity_above_fixture_default_is_not_truncated(cas, tmp_path, fmt):
    case = make_observed_case(cas, tmp_path, fmt=fmt)[0]
    args = inputs(case)
    original = cas.read(args["base_commit"])
    header, rest = original.body.split(b"author ", 1)
    tree_line, *parent_lines = header.splitlines(keepends=True)
    lines = [parent_lines[index % len(parent_lines)] for index in range(7)]
    material = GitObjectMaterial.from_body(
        "commit", fmt, tree_line + b"".join(lines) + b"author " + rest
    )
    reference = cas.persist(material)
    baseline = baseline_with(args["baseline"], head_oid=reference.object_id)
    scope, diff = build(case, baseline=baseline, base_commit=reference, max_parents=7)
    expected = tuple(line.removeprefix(b"parent ").strip().decode() for line in lines)
    assert tuple(parent.object_id for parent in scope.external_history.parents) == expected
    assert scope.metrics.commit_parent_edges == scope.max_parents == 7
    assert diff.content.text == case.diff_text
    reject(
        case,
        "git_object_references_limit",
        baseline=baseline,
        base_commit=reference,
        max_parents=6,
    )


def test_shared_subtrees_expand_every_path_and_tree_parser_has_no_fixture_entry_limit(case):
    args = inputs(case)
    old_scope = case.core.object_scope
    nodes = {node.material.object_id: node for node in old_scope.objects}
    new_subtree = next(
        entry.child
        for entry in nodes[old_scope.roots.target_tree.object_id].tree_entries
        if entry.mode == "40000"
    )
    root = case.cas.read(nodes[old_scope.roots.base_tree.object_id].material)
    aliases = b"".join(
        b"40000 a" + str(index).encode() + b"\0" + bytes.fromhex(new_subtree.object_id)
        for index in range(6)
    )
    base_tree = GitObjectMaterial.from_body("tree", root.object_format, aliases + root.body)
    tree_reference = case.cas.persist(base_tree)
    old_commit = case.cas.read(args["base_commit"])
    commit = GitObjectMaterial.from_body(
        "commit",
        old_commit.object_format,
        old_commit.body.replace(root.object_id.encode(), base_tree.object_id.encode(), 1),
    )
    commit_reference = case.cas.persist(commit)
    baseline = baseline_with(
        args["baseline"], head_oid=commit.object_id, head_tree_oid=base_tree.object_id
    )
    excluded = {root.object_id, old_scope.roots.target_tree.object_id, old_commit.object_id}
    catalog = (
        tree_reference,
        *(node.material for node in old_scope.objects if node.material.object_id not in excluded),
    )
    scope, diff = build(
        case,
        baseline=baseline,
        base_commit=commit_reference,
        base_catalog=catalog,
        limits=GitTreeClosureLimits(256, 32 * 1024 * 1024, 22, 2),
    )
    assert scope.metrics.base_expanded_entries == scope.metrics.target_expanded_entries == 22
    assert scope.metrics.direct_tree_edges == 20
    assert len(diff.projection.new_trees) == 1
    assert len(diff.projection.base.files) == len(diff.projection.files) == 8
    assert diff.content.text == case.diff_text
    assert len({node.material.object_id for node in scope.objects}) == scope.metrics.object_count
    target = next(node for node in scope.objects if "target_tree" in node.roles)
    assert len(target.tree_entries) == 8
    assert {entry.child.object_id for entry in target.tree_entries if entry.mode == "40000"} == {
        new_subtree.object_id
    }


def test_deep_snapshot_isolated_from_original_baseline_mutation_during_cas_read(case, monkeypatch):
    args = inputs(case)
    read = GitMaterialCAS.read
    first = True

    def mutate_original(port, reference):
        nonlocal first
        if first:
            first = False
            object.__setattr__(args["baseline"].source.mutations[0].after, "size", True)
        return read(port, reference)

    monkeypatch.setattr(GitMaterialCAS, "read", mutate_original)
    scope, diff = assembly.build_product_git_checkpoint_scope(
        case.cas, **args, checkpoint=lambda: None
    )
    assert scope == case.core.object_scope
    assert diff.content.text == case.diff_text
    assert type(diff.content.entries[0].after.size) is int


@pytest.mark.parametrize(
    "port",
    [
        "prepare_git_tree_diff",
        "verify_git_tree_closure",
        "snapshot_git_inventory_scope",
        "verify_git_inventory_scope_materials",
    ],
)
def test_original_algorithm_exception_is_not_reclassified(case, monkeypatch, port):
    error = KernelError("original_algorithm", "original_algorithm")

    def fail(*_, **__):
        raise error

    monkeypatch.setattr(assembly, port, fail)
    with pytest.raises(KernelError) as caught:
        build(case)
    assert caught.value is error


def test_cas_persistence_failure_returns_no_scope_and_keeps_original_error(case, monkeypatch):
    error = KernelError("git_material_cas_write_failed", "Git对象材料CAS持久化失败")

    def fail(*_):
        raise error

    monkeypatch.setattr(GitMaterialCAS, "persist", fail)
    with pytest.raises(KernelError) as caught:
        build(case)
    assert caught.value is error


@pytest.mark.parametrize(
    "exception",
    [
        ValueError("callback"),
        TypeError("callback"),
        AttributeError("callback"),
        RecursionError("callback"),
        OSError("callback"),
        KernelError("callback", "callback"),
        DiffContentLimitError(),
        ValidationError.from_exception_data("callback", []),
        asyncio.CancelledError("callback"),
        UpstreamCheckpointError(ValueError("inner")),
    ],
)
def test_every_checkpoint_preserves_original_callback_exception_identity(case, exception):
    count = 0

    def count_checkpoints():
        nonlocal count
        count += 1

    build(case, checkpoint=count_checkpoints)
    assert count > 100
    for stop in range(1, count + 1):
        current = 0

        def cancel(stop=stop):
            nonlocal current
            current += 1
            if current == stop:
                raise exception

        with pytest.raises(BaseException) as caught:
            build(case, checkpoint=cancel)
        assert caught.value is exception, (stop, count)
        assert current == stop


def test_cas_calls_have_surrounding_checkpoints_and_only_original_new_tree_writes(
    case, monkeypatch
):
    events = []
    read = GitMaterialCAS.read
    persist = GitMaterialCAS.persist

    def record_read(port, reference):
        events.append("read_before")
        result = read(port, reference)
        events.append("read_after")
        return result

    def record_persist(port, material):
        events.append("persist_before")
        result = persist(port, material)
        events.append("persist_after")
        return result

    monkeypatch.setattr(GitMaterialCAS, "read", record_read)
    monkeypatch.setattr(GitMaterialCAS, "persist", record_persist)
    scope, diff = build(case, checkpoint=lambda: events.append("check"))
    for index, event in enumerate(events):
        if event in {"persist_before", "persist_after"}:
            assert events[index - 1 if event == "persist_before" else index + 1] == "check"
        elif event == "read_before" and events[index - 1] != "persist_before":
            assert events[index - 1] == "check"
        elif event == "read_after" and events[index + 1] != "persist_after":
            assert events[index + 1] == "check"
    assert events.count("persist_before") == len(diff.projection.new_trees)
    assert type(scope) is GitInventoryScope
