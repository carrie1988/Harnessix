"""局部分层的机械边界与真实临时 SQLite CAS 负控；不发现仓库其他测试。"""

from __future__ import annotations

import asyncio
import threading
from collections import Counter
from contextlib import contextmanager

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery import git_tree_projection as projection
from harnessix.delivery.contracts import WorkspaceFileVersion, WorkspaceMutation
from harnessix.delivery.git_authentication_control import (
    GitAuthenticationControl,
    pure_git_authentication,
)
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.git_object_material import GitObjectMaterial
from harnessix.delivery.git_tree_closure import GitTreeClosureLimits
from harnessix.delivery.git_tree_diff import prepare_git_tree_diff
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.workspace.native_observation_io import UpstreamCheckpointError


def _noop():
    pass


def _tree_material(fmt, rows=()):
    rows = sorted(rows, key=lambda row: row[1] + (b"/" if row[0] == "40000" else b"\0"))
    body = b"".join(
        mode.encode("ascii") + b" " + name + b"\0" + bytes.fromhex(child.object_id)
        for mode, name, child in rows
    )
    return GitObjectMaterial.from_body("tree", fmt, body)


def _version(reference=None, mode=420):
    if reference is None:
        return WorkspaceFileVersion(presence="absent", size=0)
    return WorkspaceFileVersion(
        presence="file", sha256=reference.body_sha256, size=reference.body_bytes, mode=mode
    )


def _mutation(path="f", before=None, after=None, mode=420):
    return WorkspaceMutation(path=path, before=_version(before), after=_version(after, mode))


def _snapshot(store):
    return tuple(
        (p.relative_to(store._root).as_posix(), p.read_bytes(), p.stat().st_mode)
        for p in sorted(store._root.rglob("*"))
        if p.is_file()
    )


@pytest.fixture(params=["sha1", "sha256"])
def case(tmp_path, request):
    fmt = request.param
    state = tmp_path / "synthetic-cas"
    with SQLiteWorkspaceTransactionStore(state) as store:
        cas = GitMaterialCAS(store)
        old, new, keep = (
            cas.persist(GitObjectMaterial.from_body("blob", fmt, body))
            for body in (b"old\n", b"new\0\xff", b"keep")
        )
        empty = cas.persist(_tree_material(fmt))
        shared = cas.persist(
            _tree_material(fmt, (("40000", b"empty", empty), ("100644", b"f", old)))
        )
        root = cas.persist(
            _tree_material(
                fmt,
                (
                    ("40000", b"a", shared),
                    ("40000", b"b", shared),
                    ("40000", b"empty", empty),
                    ("100755", b"keep", keep),
                    ("100644", b"replace", old),
                ),
            )
        )
    with SQLiteWorkspaceTransactionStore(state, read_only=True) as store:
        yield (
            GitMaterialCAS(store),
            root,
            (old, keep, empty, shared),
            (
                _mutation("a/f", old, new, 493),
                _mutation("new/deep/file", after=new),
                _mutation("replace", old),
            ),
            (new,),
        )


def _project(case, checkpoint):
    return projection.prepare_git_tree_projection(
        *case,
        platform="posix",
        limits=GitTreeClosureLimits(512, 1024 * 1024, 1024, 128),
        checkpoint=checkpoint,
    )


def _capture_segments(monkeypatch, saved, edges=None):
    @contextmanager
    def observed(checkpoint):
        if edges is not None:
            edges.append("enter")
        with pure_git_authentication(checkpoint) as check:
            if check is not checkpoint:
                saved.append(check)
            yield check
            if edges is not None:
                edges.append("exit")

    monkeypatch.setattr(projection, "pure_git_authentication", observed, raising=False)


