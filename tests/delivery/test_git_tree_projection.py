"""实际原 CAS 的完整纯树投影；Git 仅用于合成差分，不冒充业务执行。"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import FrozenInstanceError, replace

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import (
    MAX_TRANSACTION_FILE_BYTES,
    WorkspaceFileVersion,
    WorkspaceMutation,
)
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.git_object_material import GitObjectMaterial
from harnessix.delivery.git_object_references import parse_git_tree
from harnessix.delivery.git_tree_projection import GitTreeProjection, prepare_git_tree_projection
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from tests.delivery.test_git_tree_closure import _checkpoint, _limits, _persist, _snapshot, _tree

_FORMATS = ("sha1", "sha256")


@pytest.fixture
def actual_cas(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        tuple(store._db.iterdump())
        yield GitMaterialCAS(store)


def _version(reference=None, mode=420):
    if reference is None:
        return WorkspaceFileVersion(presence="absent", size=0)
    return WorkspaceFileVersion(
        presence="file", sha256=reference.body_sha256, size=reference.body_bytes, mode=mode
    )


def _mutation(path, before=None, after=None, before_mode=420, after_mode=420):
    return WorkspaceMutation(
        path=path, before=_version(before, before_mode), after=_version(after, after_mode)
    )


def _project(
    cas,
    root,
    catalog,
    mutations,
    after=(),
    *,
    platform="posix",
    limits=None,
    checkpoint=_checkpoint,
):
    return prepare_git_tree_projection(
        cas,
        root,
        catalog,
        mutations,
        after,
        platform=platform,
        limits=_limits(4096, 64 * 1024 * 1024, 4096, 128) if limits is None else limits,
        checkpoint=checkpoint,
    )


def _reject(cas, root, catalog, mutations, after=(), **kwargs):
    before = _snapshot(cas.store)
    with pytest.raises(KernelError) as error:
        _project(cas, root, catalog, mutations, after, **kwargs)
    assert "private-name" not in str(error.value) and "private-body" not in str(error.value)
    assert error.value.__cause__ is None
    assert _snapshot(cas.store) == before
    return error.value


def _entries(material):
    return parse_git_tree(material, max_entries=4096, checkpoint=_checkpoint)


def _simple(cas, fmt):
    old = _persist(cas, fmt, body=b"old-private-body")
    after = _persist(cas, fmt, body=b"new-private-body\0\xff")
    root = _tree(cas, fmt, (("100644", b"private-name", old),))
    mutation = _mutation("private-name", old, after)
    return root, old, after, mutation


@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_three_operations_keep_complete_unchanged_files_empty_dirs_and_modes(
    actual_cas, fmt, platform
):
    keep = _persist(actual_cas, fmt, body=b"keep")
    old = _persist(actual_cas, fmt, body=b"old")
    new = _persist(actual_cas, fmt, body=b"\0\xff")
    empty = _tree(actual_cas, fmt)
    child = _tree(actual_cas, fmt, (("100644", b"remove", old),))
    root = _tree(
        actual_cas,
        fmt,
        (
            ("40000", b"empty", empty),
            ("100755", b"keep", keep),
            ("40000", b"nested", child),
            ("100644", b"update", old),
        ),
    )
    mutations = (
        _mutation("add", after=new),
        _mutation("nested/remove", old),
        _mutation("update", old, new, after_mode=493),
    )
    before = _snapshot(actual_cas.store)
    result = _project(
        actual_cas, root, (keep, old, empty, child), mutations, (new,), platform=platform
    )
    assert type(result) is GitTreeProjection
    assert [(f.path, f.mode, f.material) for f in result.files] == [
        ("add", "100644", new),
        ("keep", "100755", keep),
        ("update", "100755", new),
    ]
    assert [e.name for e in _entries(result.root)] == [b"add", b"empty", b"keep", b"update"]
    assert result.expanded_entries == 4 and result.tree_depth == 1
    assert result.base.expanded_entries == 5 and len(result.base.files) == 3
    assert result.new_trees == (result.root,)
    refs = {r.object_id: r.body_bytes for r in (*result.base.objects, new)}
    refs.update({m.object_id: m.body_bytes for m in result.new_trees})
    assert result.objects_body_bytes == sum(refs.values())
    assert _snapshot(actual_cas.store) == before
    for name in ("base", "root", "new_trees", "files"):
        assert name + "=" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.files = ()


@pytest.mark.parametrize("fmt", _FORMATS)
def test_shared_tree_is_path_copy_not_in_place_change_and_new_trees_deduplicate(actual_cas, fmt):
    old = _persist(actual_cas, fmt, body=b"old")
    new = _persist(actual_cas, fmt, body=b"new")
    shared = _tree(actual_cas, fmt, (("100644", b"f", old),))
    root = _tree(actual_cas, fmt, tuple(("40000", name, shared) for name in (b"a", b"b", b"c")))
    result = _project(actual_cas, root, (shared, old), (_mutation("a/f", old, new),), (new,))
    assert [(f.path, f.material) for f in result.files] == [
        ("a/f", new),
        ("b/f", old),
        ("c/f", old),
    ]
    rows = _entries(result.root)
    assert rows[0].child.object_id != shared.object_id
    assert rows[1].child.object_id == rows[2].child.object_id == shared.object_id
    both = _project(
        actual_cas,
        root,
        (shared, old),
        (_mutation("a/f", old, new), _mutation("b/f", old, new)),
        (new,),
    )
    assert len(both.new_trees) == 2
    assert tuple(m.object_id for m in both.new_trees) == tuple(
        sorted({m.object_id for m in both.new_trees})
    )


@pytest.mark.parametrize("fmt", _FORMATS)
def test_mode_only_reuses_body_and_delete_before_add_allows_file_to_directory(actual_cas, fmt):
    blob = _persist(actual_cas, fmt)
    root = _tree(actual_cas, fmt, (("100644", b"a", blob), ("100644", b"run", blob)))
    result = _project(
        actual_cas,
        root,
        (blob,),
        (
            _mutation("a", blob),
            _mutation("a/f", after=blob),
            _mutation("run", blob, blob, after_mode=493),
        ),
        (blob,),
    )
    assert [(f.path, f.mode) for f in result.files] == [("a/f", "100644"), ("run", "100755")]
    assert result.tree_depth == 1 and result.expanded_entries == 3
    assert result.objects_body_bytes == root.body_bytes + blob.body_bytes + sum(
        m.body_bytes for m in result.new_trees
    )


@pytest.mark.parametrize("fmt", _FORMATS)
def test_deleted_only_tree_recursively_prunes_but_unrelated_empty_tree_survives(actual_cas, fmt):
    blob = _persist(actual_cas, fmt)
    leaf = _tree(actual_cas, fmt, (("100644", b"f", blob),))
    middle = _tree(actual_cas, fmt, (("40000", b"d", leaf),))
    empty = _tree(actual_cas, fmt)
    root = _tree(actual_cas, fmt, (("40000", b"a", middle), ("40000", b"unrelated", empty)))
    result = _project(actual_cas, root, (blob, leaf, middle, empty), (_mutation("a/d/f", blob),))
    assert result.files == () and [e.name for e in _entries(result.root)] == [b"unrelated"]
    assert result.tree_depth == 1 and result.expanded_entries == 1
    lone = _project(actual_cas, leaf, (blob,), (_mutation("f", blob),))
    assert lone.root.body == b"" and lone.tree_depth == lone.expanded_entries == 0


@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize(
    "size", [0, 1, MAX_TRANSACTION_FILE_BYTES], ids=["empty", "binary", "8MiB"]
)
def test_readonly_reopen_full_binary_body_no_rows_or_file_bytes_written(
    tmp_path, fmt, size, monkeypatch
):
    state = tmp_path / "readonly"
    body = (bytes(range(256)) * ((size + 255) // 256))[:size]
    with SQLiteWorkspaceTransactionStore(state) as store:
        cas = GitMaterialCAS(store)
        root = _tree(cas, fmt)
        blob = _persist(cas, fmt, body=body)
    with SQLiteWorkspaceTransactionStore(state, read_only=True) as store:
        cas = GitMaterialCAS(store)
        rows = tuple(store._db.iterdump())
        changes = store._db.total_changes
        before = _snapshot(store)

        def forbidden(*args, **kwargs):
            pytest.fail("纯投影不能写 CAS 或数据库")

        monkeypatch.setattr(GitMaterialCAS, "persist", forbidden)
        monkeypatch.setattr(SQLiteWorkspaceTransactionStore, "put_blob", forbidden)
        result = _project(cas, root, (), (_mutation("f", after=blob),), (blob,))
        assert cas.read(result.files[0].material).body == body
        assert result.root.body.endswith(bytes.fromhex(blob.object_id))
        assert tuple(store._db.iterdump()) == rows and store._db.total_changes == changes
        assert _snapshot(store) == before


@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize("field", ["max_objects", "max_body_bytes", "max_entries", "max_depth"])
def test_each_capacity_exact_boundary_and_one_below_rejects_whole_target(actual_cas, fmt, field):
    root = _tree(actual_cas, fmt)
    blob = _persist(actual_cas, fmt)
    mutations = (_mutation("d/f", after=blob),)
    result = _project(actual_cas, root, (), mutations, (blob,))
    bound = _limits(4, result.objects_body_bytes, 2, 1)
    assert _project(actual_cas, root, (), mutations, (blob,), limits=bound) == result
    _reject(
        actual_cas,
        root,
        (),
        mutations,
        (blob,),
        limits=replace(bound, **{field: getattr(bound, field) - 1}),
    )


@pytest.mark.parametrize("fmt", _FORMATS)
def test_256_mutations_exact_and_257_rejected(actual_cas, fmt):
    root = _tree(actual_cas, fmt)
    blob = _persist(actual_cas, fmt, body=b"x")
    mutations = tuple(_mutation(f"f{i:03d}", after=blob) for i in range(256))
    result = _project(actual_cas, root, (), mutations, (blob,))
    assert len(result.files) == result.expanded_entries == 256
    _reject(actual_cas, root, (), (*mutations, _mutation("f256", after=blob)), (blob,))
    _reject(actual_cas, root, (), (), ())


@pytest.mark.parametrize("fmt", _FORMATS)
def test_32_mib_mirror_budget_counts_per_path_not_deduplicated(actual_cas, fmt):
    root = _tree(actual_cas, fmt)
    blob = _persist(actual_cas, fmt, body=b"x" * MAX_TRANSACTION_FILE_BYTES)
    mutations = tuple(_mutation(f"f{i}", after=blob) for i in range(4))
    result = _project(actual_cas, root, (), mutations, (blob,))
    assert len(result.files) == 4 and result.objects_body_bytes < 9 * 1024 * 1024
    _reject(actual_cas, root, (), (*mutations, _mutation("f4", after=blob)), (blob,))


@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize(
    "field,value", [("presence", "absent"), ("sha256", "a" * 64), ("size", 1), ("mode", 493)]
)
def test_before_full_presence_sha_length_mode_must_match(actual_cas, fmt, field, value):
    root, old, after, mutation = _simple(actual_cas, fmt)
    if field == "presence":
        object.__setattr__(mutation, "before", _version())
    else:
        object.__setattr__(mutation.before, field, value)
    _reject(actual_cas, root, (old,), (mutation,), (after,))


@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "unused",
        "duplicate",
        "wrongtype",
        "wrongformat",
        "wrongsha",
        "wrongsize",
        "corrupt",
        "oid_collision",
    ],
)
def test_after_catalog_exact_and_full_cas_authentication(actual_cas, fmt, case):
    root, old, after, mutation = _simple(actual_cas, fmt)
    refs = (after,)
    if case == "missing":
        refs = ()
    elif case == "unused":
        refs = (after, old)
    elif case == "duplicate":
        refs = (after, after)
    elif case == "wrongtype":
        refs = (_tree(actual_cas, fmt),)
    elif case == "wrongformat":
        refs = (
            _persist(
                actual_cas, "sha256" if fmt == "sha1" else "sha1", body=b"new-private-body\0\xff"
            ),
        )
    elif case in {"wrongsha", "wrongsize"}:
        object.__setattr__(
            mutation.after,
            "sha256" if case == "wrongsha" else "size",
            "a" * 64 if case == "wrongsha" else 1,
        )
    elif case == "oid_collision":
        object.__setattr__(after, "object_id", root.object_id)
    else:
        (actual_cas.store._blobs / after.cas_digest).write_bytes(b"corrupted")
    _reject(actual_cas, root, (old,), (mutation,), refs)


@pytest.mark.parametrize("fmt", _FORMATS)
def test_missing_unchanged_base_blob_rejected_and_directory_is_not_absent_file(actual_cas, fmt):
    root, old, after, mutation = _simple(actual_cas, fmt)
    _reject(actual_cas, root, (), (_mutation("a-new", after=after),), (after,))
    empty = _tree(actual_cas, fmt)
    tree = _tree(actual_cas, fmt, (("40000", b"private-name", empty),))
    _reject(actual_cas, tree, (empty,), (_mutation("private-name", after=after),), (after,))
    root_tree = _tree(actual_cas, fmt, (("100644", b"a", old),))
    _reject(actual_cas, root_tree, (old,), (_mutation("a/f", after=after),), (after,))


@pytest.mark.parametrize(
    "platform,path",
    [
        ("posix", "a//b"),
        ("posix", "../f"),
        ("posix", ".git/f"),
        ("posix", ".env.private"),
        ("windows", "CON"),
        ("windows", "f:ads"),
        ("windows", "f."),
    ],
)
def test_platform_and_original_mutation_path_guards(actual_cas, platform, path):
    root = _tree(actual_cas, "sha1")
    blob = _persist(actual_cas, "sha1")
    mutation = _mutation(path, after=blob)
    _reject(actual_cas, root, (), (mutation,), (blob,), platform=platform)


@pytest.mark.parametrize("case", ["files", "directory_alias", "unordered", "duplicate"])
def test_windows_comparison_keys_include_all_directories_and_mutations(actual_cas, case):
    blob = _persist(actual_cas, "sha1")
    root = _tree(actual_cas, "sha1")
    paths = {
        "files": ("A", "a"),
        "directory_alias": ("Dir/f", "dir/g"),
        "unordered": ("z", "a"),
        "duplicate": ("f", "f"),
    }[case]
    _reject(
        actual_cas,
        root,
        (),
        tuple(_mutation(path, after=blob) for path in paths),
        (blob,),
        platform="windows",
    )


@pytest.mark.parametrize(
    "case",
    [
        "mutation_list",
        "mutation_dict",
        "before_dict",
        "size_bool",
        "mode_bool",
        "size_over",
        "extra",
        "missing",
        "no_effect",
        "after_list",
        "limit_bool",
        "reference_bool",
        "cas_fake",
        "platform_fake",
    ],
)
def test_strict_actual_models_and_frozen_bypass_revalidate(actual_cas, case):
    root, old, after, mutation = _simple(actual_cas, "sha1")
    mutations, refs, kwargs = (mutation,), (after,), {}
    if case == "mutation_list":
        mutations = [mutation]
    elif case == "mutation_dict":
        mutations = (mutation.model_dump(),)
    elif case == "before_dict":
        object.__setattr__(mutation, "before", mutation.before.model_dump())
    elif case in {"size_bool", "mode_bool", "size_over"}:
        object.__setattr__(
            mutation.after,
            "size" if case != "mode_bool" else "mode",
            MAX_TRANSACTION_FILE_BYTES + 1 if case == "size_over" else True,
        )
    elif case == "extra":
        vars(mutation)["unknown"] = "private-body"
    elif case == "missing":
        del vars(mutation)["before"]
    elif case == "no_effect":
        object.__setattr__(mutation, "after", mutation.before)
    elif case == "after_list":
        refs = [after]
    elif case == "limit_bool":
        limits = _limits()
        object.__setattr__(limits, "max_objects", True)
        kwargs["limits"] = limits
    elif case == "reference_bool":
        object.__setattr__(after, "body_bytes", True)
    elif case == "cas_fake":
        with pytest.raises(KernelError):
            _project(object(), root, (old,), mutations, refs)
        return
    else:
        kwargs["platform"] = False
    _reject(actual_cas, root, (old,), mutations, refs, **kwargs)


@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize(
    "exception", [RuntimeError, TurnCancelled, asyncio.CancelledError, KernelError]
)
@pytest.mark.parametrize("phase", ["first", "middle", "last"])
def test_checkpoint_exception_is_same_object_at_all_phases(actual_cas, fmt, exception, phase):
    root, old, after, mutation = _simple(actual_cas, fmt)
    calls = []
    _project(actual_cas, root, (old,), (mutation,), (after,), checkpoint=lambda: calls.append(None))
    target = {"first": 1, "middle": len(calls) // 2, "last": len(calls)}[phase]
    failure = (
        KernelError("git_test_cancel", "synthetic") if exception is KernelError else exception()
    )
    count = 0

    def cancel():
        nonlocal count
        count += 1
        if count == target:
            raise failure

    before = _snapshot(actual_cas.store)
    with pytest.raises(exception) as rejected:
        _project(actual_cas, root, (old,), (mutation,), (after,), checkpoint=cancel)
    assert rejected.value is failure and _snapshot(actual_cas.store) == before


@pytest.mark.parametrize("fmt", _FORMATS)
def test_encoding_body_limit_preflight_before_factory_builds_oversized_tree(
    actual_cas, fmt, monkeypatch
):
    blob = _persist(actual_cas, fmt, body=b"x")
    width = 20 if fmt == "sha1" else 32
    record_size = 4000 + 8 + width
    count = MAX_TRANSACTION_FILE_BYTES // record_size
    names = [f"f{i:06d}".encode().ljust(4000, b"x") for i in range(count)]
    remaining = MAX_TRANSACTION_FILE_BYTES - count * record_size
    if remaining <= 8 + width:
        names.pop()
        remaining += record_size
    names.append(b"z" * (remaining - 8 - width))
    root = _tree(actual_cas, fmt, tuple(("100644", name, blob) for name in names))
    assert root.body_bytes == MAX_TRANSACTION_FILE_BYTES
    exact = _project(
        actual_cas,
        root,
        (blob,),
        (_mutation(names[0].decode(), blob, blob, after_mode=493),),
        (blob,),
    )
    assert exact.root.body_bytes == MAX_TRANSACTION_FILE_BYTES
    calls = []
    original = GitObjectMaterial.from_body
    monkeypatch.setattr(
        GitObjectMaterial,
        "from_body",
        staticmethod(lambda *args: calls.append(None) or original(*args)),
    )
    error = _reject(actual_cas, root, (blob,), (_mutation("zzzz", after=blob),), (blob,))
    assert error.code == "git_tree_projection_limit" and calls == []


def _git(repo, *args, body=None):
    git = shutil.which("git")
    assert git is not None
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(repo.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
    }
    result = subprocess.run(
        [git, "--git-dir=" + str(repo / ".git"), *args],
        input=body,
        capture_output=True,
        check=True,
        env=env,
    )
    return result.stdout


@pytest.mark.parametrize("fmt", _FORMATS)
def test_actual_git_253_mktree_differential_complete_root_and_canonical_order(
    actual_cas, tmp_path, fmt
):
    git = shutil.which("git")
    assert subprocess.check_output([git, "--version"]).strip() == b"git version 2.53.0"
    repo = tmp_path / "fixture-repo"
    subprocess.run(
        [git, "init", "--quiet", "--object-format=" + fmt, str(repo)],
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
    )
    old = _persist(actual_cas, fmt, body=b"old")
    new = _persist(actual_cas, fmt, body=b"\0\xff\n")
    empty = _tree(actual_cas, fmt)
    child = _tree(actual_cas, fmt, (("100644", b"f", old),))
    entries = (("40000", b"a", child), ("100644", b"a.c", old), ("40000", b"empty", empty))
    root = _tree(actual_cas, fmt, entries)
    mutations = (
        _mutation("a.c", old, new),
        _mutation("a/f", old, new, after_mode=493),
        _mutation("文.bin", after=new),
    )
    result = _project(actual_cas, root, (child, old, empty), mutations, (new,))
    for ref in (old, new):
        body = actual_cas.read(ref).body
        written = _git(repo, "hash-object", "--no-filters", "-w", "--stdin", body=body)
        assert written.strip().decode() == ref.object_id
    empty_oid = _git(repo, "mktree", "-z", body=b"").strip()
    child_oid = _git(
        repo, "mktree", "-z", body=b"100755 blob " + new.object_id.encode() + b"\tf\0"
    ).strip()
    records = b"".join(
        mode + b" " + kind + b" " + oid + b"\t" + name + b"\0"
        for mode, kind, oid, name in (
            (b"40000", b"tree", child_oid, b"a"),
            (b"100644", b"blob", new.object_id.encode(), b"a.c"),
            (b"40000", b"tree", empty_oid, b"empty"),
            (b"100644", b"blob", new.object_id.encode(), "文.bin".encode()),
        )
    )
    oid = _git(repo, "mktree", "-z", body=records).strip().decode()
    assert result.root.object_id == oid and _git(repo, "cat-file", "tree", oid) == result.root.body
    for material in result.new_trees:
        assert _git(repo, "cat-file", "tree", material.object_id) == material.body


@pytest.mark.parametrize("case", ["path", "presence", "size", "mode", "construct"])
def test_bypassed_model_fields_reject_scalar_subclasses_and_unvalidated_models(actual_cas, case):
    root, old, after, mutation = _simple(actual_cas, "sha1")
    if case == "construct":
        mutation = WorkspaceMutation.model_construct(path="private-name", after=mutation.after)
    else:
        field = "path" if case == "path" else case
        target = mutation if case == "path" else mutation.before
        scalar = getattr(target, field)
        subtype = type("NonActualScalar", (type(scalar),), {})
        object.__setattr__(target, field, subtype(scalar))
    _reject(actual_cas, root, (old,), (mutation,), (after,))


@pytest.mark.parametrize("fmt", _FORMATS)
def test_iterative_projection_127_directory_depth_at_original_path_boundary(actual_cas, fmt):
    root = _tree(actual_cas, fmt)
    blob = _persist(actual_cas, fmt)
    mutation = _mutation("/".join(["d"] * 127 + ["f"]), after=blob)
    result = _project(actual_cas, root, (), (mutation,), (blob,))
    assert result.tree_depth == 127 and result.expanded_entries == 128
    _reject(actual_cas, root, (), (mutation,), (blob,), limits=_limits(4096, 65536, 4096, 126))
