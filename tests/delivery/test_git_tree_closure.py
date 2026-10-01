"""原 SQLite CAS 完整树的只读验真；不造哈希固定点循环、不冒称业务认证。"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import FrozenInstanceError, replace

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import MAX_TRANSACTION_FILE_BYTES
from harnessix.delivery.git_material_cas import GitMaterialCAS, GitObjectMaterialReference
from harnessix.delivery.git_object_references import parse_git_tree
from harnessix.delivery.git_tree_closure import (
    GitTreeClosure,
    GitTreeClosureLimits,
    GitTreeFile,
    verify_git_tree_closure,
)
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from tests.delivery.test_git_material_cas import _material, _reference
from tests.delivery.test_git_object_references import _commit_body

_FORMATS = ("sha1", "sha256")
_PLATFORMS = ("posix", "windows")
_LIMIT_FIELDS = ("max_objects", "max_body_bytes", "max_entries", "max_depth")
_PRIVATE = b"synthetic-private-body\0\xff"


def _checkpoint():
    return None


@pytest.fixture
def actual_cas(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "actual-state") as store:
        yield GitMaterialCAS(store)


def _persist(cas, object_format, kind="blob", body=_PRIVATE):
    return cas.persist(_material(kind, object_format, body))


def _tree(cas, object_format, entries=()):
    ordered = sorted(entries, key=lambda row: row[1] + (b"/" if row[0] == "40000" else b"\0"))
    body = b"".join(
        mode.encode() + b" " + name + b"\0" + bytes.fromhex(child.object_id)
        for mode, name, child in ordered
    )
    return _persist(cas, object_format, "tree", body)


def _limits(objects=64, body_bytes=32 * 1024 * 1024, entries=128, depth=8):
    return GitTreeClosureLimits(objects, body_bytes, entries, depth)


def _verify(cas, root, catalog=(), *, platform="posix", limits=None, checkpoint=_checkpoint):
    return verify_git_tree_closure(
        cas,
        root,
        catalog,
        platform=platform,
        limits=limits if limits is not None else _limits(),
        checkpoint=checkpoint,
    )


def _snapshot(store):
    # 比较真实完整字节与文件身份，不把只读打开造成的 atime 变化误当正文写入。
    return tuple(
        (
            path.relative_to(store._root).as_posix(),
            path.read_bytes(),
            path.stat().st_ino,
            path.stat().st_mtime_ns,
            path.stat().st_mode,
        )
        for path in sorted(store._root.rglob("*"))
        if path.is_file()
    )


def _read_counts(monkeypatch):
    counts = Counter()
    original = GitMaterialCAS.read

    def observed(cas, reference):
        counts[reference.object_id] += 1
        return original(cas, reference)

    monkeypatch.setattr(GitMaterialCAS, "read", observed)
    return counts


def _reject(cas, root, catalog=(), *, code=None, **kwargs):
    before = _snapshot(cas.store)
    with pytest.raises(KernelError) as rejected:
        _verify(cas, root, catalog, **kwargs)
    if code:
        assert rejected.value.code == code
    assert _PRIVATE.decode("latin1") not in str(rejected.value)
    assert rejected.value.__cause__ is None
    assert _snapshot(cas.store) == before
    return rejected.value


def _shared(cas, object_format):
    blob = _persist(cas, object_format)
    child = _tree(cas, object_format, (("100644", b"f", blob),))
    root = _tree(cas, object_format, (("40000", b"a", child), ("40000", b"b", child)))
    return root, child, blob


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("platform", _PLATFORMS)
@pytest.mark.parametrize("include_root", [False, True])
def test_empty_tree_zero_bytes_entries_and_depth_with_one_root_object(
    actual_cas, object_format, platform, include_root
):
    root = _tree(actual_cas, object_format)
    before = _snapshot(actual_cas.store)
    closure = _verify(
        actual_cas,
        root,
        (root,) if include_root else (),
        platform=platform,
        limits=_limits(1, 0, 0, 0),
    )
    assert type(closure) is GitTreeClosure
    assert closure.root == root and closure.objects == (root,) and closure.files == ()
    assert (closure.body_bytes, closure.expanded_entries, closure.tree_depth) == (0, 0, 0)
    assert _snapshot(actual_cas.store) == before
    with pytest.raises(FrozenInstanceError):
        closure.files = ()
    _reject(actual_cas, root, limits=_limits(0, 0, 0, 0), code="git_tree_closure_limit")


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("platform", _PLATFORMS)
def test_multilevel_modes_empty_and_binary_files_sorted_complete_and_unique(
    actual_cas, object_format, platform
):
    empty = _persist(actual_cas, object_format, body=b"")
    binary = _persist(actual_cas, object_format)
    executable = _persist(actual_cas, object_format, body=b"synthetic-executable")
    leaf = _tree(
        actual_cas, object_format, (("100644", b"empty", empty), ("100755", b"run", executable))
    )
    middle = _tree(actual_cas, object_format, (("40000", b"d", leaf),))
    root = _tree(
        actual_cas,
        object_format,
        (
            ("40000", b"nested", middle),
            ("100644", b"top.bin", binary),
            ("100755", b"z-run", executable),
        ),
    )
    objects = (root, middle, leaf, empty, binary, executable)
    before = _snapshot(actual_cas.store)
    closure = _verify(
        actual_cas,
        root,
        tuple(reversed(objects)),
        platform=platform,
        limits=_limits(6, sum(obj.body_bytes for obj in objects), 6, 2),
    )
    assert closure.objects == tuple(sorted(objects, key=lambda obj: obj.object_id))
    assert [(item.path, item.mode, item.material) for item in closure.files] == [
        ("nested/d/empty", "100644", empty),
        ("nested/d/run", "100755", executable),
        ("top.bin", "100644", binary),
        ("z-run", "100755", executable),
    ]
    assert (closure.expanded_entries, closure.tree_depth) == (6, 2)
    assert closure.body_bytes == sum(obj.body_bytes for obj in objects)
    assert all(type(item) is GitTreeFile for item in closure.files)
    assert "path=" not in repr(closure.files[0]) and "files=" not in repr(closure)
    with pytest.raises(FrozenInstanceError):
        closure.files[0].mode = "100755"
    assert _snapshot(actual_cas.store) == before


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("platform", _PLATFORMS)
def test_same_subtree_expands_each_path_but_reads_and_counts_each_object_once(
    actual_cas, monkeypatch, object_format, platform
):
    root, child, blob = _shared(actual_cas, object_format)
    counts = _read_counts(monkeypatch)
    closure = _verify(
        actual_cas,
        root,
        (blob, child),
        platform=platform,
        limits=_limits(3, sum(obj.body_bytes for obj in (root, child, blob)), 4, 1),
    )
    assert [item.path for item in closure.files] == ["a/f", "b/f"]
    assert closure.files[0].material == closure.files[1].material == blob
    assert counts == Counter({obj.object_id: 1 for obj in (root, child, blob)})
    assert len(closure.objects) == 3 and closure.expanded_entries == 4
    assert closure.tree_depth == 1
    assert closure.body_bytes == sum(obj.body_bytes for obj in (root, child, blob))


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("include_root", [False, True])
def test_orphan_catalog_is_validated_and_budgeted_but_never_read_or_returned(
    actual_cas, monkeypatch, object_format, include_root
):
    root = _tree(actual_cas, object_format)
    orphan = _reference(_material("blob", object_format, b"not-stored-orphan"))
    catalog = (root, orphan) if include_root else (orphan,)
    counts = _read_counts(monkeypatch)
    _reject(actual_cas, root, catalog, limits=_limits(1), code="git_tree_closure_limit")
    assert not counts
    closure = _verify(actual_cas, root, catalog, limits=_limits(2, 0, 0, 0))
    assert closure.objects == (root,) and closure.body_bytes == 0
    assert counts == Counter({root.object_id: 1})


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("size", [0, MAX_TRANSACTION_FILE_BYTES], ids=["empty", "8MiB"])
def test_readonly_reopen_complete_binary_leaf_byte_snapshot_and_no_new_rows(
    actual_cas, tmp_path, monkeypatch, object_format, size
):
    body = (bytes(range(256)) * (size // 256)) if size else b""
    blob = _persist(actual_cas, object_format, body=body)
    root = _tree(actual_cas, object_format, (("100644", b"payload.bin", blob),))
    state = actual_cas.store._root
    actual_cas.store.close()
    with SQLiteWorkspaceTransactionStore(state, read_only=True) as store:
        readonly = GitMaterialCAS(store)
        # SQLite 的首次元数据查询可建立 WAL/SHM；在验真观察区间前完成夹具查询。
        rows = tuple(store._db.iterdump())
        before = _snapshot(store)
        changes = store._db.total_changes

        def deny_write(*args, **kwargs):
            raise AssertionError("闭包验真不得写原CAS")

        monkeypatch.setattr(SQLiteWorkspaceTransactionStore, "put_blob", deny_write)
        closure = _verify(readonly, root, (blob,), limits=_limits(2, root.body_bytes + size, 1, 0))
        assert closure.files == (GitTreeFile("payload.bin", "100644", blob),)
        assert readonly.read(closure.files[0].material).body == body
        assert _snapshot(store) == before
        assert store._db.total_changes == changes
        assert tuple(store._db.iterdump()) == rows
        assert closure.body_bytes == root.body_bytes + size


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("field", _LIMIT_FIELDS)
def test_each_exact_closure_limit_and_one_less_rejects_whole_tree(actual_cas, object_format, field):
    objects = _shared(actual_cas, object_format)
    root = objects[0]
    limits = _limits(3, sum(obj.body_bytes for obj in objects), 4, 1)
    assert len(_verify(actual_cas, root, objects, limits=limits).files) == 2
    below = replace(limits, **{field: getattr(limits, field) - 1})
    _reject(actual_cas, root, objects, limits=below, code="git_tree_closure_limit")


@pytest.mark.parametrize("object_format", _FORMATS)
def test_depth_limit_is_checked_before_reading_child_tree(actual_cas, monkeypatch, object_format):
    child = _reference(_material("tree", object_format, b""))
    root = _tree(actual_cas, object_format, (("40000", b"child", child),))
    counts = _read_counts(monkeypatch)
    _reject(actual_cas, root, (child,), limits=_limits(depth=0), code="git_tree_closure_limit")
    assert counts == Counter({root.object_id: 1})


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("kind", ["tree", "blob"])
def test_missing_catalog_object_even_unchanged_file_prevents_any_partial_result(
    actual_cas, object_format, kind
):
    child = _persist(actual_cas, object_format, kind, b"" if kind == "tree" else _PRIVATE)
    root = _tree(
        actual_cas,
        object_format,
        (("40000" if kind == "tree" else "100644", b"unchanged.txt", child),),
    )
    _reject(actual_cas, root, code="git_tree_closure_missing")


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("fault", ["missing-root", "missing-blob", "corrupt-blob"])
def test_actual_cas_missing_and_corrupt_body_remain_detected_without_any_write(
    actual_cas, object_format, fault
):
    blob = _persist(actual_cas, object_format)
    root = _tree(actual_cas, object_format, (("100644", b"f", blob),))
    affected = root if fault == "missing-root" else blob
    path = actual_cas.store._root / "blobs" / affected.cas_digest
    if fault == "corrupt-blob":
        path.write_bytes(b"corrupt-fixture-only")
    else:
        path.unlink()
    _reject(actual_cas, root, (blob,), code="git_material_cas_read_failed")


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("mode", ["40000", "100644"])
def test_same_format_wrong_child_type_is_not_authenticated_by_plain_cas_sha(
    actual_cas, object_format, mode
):
    child = _persist(actual_cas, object_format, "blob" if mode == "40000" else "tree", b"")
    root = _tree(actual_cas, object_format, ((mode, b"wrong-type", child),))
    _reject(actual_cas, root, (child,), code="git_tree_closure_type_mismatch")


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize(("mode", "kind"), [("120000", "blob"), ("160000", "commit")])
def test_parser_accepts_link_modes_but_complete_file_closure_rejects_them(
    actual_cas, monkeypatch, object_format, mode, kind
):
    body = _PRIVATE if kind == "blob" else _commit_body(object_format)
    child = _persist(actual_cas, object_format, kind, body)
    root = _tree(actual_cas, object_format, ((mode, b"unsupported", child),))
    assert (
        parse_git_tree(actual_cas.read(root), max_entries=1, checkpoint=_checkpoint)[0].mode == mode
    )
    counts = _read_counts(monkeypatch)
    _reject(actual_cas, root, (child,), code="git_tree_closure_unsupported")
    assert counts == Counter({root.object_id: 1})


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("platform", _PLATFORMS)
@pytest.mark.parametrize("name", [b"raw-\xff", b"line\nname", b"tab\tname", b"a\\b", b"C:drive"])
def test_parser_raw_names_are_not_sufficient_for_workspace_paths(
    actual_cas, object_format, platform, name
):
    blob = _persist(actual_cas, object_format)
    root = _tree(actual_cas, object_format, (("100644", name, blob),))
    assert (
        parse_git_tree(actual_cas.read(root), max_entries=1, checkpoint=_checkpoint)[0].name == name
    )
    _reject(actual_cas, root, (blob,), platform=platform, code="git_tree_closure_path_denied")


@pytest.mark.parametrize(
    "name",
    [
        b"CON",
        b"NUL.txt",
        "COM¹.log".encode(),
        b"file:stream",
        b"trailing.",
        b"trailing ",
        b"x" * 256,
    ],
)
def test_windows_reserved_ads_folded_and_overlong_segments_are_rejected(actual_cas, name):
    blob = _persist(actual_cas, "sha1")
    root = _tree(actual_cas, "sha1", (("100644", name, blob),))
    _reject(actual_cas, root, (blob,), platform="windows", code="git_tree_closure_path_denied")


@pytest.mark.parametrize("scenario", ["files", "directories", "directory-file", "unicode"])
def test_windows_comparison_keys_include_every_directory_and_file(actual_cas, scenario):
    blob = _persist(actual_cas, "sha1")
    child = _tree(actual_cas, "sha1", (("100644", b"foo", blob),))
    rows = {
        "files": (("100644", b"Name", blob), ("100644", b"name", blob)),
        "directories": (("40000", b"Dir", child), ("40000", b"dir", child)),
        "directory-file": (("40000", b"Dir", child), ("100644", b"dir", blob)),
        "unicode": (("100644", "Straße".encode(), blob), ("100644", b"STRASSE", blob)),
    }
    root = _tree(actual_cas, "sha1", rows[scenario])
    catalog = (blob, child)
    assert len(_verify(actual_cas, root, catalog, platform="posix").files) == 2
    _reject(actual_cas, root, catalog, platform="windows", code="git_tree_closure_path_denied")


@pytest.mark.parametrize("platform", _PLATFORMS)
def test_nonascii_paths_and_modes_are_sorted_by_utf8_bytes(actual_cas, platform):
    blob = _persist(actual_cas, "sha1")
    names = ("é", "z", "a")
    root = _tree(actual_cas, "sha1", tuple(("100755", name.encode(), blob) for name in names))
    closure = _verify(actual_cas, root, (blob,), platform=platform)
    assert [item.path for item in closure.files] == ["a", "z", "é"]


@pytest.mark.parametrize("field", _LIMIT_FIELDS)
@pytest.mark.parametrize("value", [True, False, -1, 1.0, "1", None])
def test_limits_require_four_actual_nonnegative_ints(field, value):
    with pytest.raises(KernelError) as rejected:
        replace(_limits(), **{field: value})
    assert rejected.value.code == "git_tree_closure_limit_invalid"


def test_limits_have_no_defaults_or_int_subclass_coercion():
    with pytest.raises(TypeError):
        GitTreeClosureLimits()

    class Limit(int):
        pass

    with pytest.raises(KernelError):
        GitTreeClosureLimits(Limit(1), 0, 0, 0)


@pytest.mark.parametrize("field", _LIMIT_FIELDS)
@pytest.mark.parametrize("value", [True, -1, None])
def test_frozen_limits_mutation_is_revalidated(actual_cas, field, value):
    root = _tree(actual_cas, "sha1")
    limits = _limits()
    object.__setattr__(limits, field, value)
    _reject(actual_cas, root, limits=limits, code="git_tree_closure_limit_invalid")


@pytest.mark.parametrize("field", _LIMIT_FIELDS)
def test_frozen_limits_missing_attribute_is_fixed_error_not_native_attribute_error(
    actual_cas, field
):
    root = _tree(actual_cas, "sha1")
    limits = _limits()
    object.__delattr__(limits, field)
    _reject(actual_cas, root, limits=limits)


@pytest.mark.parametrize("value", [None, True, (), []])
def test_limits_require_exact_class(actual_cas, value):
    root = _tree(actual_cas, "sha1")
    with pytest.raises(KernelError):
        verify_git_tree_closure(
            actual_cas, root, (), platform="posix", limits=value, checkpoint=_checkpoint
        )


@pytest.mark.parametrize("value", [None, [], {}, True])
def test_catalog_requires_exact_tuple(actual_cas, value):
    root = _tree(actual_cas, "sha1")
    _reject(actual_cas, root, value, code="git_tree_closure_invalid")


def test_catalog_duplicate_mismatched_root_and_other_format_reject(actual_cas):
    root = _tree(actual_cas, "sha1")
    other = _persist(actual_cas, "sha256")
    altered = replace(root, object_type="blob")
    for catalog in ((root, root), (altered,), (other,), (None,)):
        _reject(actual_cas, root, catalog, code="git_tree_closure_invalid")
    blob = _persist(actual_cas, "sha1")
    _reject(actual_cas, blob, code="git_tree_closure_type_mismatch")


@pytest.mark.parametrize("where", ["root", "catalog"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "other"),
        ("object_type", True),
        ("object_id", False),
        ("object_format", "SHA1"),
        ("body_bytes", True),
        ("body_sha256", "A" * 64),
        ("cas_digest", "0" * 64),
    ],
)
def test_root_and_catalog_reference_frozen_bypass_is_revalidated(actual_cas, where, field, value):
    root = _tree(actual_cas, "sha1")
    victim = root if where == "root" else _persist(actual_cas, "sha1")
    object.__setattr__(victim, field, value)
    _reject(
        actual_cas, root, (victim,) if where == "catalog" else (), code="git_tree_closure_invalid"
    )


def test_exact_cas_reference_limits_and_catalog_classes_without_duck_coercion(actual_cas):
    root = _tree(actual_cas, "sha1")

    class CAS(GitMaterialCAS):
        pass

    class Reference(GitObjectMaterialReference):
        pass

    class Limits(GitTreeClosureLimits):
        pass

    class Catalog(tuple):
        pass

    _reject(CAS(actual_cas.store), root)
    _reject(actual_cas, Reference(**root.binding()))
    _reject(actual_cas, root, limits=Limits(1, 0, 0, 0))
    _reject(actual_cas, root, Catalog((root,)))
    _reject(actual_cas, object.__new__(GitObjectMaterialReference))


@pytest.mark.parametrize("platform", [True, "POSIX", "linux", b"posix", None])
def test_platform_requires_exact_supported_string(actual_cas, platform):
    root = _tree(actual_cas, "sha1")
    _reject(actual_cas, root, platform=platform, code="git_tree_closure_path_denied")


@pytest.mark.parametrize("error_type", [TurnCancelled, asyncio.CancelledError, RuntimeError])
@pytest.mark.parametrize("phase", ["entry", "middle", "last"])
def test_callback_cancellation_or_unknown_error_is_not_swallowed_or_partial(
    actual_cas, error_type, phase
):
    objects = _shared(actual_cas, "sha1")
    calls = []
    _verify(actual_cas, objects[0], objects, checkpoint=lambda: calls.append(None))
    target = {"entry": 1, "middle": len(calls) // 2, "last": len(calls)}[phase]
    error = error_type("synthetic-checkpoint-only")
    count = 0
    before = _snapshot(actual_cas.store)

    def check():
        nonlocal count
        count += 1
        if count == target:
            raise error

    with pytest.raises(error_type) as caught:
        _verify(actual_cas, objects[0], objects, checkpoint=check)
    assert caught.value is error and count == target
    assert _snapshot(actual_cas.store) == before