def test_segments_full_boundaries_and_all_saved_tokens_revoked_at_real_cas(case, monkeypatch):
    counts = Counter()
    saved, edges, phases, tokens = [], [], [], []

    def local():
        counts["local"] += 1

    def full():
        assert control._segment is None
        counts["full"] += 1

    control = GitAuthenticationControl(local, full)
    # 实例同名方法不是可信扩展点，必须调用原类方法。
    control.pure = lambda: pytest.fail("instance pure shadow executed")
    _capture_segments(monkeypatch, saved, edges)
    for name in ("_before", "_after", "_apply", "_namespace", "_trees", "_directory_paths"):
        original = getattr(projection, name, None)
        assert original is not None, name

        def stage(*args, _name=name, _original=original, **kwargs):
            phases.append((_name, control._segment is not None))
            tokens.append(control._segment)
            assert (args[-1] is control) == (_name == "_after")
            return _original(*args, **kwargs)

        monkeypatch.setattr(projection, name, stage)

    original_read = GitMaterialCAS.read
    original_add = projection._Objects.add
    reads = Counter()
    additions = []

    def add(objects, reference):
        additions.append((reference.object_id, control._segment is not None))
        return original_add(objects, reference)

    def read(cas, reference):
        assert control._segment is None
        reads[reference.object_id] += 1
        return original_read(cas, reference)

    def store_checkpoint():
        assert control._segment is None
        before = counts.copy()
        for check in saved:
            check()
        assert counts["local"] == before["local"]
        assert counts["full"] == before["full"] + len(saved)

    monkeypatch.setattr(GitMaterialCAS, "read", read)
    monkeypatch.setattr(projection._Objects, "add", add)
    monkeypatch.setattr(case[0].store, "_checkpoint", store_checkpoint)
    before = _snapshot(case[0].store)
    result = _project(case, control)
    assert _snapshot(case[0].store) == before
    assert phases == [
        ("_directory_paths", True),
        ("_before", True),
        ("_after", False),
        ("_apply", True),
        ("_namespace", True),
        ("_trees", True),
    ]
    assert edges == [edge for _ in saved for edge in ("enter", "exit")]
    assert tokens[3] is tokens[4] is tokens[5] and tokens[1] is not tokens[3]
    assert counts["local"] > len(saved) and control._segment is None
    # 目录重读未被 Closure 观察缓存替代；共享 tree 仍逐路径展开。
    for reference in result.base.objects:
        assert reads[reference.object_id] == (2 if reference.object_type == "tree" else 1)
    assert reads[case[4][0].object_id] == 1
    assert additions[: len(result.base.objects)] == [
        (reference.object_id, True) for reference in result.base.objects
    ]
    assert additions[len(result.base.objects)] == (case[4][0].object_id, False)
    assert all(pure for _, pure in additions[len(result.base.objects) + 1 :])
    before = counts.copy()
    for check in saved:
        check()
    assert counts["local"] == before["local"]
    assert counts["full"] == before["full"] + len(saved)


def test_directory_paths_has_no_cas_preserves_lifo_and_shared_empty_paths(case, monkeypatch):
    base = _project(case, _noop).base
    trees = {
        ref.object_id: projection.parse_git_tree(
            case[0].read(ref), max_entries=1024, checkpoint=_noop
        )
        for ref in base.objects
        if ref.object_type == "tree"
    }
    visited = []

    class TracedTrees(dict):
        def __getitem__(self, key):
            visited.append(key)
            return super().__getitem__(key)

    monkeypatch.setattr(GitMaterialCAS, "read", lambda *a: pytest.fail("DFS performed CAS IO"))
    paths = projection._directory_paths(TracedTrees(trees), base.root.object_id, _noop)
    assert paths == {"", "a", "a/empty", "b", "b/empty", "empty"}
    empty, shared = case[2][2:]
    expected = [base.root.object_id, empty.object_id, shared.object_id, empty.object_id]
    expected += [shared.object_id, empty.object_id]
    assert visited == expected
    visited.clear()
    assert projection._directory_paths(TracedTrees(trees), base.root.object_id, _noop) == paths
    assert visited == expected


def test_final_full_checkpoint_still_precedes_return_sort(case, monkeypatch):
    events = []
    original_sorted = sorted
    original_projection = projection.GitTreeProjection

    def sorted_values(values, **kwargs):
        values = tuple(values)
        if values and all(type(value) is projection.GitTreeFile for value in values):
            events.append("return-sort")
        return original_sorted(values, **kwargs)

    def constructed(*args):
        events.append("return")
        return original_projection(*args)

    monkeypatch.setattr(projection, "sorted", sorted_values, raising=False)
    monkeypatch.setattr(projection, "GitTreeProjection", constructed)
    _project(case, GitAuthenticationControl(_noop, lambda: events.append("full")))
    assert events[-3:] == ["full", "return-sort", "return"]


