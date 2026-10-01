"""真实SQLite CAS的完整树与Diff同源规划；无写入、事务伪造或业务批准。"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import FrozenInstanceError, replace

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery import git_tree_diff as coordinator
from harnessix.delivery.contracts import MAX_TRANSACTION_FILE_BYTES
from harnessix.delivery.diff_content import WorkspaceDiffContent
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.git_object_references import parse_git_tree
from harnessix.delivery.git_tree_diff import GitTreeDiff, prepare_git_tree_diff
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from tests.delivery.test_diff_content import MARKER, IntSubclass
from tests.delivery.test_git_tree_closure import _checkpoint, _limits, _persist, _snapshot, _tree
from tests.delivery.test_git_tree_projection import _mutation
from tests.delivery.test_git_tree_projection import actual_cas as actual_cas

_FORMATS = ("sha1", "sha256")
_MAX_DIFF = 64 * 1024 * 1024


def _prepare(
    cas,
    root,
    catalog,
    mutations,
    after=(),
    *,
    platform="posix",
    limits=None,
    max_diff_bytes=_MAX_DIFF,
    checkpoint=_checkpoint,
):
    return prepare_git_tree_diff(
        cas,
        root,
        catalog,
        mutations,
        after,
        platform=platform,
        limits=_limits(4096, 64 * 1024 * 1024, 4096, 128) if limits is None else limits,
        checkpoint=checkpoint,
        max_diff_bytes=max_diff_bytes,
    )


def _observe(store):
    # 先启动元数据只读观察，再冻结所有DB/CAS文件；不把首次WAL/SHM打开误当写入。
    rows = tuple(store._db.iterdump())
    return rows, store._db.total_changes, _snapshot(store)


def _reject(cas, root, catalog, mutations, after=(), *, code=None, **kwargs):
    before = _observe(cas.store)
    with pytest.raises(KernelError) as error:
        _prepare(cas, root, catalog, mutations, after, **kwargs)
    if code is not None:
        assert error.value.code == code
    assert "private-name" not in str(error.value) and "private-body" not in str(error.value)
    assert error.value.__cause__ is None
    assert _observe(cas.store) == before
    return error.value


def _simple(cas, fmt):
    old = _persist(cas, fmt, body=b"old-private-body\n")
    new = _persist(cas, fmt, body=b"new-private-body\n")
    keep = _persist(cas, fmt, body=b"unchanged\n")
    root = _tree(cas, fmt, (("100644", b"private-name", old), ("100644", b"untouched", keep)))
    return root, (old, keep), _mutation("private-name", old, new), (new,)


@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_complete_target_and_diff_share_exact_sources_modes_and_untouched_members(
    actual_cas, fmt, platform
):
    old = _persist(actual_cas, fmt, body="旧行🙂\n".encode())
    new = _persist(actual_cas, fmt, body="新文中文🙂".encode())
    binary = _persist(actual_cas, fmt, body=b"\0\xff")
    empty_blob = _persist(actual_cas, fmt, body=b"")
    keep = _persist(actual_cas, fmt, body=b"untouched\n")
    empty_tree = _tree(actual_cas, fmt)
    child = _tree(actual_cas, fmt, (("100644", b"remove", binary),))
    root = _tree(
        actual_cas,
        fmt,
        (
            ("40000", b"empty-dir", empty_tree),
            ("100755", b"keep", keep),
            ("40000", b"nested", child),
            ("100644", b"update", old),
        ),
    )
    mutations = (
        _mutation("add-empty", after=empty_blob),
        _mutation("nested/remove", binary),
        _mutation("update", old, new, after_mode=493),
    )
    before = _observe(actual_cas.store)
    result = _prepare(
        actual_cas,
        root,
        (old, binary, keep, empty_tree, child),
        mutations,
        (new, empty_blob),
        platform=platform,
    )
    assert type(result) is GitTreeDiff and type(result.content) is WorkspaceDiffContent
    assert result.spec_version == "harnessix.git-tree-diff/v1"
    assert [(f.path, f.mode, f.material) for f in result.projection.files] == [
        ("add-empty", "100644", empty_blob),
        ("keep", "100755", keep),
        ("update", "100755", new),
    ]
    assert [
        e.name
        for e in parse_git_tree(result.projection.root, max_entries=8, checkpoint=_checkpoint)
    ] == [
        b"add-empty",
        b"empty-dir",
        b"keep",
        b"update",
    ]
    assert [(e.path, e.kind) for e in result.content.entries] == [
        ("add-empty", "added"),
        ("nested/remove", "deleted"),
        ("update", "modified"),
    ]
    changed = next(e for e in result.content.entries if e.path == "update")
    assert changed.before == mutations[-1].before and changed.after == mutations[-1].after
    assert "old mode 644\nnew mode 755\n" in result.content.text
    assert "+新文中文🙂\n" + MARKER in result.content.text
    assert "untouched" not in result.content.text and "Binary files differ" in result.content.text
    assert result.content.utf8_bytes == len(result.content.text.encode())
    assert result.content.sha256 == hashlib.sha256(result.content.text.encode()).hexdigest()
    assert "projection=" not in repr(result) and "content=" not in repr(result)
    assert not hasattr(result, "transaction_id") and not hasattr(result, "approval")
    with pytest.raises(FrozenInstanceError):
        result.content = None
    assert _observe(actual_cas.store) == before


@pytest.mark.parametrize("fmt", _FORMATS)
def test_unique_rename_and_mode_only_use_complete_blob_not_git_execution(actual_cas, fmt):
    blob = _persist(actual_cas, fmt, body=b"same\0\xff")
    root = _tree(actual_cas, fmt, (("100644", b"old", blob), ("100644", b"run", blob)))
    mutations = (
        _mutation("new", after=blob),
        _mutation("old", blob),
        _mutation("run", blob, blob, after_mode=493),
    )
    before = _observe(actual_cas.store)
    result = _prepare(actual_cas, root, (blob,), mutations, (blob,))
    assert [(e.kind, e.original_path, e.path) for e in result.content.entries] == [
        ("renamed", "old", "new"),
        ("modified", None, "run"),
    ]
    assert "similarity index 100%" in result.content.text
    assert all(e.binary for e in result.content.entries)
    assert [(f.path, f.mode) for f in result.projection.files] == [
        ("new", "100644"),
        ("run", "100755"),
    ]
    assert _observe(actual_cas.store) == before


@pytest.mark.parametrize("fmt", _FORMATS)
def test_net_mutation_uses_first_before_last_after_without_intermediate_authority(actual_cas, fmt):
    first = _persist(actual_cas, fmt, body=b"first\n")
    middle = _persist(actual_cas, fmt, body=b"intermediate\n")
    last = _persist(actual_cas, fmt, body=b"last\n")
    root = _tree(actual_cas, fmt, (("100644", b"f", first),))
    mutation = _mutation("f", first, last)
    result = _prepare(actual_cas, root, (first,), (mutation,), (last,))
    assert result.projection.files[0].material == last
    assert "-first\n+last\n" in result.content.text and "intermediate" not in result.content.text
    assert result.content.entries[0].before.sha256 == first.body_sha256
    _reject(actual_cas, root, (first,), (mutation,), (last, middle))


@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize(
    "size", [0, 1, MAX_TRANSACTION_FILE_BYTES], ids=["empty", "binary", "8MiB"]
)
def test_readonly_reopen_complete_binary_material_and_all_state_bytes(
    tmp_path, monkeypatch, fmt, size
):
    state = tmp_path / "state"
    body = (bytes(range(256)) * ((size + 255) // 256))[:size]
    with SQLiteWorkspaceTransactionStore(state) as store:
        cas = GitMaterialCAS(store)
        root = _tree(cas, fmt)
        blob = _persist(cas, fmt, body=body)
    with SQLiteWorkspaceTransactionStore(state, read_only=True) as store:
        cas = GitMaterialCAS(store)
        before = _observe(store)

        def forbidden(*args, **kwargs):
            pytest.fail("完整树与Diff规划不能写CAS或事务")

        monkeypatch.setattr(GitMaterialCAS, "persist", forbidden)
        for name in ("put_blob", "_put_blob", "save", "transition"):
            monkeypatch.setattr(SQLiteWorkspaceTransactionStore, name, forbidden)
        result = _prepare(cas, root, (), (_mutation("full.bin", after=blob),), (blob,))
        assert cas.read(result.projection.files[0].material).body == body
        (entry,) = result.content.entries
        assert entry.after.size == size and entry.after.sha256 == hashlib.sha256(body).hexdigest()
        assert entry.binary is (size > 0)
        assert result.projection.root.body.endswith(bytes.fromhex(blob.object_id))
        assert _observe(store) == before


@pytest.mark.parametrize("fmt", _FORMATS)
def test_diff_utf8_exact_one_below_and_one_above_rejects_whole_result(actual_cas, fmt):
    old = _persist(actual_cas, fmt, body="旧🙂\n".encode())
    new = _persist(actual_cas, fmt, body="中文🙂新".encode())
    root = _tree(actual_cas, fmt, (("100644", "文🙂".encode(), old),))
    mutations = (_mutation("文🙂", old, new, after_mode=493),)
    baseline = _prepare(actual_cas, root, (old,), mutations, (new,))
    size = baseline.content.utf8_bytes
    assert size > len(baseline.content.text)
    assert _prepare(actual_cas, root, (old,), mutations, (new,), max_diff_bytes=size) == baseline
    assert (
        _prepare(actual_cas, root, (old,), mutations, (new,), max_diff_bytes=size + 1) == baseline
    )
    _reject(
        actual_cas,
        root,
        (old,),
        mutations,
        (new,),
        max_diff_bytes=size - 1,
        code="git_tree_diff_limit",
    )


@pytest.mark.parametrize(
    "limit", [None, True, False, 0, -1, _MAX_DIFF + 1, 1.0, "1", IntSubclass(1)]
)
def test_invalid_diff_limit_is_fixed_error_before_any_cas_access(monkeypatch, limit):
    def forbidden(*args, **kwargs):
        pytest.fail("无效Diff容量不能读取CAS或构造投影")

    monkeypatch.setattr(GitMaterialCAS, "read", forbidden)
    with pytest.raises(KernelError) as error:
        _prepare(object(), object(), (), (), max_diff_bytes=limit)
    assert error.value.code == "git_tree_diff_limit_invalid"


@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize("field", ["max_objects", "max_body_bytes", "max_entries", "max_depth"])
def test_all_original_tree_budgets_exact_and_one_below(actual_cas, fmt, field):
    blob = _persist(actual_cas, fmt, body=b"\0")
    root = _tree(actual_cas, fmt)
    mutations = (_mutation("nested/f", after=blob),)
    result = _prepare(actual_cas, root, (), mutations, (blob,))
    limits = _limits(4, result.projection.objects_body_bytes, 2, 1)
    assert _prepare(actual_cas, root, (), mutations, (blob,), limits=limits) == result
    _reject(
        actual_cas,
        root,
        (),
        mutations,
        (blob,),
        limits=replace(limits, **{field: getattr(limits, field) - 1}),
    )


@pytest.mark.parametrize("fmt", _FORMATS)
def test_original_256_mutation_boundary_is_not_a_new_default_capacity(actual_cas, fmt):
    blob = _persist(actual_cas, fmt, body=b"\0")
    root = _tree(actual_cas, fmt)
    mutations = tuple(_mutation(f"f{i:03d}", after=blob) for i in range(256))
    result = _prepare(actual_cas, root, (), mutations, (blob,))
    assert len(result.content.entries) == len(result.projection.files) == 256
    _reject(actual_cas, root, (), (*mutations, _mutation("f256", after=blob)), (blob,))


@pytest.mark.parametrize("fmt", _FORMATS)
def test_net_mirror_exact_32_mib_and_plus_one_counts_paths_not_cas_dedup(actual_cas, fmt):
    blob = _persist(actual_cas, fmt, body=b"\0" * MAX_TRANSACTION_FILE_BYTES)
    extra = _persist(actual_cas, fmt, body=b"x")
    root = _tree(actual_cas, fmt)
    mutations = tuple(_mutation(f"f{i}", after=blob) for i in range(4))
    result = _prepare(actual_cas, root, (), mutations, (blob,))
    assert (
        len(result.content.entries) == 4 and result.projection.objects_body_bytes < 9 * 1024 * 1024
    )
    _reject(
        actual_cas,
        root,
        (),
        (*mutations, _mutation("f4", after=extra)),
        (blob, extra),
        code="git_tree_projection_limit",
    )


@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize(
    "field,value", [("sha256", "f" * 64), ("size", 1), ("mode", 493), ("size", True)]
)
def test_exact_before_after_fields_and_frozen_bypass_are_revalidated(
    actual_cas, fmt, side, field, value
):
    root, catalog, mutation, after = _simple(actual_cas, fmt)
    if side == "after" and field == "mode":
        # 新模式是独立合法意图；这里用不合法模式，而不是误拒绝合法chmod。
        value = 0o600
    object.__setattr__(getattr(mutation, side), field, value)
    _reject(actual_cas, root, catalog, (mutation,), after)


@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize("side", ["before", "after", "untouched"])
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_or_corrupt_full_cas_material_never_returns_partial_diff(
    actual_cas, fmt, side, damage
):
    root, catalog, mutation, after = _simple(actual_cas, fmt)
    reference = {"before": catalog[0], "after": after[0], "untouched": catalog[1]}[side]
    path = actual_cas.store._root / "blobs" / reference.cas_digest
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupted-private-body")
    _reject(actual_cas, root, catalog, (mutation,), after, code="git_material_cas_read_failed")


@pytest.mark.parametrize("fmt", _FORMATS)
def test_missing_unchanged_catalog_member_has_priority_over_positive_tiny_diff_budget(
    actual_cas, fmt
):
    root, catalog, mutation, after = _simple(actual_cas, fmt)
    _reject(
        actual_cas,
        root,
        (catalog[0],),
        (mutation,),
        after,
        max_diff_bytes=1,
        code="git_tree_closure_missing",
    )


@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize(
    "attack", ["cas", "root", "catalog-list", "after-list", "mutations-list", "limits"]
)
def test_original_strict_typed_inputs_cannot_be_bypassed_by_diff_wrapper(actual_cas, fmt, attack):
    root, catalog, mutation, after = _simple(actual_cas, fmt)
    arguments = dict(cas=actual_cas, root=root, catalog=catalog, mutations=(mutation,), after=after)
    if attack == "cas":
        arguments["cas"] = object()
    elif attack == "root":
        object.__setattr__(root, "body_bytes", True)
    elif attack == "limits":
        limits = _limits()
        object.__setattr__(limits, "max_entries", True)
        arguments["limits"] = limits
    else:
        name = {"catalog-list": "catalog", "after-list": "after", "mutations-list": "mutations"}[
            attack
        ]
        arguments[name] = list(arguments[name])
    before = _observe(actual_cas.store)
    with pytest.raises(KernelError):
        _prepare(**arguments)
    assert _observe(actual_cas.store) == before


@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize(
    "platform,path",
    [
        ("posix", "../escape"),
        ("posix", ".env/private-name"),
        ("windows", "CON.txt"),
        ("windows", "a:stream"),
    ],
)
def test_platform_protected_or_escaping_paths_are_not_authorized_by_content_marker(
    actual_cas, fmt, platform, path
):
    blob = _persist(actual_cas, fmt, body=(MARKER + "private-body").encode())
    root = _tree(actual_cas, fmt)
    _reject(actual_cas, root, (), (_mutation(path, after=blob),), (blob,), platform=platform)


@pytest.mark.parametrize("fmt", _FORMATS)
def test_windows_comparison_keys_and_final_file_ancestor_conflicts(actual_cas, fmt):
    blob = _persist(actual_cas, fmt, body=b"content\n")
    root = _tree(actual_cas, fmt, (("100644", b"Dir", blob),))
    _reject(
        actual_cas, root, (blob,), (_mutation("dir/f", after=blob),), (blob,), platform="windows"
    )
    _reject(actual_cas, root, (blob,), (_mutation("Dir/f", after=blob),), (blob,))


@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize("side", ["before", "after"])
def test_cas_second_read_after_real_projection_rejects_tampering(
    actual_cas, fmt, side, monkeypatch
):
    root, catalog, mutation, after = _simple(actual_cas, fmt)
    reference = catalog[0] if side == "before" else after[0]
    original = coordinator.prepare_git_tree_projection
    tampered = []

    def mutate_after_full_verification(*args, **kwargs):
        projection = original(*args, **kwargs)
        (actual_cas.store._root / "blobs" / reference.cas_digest).write_bytes(
            b"tampered-private-body"
        )
        tampered.append(_observe(actual_cas.store))
        return projection

    monkeypatch.setattr(coordinator, "prepare_git_tree_projection", mutate_after_full_verification)
    with pytest.raises(KernelError) as error:
        _prepare(actual_cas, root, catalog, (mutation,), after)
    assert error.value.code == "git_material_cas_read_failed" and len(tampered) == 1
    assert _observe(actual_cas.store) == tampered[0]


@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize("attack", ["path", "before-mode", "after-mode"])
def test_same_net_mutation_is_frozen_for_projection_and_diff(actual_cas, fmt, attack, monkeypatch):
    root, catalog, mutation, after = _simple(actual_cas, fmt)
    original = coordinator.prepare_git_tree_projection

    def mutate_caller_alias_after_projection(*args, **kwargs):
        projection = original(*args, **kwargs)
        if attack == "path":
            object.__setattr__(mutation, "path", "different-path")
        else:
            # 内层版本也须快照；只复制外层Mutation仍会形成目标树/Diff分歧。
            version = mutation.before if attack == "before-mode" else mutation.after
            object.__setattr__(version, "mode", 493)
        return projection

    monkeypatch.setattr(
        coordinator, "prepare_git_tree_projection", mutate_caller_alias_after_projection
    )
    before = _observe(actual_cas.store)
    try:
        result = _prepare(actual_cas, root, catalog, (mutation,), after)
    except KernelError:
        pass
    else:
        assert [entry.path for entry in result.content.entries] == ["private-name"]
        assert result.content.entries[0].before.mode == 420
        assert result.content.entries[0].after.mode == 420
        assert "different-path" not in result.content.text
        assert "old mode" not in result.content.text and "new mode" not in result.content.text
        assert [file.path for file in result.projection.files] == ["private-name", "untouched"]
    assert _observe(actual_cas.store) == before


@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize(
    "exception", [RuntimeError, TimeoutError, TurnCancelled, asyncio.CancelledError, KernelError]
)
@pytest.mark.parametrize("phase", ["first", "middle", "last"])
def test_checkpoint_failure_or_timeout_is_same_object_and_no_state_write(
    actual_cas, fmt, exception, phase
):
    root, catalog, mutation, after = _simple(actual_cas, fmt)
    calls = []
    _prepare(actual_cas, root, catalog, (mutation,), after, checkpoint=lambda: calls.append(None))
    target = {"first": 1, "middle": len(calls) // 2, "last": len(calls)}[phase]
    failure = (
        KernelError("git_diff_cancel", "合成取消") if exception is KernelError else exception()
    )
    count = 0

    def cancel():
        nonlocal count
        count += 1
        if count == target:
            raise failure

    before = _observe(actual_cas.store)
    with pytest.raises(exception) as error:
        _prepare(actual_cas, root, catalog, (mutation,), after, checkpoint=cancel)
    assert error.value is failure and _observe(actual_cas.store) == before


@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize(
    "exception", [RuntimeError, TimeoutError, TurnCancelled, asyncio.CancelledError, KernelError]
)
def test_diff_body_reader_exception_after_verified_projection_is_not_rewrapped(
    actual_cas, fmt, exception, monkeypatch
):
    root, catalog, mutation, after = _simple(actual_cas, fmt)
    original = coordinator.prepare_git_tree_projection
    failure = (
        KernelError("git_diff_read", "合成读取失败") if exception is KernelError else exception()
    )

    def verified_then_reader_failure(*args, **kwargs):
        projection = original(*args, **kwargs)

        def read(*args, **kwargs):
            raise failure

        monkeypatch.setattr(GitMaterialCAS, "read", read)
        return projection

    monkeypatch.setattr(coordinator, "prepare_git_tree_projection", verified_then_reader_failure)
    before = _observe(actual_cas.store)
    with pytest.raises(exception) as error:
        _prepare(actual_cas, root, catalog, (mutation,), after)
    assert error.value is failure and _observe(actual_cas.store) == before