def test_deep_shared_paths_keep_original_capacity_but_localize_compute(case, tmp_path, monkeypatch):
    fmt = case[1].object_format
    state = tmp_path / "deep-synthetic-cas"
    with SQLiteWorkspaceTransactionStore(state) as store:
        cas = GitMaterialCAS(store)
        old = cas.persist(GitObjectMaterial.from_body("blob", fmt, b"old"))
        new = cas.persist(GitObjectMaterial.from_body("blob", fmt, b"new\0\xff"))
        empty = cas.persist(_tree_material(fmt))
        child = cas.persist(_tree_material(fmt, (("100644", b"f", old),)))
        catalog = [old, empty, child]
        for _ in range(96):
            child = cas.persist(_tree_material(fmt, (("40000", b"d", child),)))
            catalog.append(child)
        root = cas.persist(
            _tree_material(
                fmt,
                (("40000", b"a", child), ("40000", b"b", child), ("40000", b"empty", empty)),
            )
        )
    path = "a/" + "d/" * 96 + "f"
    counts, active = Counter(), []
    for name in ("_before", "_apply", "_namespace", "_trees"):
        original = getattr(projection, name)

        def stage(*args, _name=name, _original=original, **kwargs):
            active.append(_name)
            try:
                return _original(*args, **kwargs)
            finally:
                active.pop()

        monkeypatch.setattr(projection, name, stage)

    def local():
        counts[("local", active[-1] if active else "boundary")] += 1

    def full():
        counts[("full", active[-1] if active else "boundary")] += 1

    with SQLiteWorkspaceTransactionStore(state, read_only=True) as store:
        before = _snapshot(store)
        result = _project(
            (GitMaterialCAS(store), root, tuple(catalog), (_mutation(path, old, new),), (new,)),
            GitAuthenticationControl(local, full),
        )
        assert _snapshot(store) == before
    assert result.tree_depth == result.base.tree_depth == 97
    assert result.expanded_entries == result.base.expanded_entries == 197
    assert [file.path for file in result.files] == [path, "b/" + "d/" * 96 + "f"]
    assert result.files[0].material == new and result.files[1].material == old
    assert counts[("local", "_namespace")] >= 197
    assert counts[("local", "_trees")] >= 390
    for name in ("_before", "_apply", "_namespace", "_trees"):
        assert counts[("local", name)] > 0 and counts[("full", name)] == 0


def test_output_exact_bodies_oids_modes_empty_and_shared_paths_no_writes(case, monkeypatch):
    legacy = _project(case, _noop)
    before = _snapshot(case[0].store)
    database = tuple(case[0].store._db.iterdump())

    def forbidden(*args, **kwargs):
        pytest.fail("projection attempted a write")

    monkeypatch.setattr(GitMaterialCAS, "persist", forbidden)
    monkeypatch.setattr(SQLiteWorkspaceTransactionStore, "put_blob", forbidden)
    result = _project(case, GitAuthenticationControl(_noop, _noop))
    assert result == legacy
    assert _snapshot(case[0].store) == before
    assert tuple(case[0].store._db.iterdump()) == database
    assert case[0].store._db.total_changes == 0
    assert [(f.path, f.mode) for f in result.files] == [
        ("a/f", "100755"),
        ("b/f", "100644"),
        ("keep", "100755"),
        ("new/deep/file", "100644"),
    ]
    old, keep, empty, shared = case[2]
    new = case[4][0]
    fmt = case[1].object_format
    a = _tree_material(fmt, (("40000", b"empty", empty), ("100755", b"f", new)))
    deep = _tree_material(fmt, (("100644", b"file", new),))
    parent = _tree_material(fmt, (("40000", b"deep", deep),))
    root = _tree_material(
        fmt,
        (
            ("40000", b"a", a),
            ("40000", b"b", shared),
            ("40000", b"empty", empty),
            ("100755", b"keep", keep),
            ("40000", b"new", parent),
        ),
    )
    assert result.root == root
    assert result.new_trees == tuple(sorted((a, deep, parent, root), key=lambda m: m.object_id))
    assert result.expanded_entries == 11 and result.tree_depth == 2
    assert result.files[1].material == old


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
def test_unknown_callbacks_keep_full_trace_and_never_local(case, monkeypatch, kind):
    events = []

    def full():
        events.append("checkpoint")

    class Proxy:
        def __call__(self):
            full()

        def pure(self):
            pytest.fail("unknown proxy pure executed")

    class Subclass(GitAuthenticationControl):
        def pure(self):
            pytest.fail("subclass pure executed")

    checkpoint = {
        "function": full,
        "proxy": Proxy(),
        "subclass": Subclass(lambda: pytest.fail("foreign local executed"), full),
    }[kind]
    original_read = GitMaterialCAS.read

    def read(cas, reference):
        events.append(("read", reference.object_id))
        return original_read(cas, reference)

    monkeypatch.setattr(GitMaterialCAS, "read", read)
    result = _project(case, checkpoint)
    observed = events.copy()
    events.clear()
    assert _project(case, full) == result
    assert events == observed
    # 固定旧实现的完整 checkpoint/read 序列（非只比较总数）。
    groups = []
    for event in events:
        if event == "checkpoint":
            if groups and type(groups[-1]) is int:
                groups[-1] += 1
            else:
                groups.append(1)
        else:
            groups.append(event)
    old, keep, empty, shared = case[2]
    root, new = case[1], case[4][0]
    tail = (
        [8, ("read", empty.object_id), 3, ("read", shared.object_id), 29]
        if root.object_format == "sha1"
        else [9, ("read", shared.object_id), 5, ("read", empty.object_id), 26]
    )
    assert groups == [
        9,
        ("read", root.object_id),
        13,
        ("read", keep.object_id),
        3,
        ("read", old.object_id),
        3,
        ("read", shared.object_id),
        10,
        ("read", empty.object_id),
        15,
        ("read", root.object_id),
        *tail,
        ("read", new.object_id),
        42,
    ]


@pytest.mark.parametrize(
    "stage", ["parse_git_tree", "_directory_paths", "_before", "_apply", "_namespace", "_trees"]
)
@pytest.mark.parametrize("failure_kind", ["cancel", "task_cancel", "upstream"])
def test_local_first_failure_identity_and_revocation(case, monkeypatch, stage, failure_kind):
    failure = {
        "cancel": TurnCancelled(),
        "task_cancel": asyncio.CancelledError(),
        "upstream": UpstreamCheckpointError(TurnCancelled()),
    }[failure_kind]
    saved, active = [], []
    full_count = 0

    def local():
        if active:
            raise failure

    def full():
        nonlocal full_count
        assert control._segment is None
        full_count += 1

    control = GitAuthenticationControl(local, full)
    _capture_segments(monkeypatch, saved)
    original = getattr(projection, stage)

    def fail_stage(*args, **kwargs):
        active.append(stage)
        return original(*args, **kwargs)

    monkeypatch.setattr(projection, stage, fail_stage)
    before = _snapshot(case[0].store)
    with pytest.raises(type(failure)) as rejected:
        _project(case, control)
    assert rejected.value is failure
    assert control._segment is None and _snapshot(case[0].store) == before
    previous = full_count
    for check in saved:
        check()
    assert full_count == previous + len(saved)


@pytest.mark.parametrize("stage", ["_before", "_after", "_trees"])
def test_semantic_drift_at_exit_or_next_entry_blocks_result(case, monkeypatch, stage):
    failure = KernelError("fixture_origin_drift", "synthetic origin changed")
    drift = False
    original = getattr(projection, stage)

    def full():
        assert control._segment is None
        if drift:
            raise failure

    control = GitAuthenticationControl(_noop, full)

    def change(*args, **kwargs):
        nonlocal drift
        result = original(*args, **kwargs)
        drift = True
        return result

    monkeypatch.setattr(projection, stage, change)
    before = _snapshot(case[0].store)
    with pytest.raises(KernelError) as rejected:
        _project(case, control)
    assert rejected.value is failure
    assert control._segment is None and _snapshot(case[0].store) == before


@pytest.mark.parametrize("stage", ["_before", "_after", "_trees"])
def test_declared_control_binding_drift_is_rejected_at_boundary(case, monkeypatch, stage):
    control = GitAuthenticationControl(_noop, _noop)
    original = getattr(projection, stage)

    def change(*args, **kwargs):
        result = original(*args, **kwargs)
        control._authenticate = lambda: pytest.fail("replacement authenticate executed")
        return result

    monkeypatch.setattr(projection, stage, change)
    with pytest.raises(KernelError) as rejected:
        _project(case, control)
    assert rejected.value.code == "git_authentication_control_invalid"
    assert control._segment is None


def test_exit_local_failure_is_not_masked_by_full_authentication(case, monkeypatch):
    local_failure, boundary_failure = TurnCancelled(), KernelError("fixture_drift", "changed")
    failed = False
    original = projection._trees

    def local():
        if failed:
            raise local_failure

    def full():
        if failed:
            raise boundary_failure

    def completed(*args, **kwargs):
        nonlocal failed
        result = original(*args, **kwargs)
        failed = True
        return result

    control = GitAuthenticationControl(local, full)
    monkeypatch.setattr(projection, "_trees", completed)
    with pytest.raises(TurnCancelled) as rejected:
        _project(case, control)
    assert rejected.value is local_failure and control._segment is None


def test_real_store_upstream_first_failure_identity_no_io_in_pure(case, monkeypatch):
    failure = TurnCancelled()
    control = GitAuthenticationControl(_noop, _noop)

    def reject():
        assert control._segment is None
        raise failure

    monkeypatch.setattr(case[0].store, "_checkpoint", reject)
    before = _snapshot(case[0].store)
    with pytest.raises(TurnCancelled) as rejected:
        _project(case, control)
    assert rejected.value is failure
    assert control._segment is None and _snapshot(case[0].store) == before


@pytest.mark.parametrize("edge", ["enter", "exit"])
def test_snapshot_authenticates_both_boundaries_without_failure_mapping(edge):
    full_count, local_count = 0, 0
    failure = TurnCancelled()

    def full():
        nonlocal full_count
        assert control._segment is None
        full_count += 1
        if full_count == (1 if edge == "enter" else 2):
            raise failure

    def local():
        nonlocal local_count
        local_count += 1

    control = GitAuthenticationControl(local, full)
    mutation = _mutation(after=_dummy_blob())
    with pytest.raises(TurnCancelled) as rejected:
        projection.snapshot_git_tree_mutations((mutation,), "posix", control)
    assert rejected.value is failure and control._segment is None
    assert full_count == (1 if edge == "enter" else 2)
    assert local_count == (0 if edge == "enter" else 2)


def _dummy_blob():
    return GitObjectMaterial.from_body("blob", "sha1", b"fixture")


def test_nested_snapshot_revokes_outer_and_preserves_nested_first_failure(monkeypatch):
    saved = []
    nested = False
    counts = Counter()
    mutation = _mutation(after=_dummy_blob())
    failure = TurnCancelled()

    def local():
        nonlocal nested
        counts["local"] += 1
        if nested:
            raise failure
        nested = True
        projection.snapshot_git_tree_mutations((mutation,), "posix", control)

    def full():
        assert control._segment is None
        counts["full"] += 1

    control = GitAuthenticationControl(local, full)
    _capture_segments(monkeypatch, saved)
    with pytest.raises(TurnCancelled) as rejected:
        projection.snapshot_git_tree_mutations((mutation,), "posix", control)
    assert rejected.value is failure and len(saved) == 2 and control._segment is None
    previous = counts.copy()
    for check in saved:
        check()
    assert counts["local"] == previous["local"]
    assert counts["full"] == previous["full"] + 2


def test_successful_nested_snapshot_cannot_restore_outer_token(monkeypatch):
    saved, counts = [], Counter()
    mutation = _mutation(after=_dummy_blob())
    nested = False

    def local():
        nonlocal nested
        counts["local"] += 1
        if not nested:
            nested = True
            assert projection.snapshot_git_tree_mutations((mutation,), "posix", control) == (
                mutation,
            )
            assert control._segment is None
            previous = counts.copy()
            saved[0]()
            assert counts["local"] == previous["local"]
            assert counts["full"] == previous["full"] + 1

    control = GitAuthenticationControl(local, lambda: counts.update(["full"]))
    _capture_segments(monkeypatch, saved)
    assert projection.snapshot_git_tree_mutations((mutation,), "posix", control) == (mutation,)
    assert len(saved) == 2 and control._segment is None


@pytest.mark.asyncio
async def test_foreign_task_and_thread_never_receive_origin_local():
    counts = Counter()
    control = GitAuthenticationControl(
        lambda: counts.update(["local"]), lambda: counts.update(["full"])
    )
    mutation = _mutation(after=_dummy_blob())

    async def child(checkpoint=control):
        return projection.snapshot_git_tree_mutations((mutation,), "posix", checkpoint)

    assert await asyncio.create_task(child()) == (mutation,)
    assert counts["local"] == 0 and counts["full"] > 0
    with pure_git_authentication(control) as captured:
        assert await asyncio.create_task(child(captured)) == (mutation,)
        assert counts["local"] == 0 and control._segment is None
    results, errors = [], []

    def thread():
        try:
            results.append(projection.snapshot_git_tree_mutations((mutation,), "posix", captured))
        except BaseException as error:
            errors.append(error)

    with pure_git_authentication(control) as captured:
        worker = threading.Thread(target=thread)
        worker.start()
        worker.join()
    assert not errors and results == [(mutation,)]
    assert counts["local"] == 0 and control._segment is None


@pytest.mark.asyncio
async def test_foreign_task_and_thread_projection_uses_only_original_full_authentication(case):
    counts = Counter()
    control = GitAuthenticationControl(
        lambda: counts.update(["local"]), lambda: counts.update(["full"])
    )

    async def child():
        return _project(case, control)

    expected = _project(case, _noop)
    assert await asyncio.create_task(child()) == expected
    assert counts["local"] == 0 and counts["full"] > 0 and control._segment is None
    results, errors = [], []

    def thread():
        try:
            results.append(_project(case, control))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=thread)
    worker.start()
    worker.join()
    assert not errors and results == [expected]
    assert counts["local"] == 0 and control._segment is None


@pytest.mark.parametrize("model", ["mutation", "version"])
@pytest.mark.parametrize("key_type", [object, str])
def test_sparse_model_dict_rejects_hostile_key_without_hash_or_eq(model, key_type):
    counts = Counter()

    class HostileKey(key_type):
        def __hash__(self):
            counts["hash"] += 1
            return hash("path" if model == "mutation" else "presence")

        def __eq__(self, other):
            counts["eq"] += 1
            return False

    mutation = _mutation(after=_dummy_blob())
    value = mutation if model == "mutation" else mutation.before
    state = dict(vars(value))
    state[HostileKey()] = None
    for i in range(1024):
        state[f"padding{i}"] = None
    for i in range(1024):
        del state[f"padding{i}"]
    object.__setattr__(value, "__dict__", state)
    counts.clear()
    with pytest.raises(KernelError) as rejected:
        projection.snapshot_git_tree_mutations((mutation,), "posix", _noop)
    assert rejected.value.code == "git_tree_projection_invalid"
    assert rejected.value.__cause__ is None and not counts


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", b"f"),
        ("presence", b"file"),
        ("size", True),
        ("sha256", b"0" * 64),
        ("mode", True),
        ("unknown", None),
    ],
)
def test_rebuilt_model_fields_remain_strict_with_fixed_error_code(field, value):
    mutation = _mutation(after=_dummy_blob())
    state = vars(mutation if field in {"path", "unknown"} else mutation.after)
    state[field] = value
    with pytest.raises(KernelError) as rejected:
        projection.snapshot_git_tree_mutations((mutation,), "posix", _noop)
    assert rejected.value.code == "git_tree_projection_invalid"


@pytest.mark.parametrize("model", ["mutation", "version"])
def test_model_dict_subclass_rejected_before_any_dict_callback(model):
    class ExecutableDict(dict):
        def __iter__(self):
            pytest.fail("dict subclass iteration")

        def items(self):
            pytest.fail("dict subclass items")

        def __getitem__(self, key):
            pytest.fail("dict subclass lookup")

    mutation = _mutation(after=_dummy_blob())
    value = mutation if model == "mutation" else mutation.before
    object.__setattr__(value, "__dict__", ExecutableDict(vars(value)))
    with pytest.raises(KernelError) as rejected:
        projection.snapshot_git_tree_mutations((mutation,), "posix", _noop)
    assert rejected.value.code == "git_tree_projection_invalid"


def test_diff_and_projection_each_rebuild_original_snapshot(case, monkeypatch):
    calls = []
    original = projection.snapshot_git_tree_mutations

    def observed(values, platform, checkpoint):
        result = original(values, platform, checkpoint)
        if checkpoint is control:
            calls.append(result)
        return result

    control = GitAuthenticationControl(_noop, _noop)
    monkeypatch.setattr(projection, "snapshot_git_tree_mutations", observed)
    # Diff 导入绑定独立，仍保留两次真实快照，而不是跨端缓存同一结果。
    from harnessix.delivery import git_tree_diff

    monkeypatch.setattr(git_tree_diff, "snapshot_git_tree_mutations", observed)
    result = prepare_git_tree_diff(
        *case,
        platform="posix",
        limits=GitTreeClosureLimits(512, 1024 * 1024, 1024, 128),
        checkpoint=control,
        max_diff_bytes=1024 * 1024,
    )
    assert result is not None and len(calls) == 2
    assert calls[0] == calls[1] == case[3]
    assert calls[0] is not calls[1] and calls[0] is not case[3]
    assert all(a is not b for a, b in zip(calls[0], calls[1], strict=True))
    assert all(a.before is not b.before for a, b in zip(calls[0], calls[1], strict=True))
    assert all(a.after is not b.after for a, b in zip(calls[0], calls[1], strict=True))
