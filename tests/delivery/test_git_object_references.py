"""完整原始对象的直接引用解析；真实 Git 差分仅为合成夹具，不证明业务闭包。"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_object_material import GitObjectMaterial, GitObjectRead
from harnessix.delivery.git_object_references import (
    GitCommitReferences,
    GitTreeEntry,
    parse_git_commit,
    parse_git_tree,
)
from tests.delivery.test_git_material_cas import _material

_FORMATS = ("sha1", "sha256")
_MODES = (
    ("40000", "tree"),
    ("100644", "blob"),
    ("100755", "blob"),
    ("120000", "blob"),
    ("160000", "commit"),
)
_NAME = b"synthetic-private-basename"
_AUTHOR = b"author Reference Fixture <test@harnessix.invalid> 1700000000 +0000"
_COMMITTER = b"committer Reference Fixture <test@harnessix.invalid> 1700000000 +0000"


def _checkpoint():
    return None


def _pointer(object_format, byte=1):
    return bytes([byte]) * (20 if object_format == "sha1" else 32)


def _entry(object_format, mode=b"100644", name=_NAME, raw_oid=None):
    return mode + b" " + name + b"\0" + (_pointer(object_format) if raw_oid is None else raw_oid)


def _commit_body(object_format, parents=(), *, extensions=b"", message=b"fixture\n"):
    lines = [b"tree " + _pointer(object_format).hex().encode("ascii")]
    lines.extend(b"parent " + value for value in parents)
    lines.extend((_AUTHOR, _COMMITTER))
    if extensions:
        lines.append(extensions)
    return b"\n".join(lines) + b"\n\n" + message


def _parse(kind, material, limit=32, checkpoint=_checkpoint):
    if kind == "tree":
        return parse_git_tree(material, max_entries=limit, checkpoint=checkpoint)
    return parse_git_commit(material, max_parents=limit, checkpoint=checkpoint)


def _reject(kind, body, object_format="sha1", *, limit=32, code=None):
    material = _material(kind, object_format, body)
    with pytest.raises(KernelError) as rejected:
        _parse(kind, material, limit)
    assert rejected.value.code == (code or "git_object_references_invalid")
    assert _NAME.decode("ascii") not in str(rejected.value)
    assert rejected.value.__cause__ is None
    return rejected.value


@pytest.mark.parametrize("object_format", _FORMATS)
def test_empty_tree_exact_zero_limit_and_complete_material(object_format):
    material = _material("tree", object_format, b"")
    assert _parse("tree", material, 0) == ()
    assert material.body_bytes == 0
    assert material == _material("tree", object_format, b"")


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize(("mode", "kind"), _MODES)
def test_all_five_canonical_modes_have_precise_raw_oid_type_and_format(object_format, mode, kind):
    width = 20 if object_format == "sha1" else 32
    raw = bytes(range(width))
    material = _material("tree", object_format, _entry(object_format, mode.encode(), raw_oid=raw))
    entries = _parse("tree", material, 1)
    assert type(entries) is tuple and len(entries) == 1
    entry = entries[0]
    assert type(entry) is GitTreeEntry
    assert entry.mode == mode and entry.name == _NAME
    assert type(entry.child) is GitObjectRead
    assert entry.child == GitObjectRead(kind, raw.hex(), object_format)
    assert "name=" not in repr(entry) and _NAME.decode() not in repr(entry)
    assert "body=" not in repr(material)
    with pytest.raises(FrozenInstanceError):
        entry.mode = "100644"


@pytest.mark.parametrize("object_format", _FORMATS)
def test_git_directory_slash_order_not_basename_order(object_format):
    body = _entry(object_format, name=b"a.c") + _entry(object_format, b"40000", b"a")
    assert [entry.name for entry in _parse("tree", _material("tree", object_format, body))] == [
        b"a.c",
        b"a",
    ]
    reverse = _entry(object_format, b"40000", b"a") + _entry(object_format, name=b"a.c")
    _reject("tree", reverse, object_format)


@pytest.mark.parametrize("object_format", _FORMATS)
def test_nonadjacent_duplicate_basename_even_when_git_sort_keys_increase(object_format):
    body = (
        _entry(object_format, name=b"a")
        + _entry(object_format, name=b"a.c")
        + _entry(object_format, b"40000", b"a")
    )
    _reject("tree", body, object_format)


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("names", [(b"b", b"a"), (b"same", b"same")])
def test_unsorted_or_duplicate_tree_entries_reject(object_format, names):
    _reject("tree", b"".join(_entry(object_format, name=name) for name in names), object_format)


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("name", [b"", b".", b"..", b".git", b".GiT", b"a/b", b"/a"])
def test_invalid_tree_basename_rejects_without_rendering_material(object_format, name):
    _reject("tree", _entry(object_format, name=name), object_format)


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize(
    "name", [b"raw-\xff\xfe", b"line\nname", b"tab\tname", b"space name", b" leading"]
)
def test_parser_preserves_raw_names_without_utf8_or_workspace_coercion(object_format, name):
    material = _material("tree", object_format, _entry(object_format, name=name))
    assert _parse("tree", material)[0].name == name
    assert material.body == _entry(object_format, name=name)


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize(
    "mode",
    [
        b"",
        b"040000",
        b"0100644",
        b"100664",
        b"100600",
        b"100777",
        b"10064",
        b"40001",
        b"140000",
        b"160001",
        b"100644 ",
        b"100644\t",
        b"+100644",
        b"0x81a4",
        b"\xff",
    ],
)
def test_mode_boundary_preserves_leading_space_names_and_rejects_invalid_modes(object_format, mode):
    if mode == b"100644 ":
        entry = _parse("tree", _material("tree", object_format, _entry(object_format, mode=mode)))[
            0
        ]
        assert entry.mode == "100644" and entry.name == b" " + _NAME
        return
    _reject("tree", _entry(object_format, mode=mode), object_format)


@pytest.mark.parametrize(
    ("object_format", "cut"),
    [(fmt, cut) for fmt in _FORMATS for cut in range(1, len(_entry(fmt, name=b"f")))],
)
def test_every_nonempty_prefix_of_one_tree_record_is_truncated(object_format, cut):
    _reject("tree", _entry(object_format, name=b"f")[:cut], object_format)


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("tail", [b"\0", b"\n", b" ", b"x", b"100644 unfinished\0", b"\xff"])
def test_valid_tree_record_with_extra_trailing_fragment_rejects(object_format, tail):
    _reject("tree", _entry(object_format) + tail, object_format)


@pytest.mark.parametrize("object_format", _FORMATS)
def test_missing_separator_zero_oid_and_wrong_raw_oid_width(object_format):
    raw = _pointer(object_format)
    bodies = (
        b"100644" + _NAME + b"\0" + raw,
        b"100644 " + _NAME + raw,
        _entry(object_format, raw_oid=b"\0" * len(raw)),
        _entry(object_format, raw_oid=_pointer("sha256" if object_format == "sha1" else "sha1")),
        _entry(object_format, name=b"a\0suffix"),
    )
    for body in bodies:
        _reject("tree", body, object_format)


@pytest.mark.parametrize("object_format", _FORMATS)
def test_tree_entry_limit_exact_boundary_without_partial_result(object_format):
    body = b"".join(_entry(object_format, name=name) for name in (b"a", b"b", b"c"))
    material = _material("tree", object_format, body)
    assert len(_parse("tree", material, 3)) == len(_parse("tree", material, 4)) == 3
    for limit in (0, 1, 2):
        _reject("tree", body, object_format, limit=limit, code="git_object_references_limit")


@pytest.mark.parametrize("kind", ["tree", "commit"])
@pytest.mark.parametrize("limit", [True, False, -1, 1.0, "1", None])
def test_limits_require_actual_nonnegative_integers(kind, limit):
    body = b"" if kind == "tree" else _commit_body("sha1")
    _reject(kind, body, limit=limit, code="git_object_references_limit_invalid")


@pytest.mark.parametrize("kind", ["tree", "commit"])
def test_integer_subclass_is_not_a_canonical_limit(kind):
    class Limit(int):
        pass

    body = b"" if kind == "tree" else _commit_body("sha1")
    _reject(kind, body, limit=Limit(1), code="git_object_references_limit_invalid")


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("parents_count", [0, 1, 3])
def test_root_single_and_merge_parent_order_exact_limit(object_format, parents_count):
    parents = tuple(
        _pointer(object_format, byte).hex().encode() for byte in range(2, 2 + parents_count)
    )
    material = _material("commit", object_format, _commit_body(object_format, parents))
    result = _parse("commit", material, parents_count)
    assert type(result) is GitCommitReferences and type(result.parents) is tuple
    assert result.tree == GitObjectRead("tree", _pointer(object_format).hex(), object_format)
    assert result.parents == tuple(
        GitObjectRead("commit", oid.decode(), object_format) for oid in parents
    )
    assert _parse("commit", material, parents_count + 1) == result
    with pytest.raises(FrozenInstanceError):
        result.parents = ()
    if parents_count:
        _reject(
            "commit",
            material.body,
            object_format,
            limit=parents_count - 1,
            code="git_object_references_limit",
        )


@pytest.mark.parametrize("object_format", _FORMATS)
def test_repeated_parents_preserve_original_order_and_consume_capacity(object_format):
    first, second = (_pointer(object_format, byte).hex().encode() for byte in (2, 3))
    body = _commit_body(object_format, (first, second, first))
    result = _parse("commit", _material("commit", object_format, body), 3)
    assert [parent.object_id for parent in result.parents] == [
        first.decode(),
        second.decode(),
        first.decode(),
    ]
    _reject("commit", body, object_format, limit=2, code="git_object_references_limit")


@pytest.mark.parametrize("object_format", _FORMATS)
def test_message_tree_parent_text_and_nul_are_not_references(object_format):
    message = b"tree " + b"0" * 64 + b"\nparent invalid\n\0binary message\n"
    material = _material("commit", object_format, _commit_body(object_format, message=message))
    assert _parse("commit", material, 0).parents == ()
    assert material.body.endswith(message)


@pytest.mark.parametrize("object_format", _FORMATS)
def test_unknown_extensions_and_continuations_keep_original_material(object_format):
    extensions = (
        b"encoding arbitrary\nx-fixture original\n continuation\ngpgsig synthetic-only"
        b"\n tree invalid\n parent invalid\n \n end"
    )
    body = _commit_body(object_format, extensions=extensions, message=b"\xffno final LF")
    material = _material("commit", object_format, body)
    before = (material.body, material.object_id, material.body_sha256)
    assert _parse("commit", material, 0).parents == ()
    assert (material.body, material.object_id, material.body_sha256) == before


@pytest.mark.parametrize("object_format", _FORMATS)
def test_commit_no_message_single_final_lf_and_header_identity_structure_only(object_format):
    body = (
        b"tree " + _pointer(object_format).hex().encode() + b"\nauthor opaque\ncommitter opaque\n"
    )
    assert _parse("commit", _material("commit", object_format, body), 0).parents == ()
    _reject("commit", body[:-1], object_format)


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("field", [b"tree", b"parent"])
@pytest.mark.parametrize(
    "defect", ["uppercase", "short", "long", "zero", "space", "tab", "nonascii"]
)
def test_commit_pointers_require_precise_lowercase_nonzero_oid(object_format, field, defect):
    oid = b"ab" * len(_pointer(object_format))
    values = {
        "uppercase": oid.upper(),
        "short": oid[:-1],
        "long": oid + b"0",
        "zero": b"0" * len(oid),
        "space": oid + b" ",
        "tab": b"\t" + oid,
        "nonascii": b"\xff" + oid[1:],
    }
    body = _commit_body(object_format)
    if field == b"tree":
        body = body.replace(_pointer(object_format).hex().encode(), values[defect], 1)
    else:
        body = _commit_body(object_format, (values[defect],))
    _reject("commit", body, object_format)


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize(
    "case",
    [
        "tree-late",
        "parent-first",
        "tree-duplicate",
        "parent-late",
        "author-duplicate",
        "committer-duplicate",
        "missing-author",
        "missing-committer",
        "empty-author",
        "orphan-continuation",
        "tab-continuation",
        "nul-header",
        "no-tree",
        "invalid-key",
    ],
)
def test_commit_header_order_duplicates_and_structure_reject(object_format, case):
    tree = b"tree " + _pointer(object_format).hex().encode()
    parent = b"parent " + _pointer(object_format, 2).hex().encode()
    base = [tree, _AUTHOR, _COMMITTER]
    variants = {
        "tree-late": [b"x-fixture before", *base],
        "parent-first": [parent, *base],
        "tree-duplicate": [*base, tree],
        "parent-late": [*base, b"x-fixture value", parent],
        "author-duplicate": [*base, _AUTHOR],
        "committer-duplicate": [*base, _COMMITTER],
        "missing-author": [tree, _COMMITTER],
        "missing-committer": [tree, _AUTHOR],
        "empty-author": [tree, b"author ", _COMMITTER],
        "orphan-continuation": [b" continuation", *base],
        "tab-continuation": [*base, b"x-fixture value", b"\tcontinuation"],
        "nul-header": [tree, _AUTHOR + b"\0", _COMMITTER],
        "no-tree": [_AUTHOR, _COMMITTER],
        "invalid-key": [*base, b"x\x01 value"],
    }
    _reject("commit", b"\n".join(variants[case]) + b"\n\nfixture", object_format)


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize(
    "extension",
    [
        b"x-extra raw-value",
        b"x-extra \xff\xfe",
        b"\xff-key \xfe-value",
        b"x-extra \n opaque continuation",
    ],
)
def test_unknown_raw_header_key_value_and_empty_first_line_remain_opaque(object_format, extension):
    body = _commit_body(object_format, extensions=extension)
    material = _material("commit", object_format, body)
    before = (material.body, material.object_id, material.body_sha256)
    assert _parse("commit", material, 0).parents == ()
    assert (material.body, material.object_id, material.body_sha256) == before


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("header", [b"tree", b"parent", b"author", b"committer"])
def test_core_header_continuation_cannot_be_hidden_as_extension(object_format, header):
    parent = _pointer(object_format, 2).hex().encode()
    body = _commit_body(object_format, (parent,))
    lines = body.split(b"\n")
    position = next(index for index, line in enumerate(lines) if line.startswith(header + b" "))
    lines.insert(position + 1, b" continuation")
    _reject("commit", b"\n".join(lines), object_format)


@pytest.mark.parametrize("kind", ["tree", "commit"])
@pytest.mark.parametrize("value", [None, True, b"body", {}, object()])
def test_parser_requires_actual_material_class(kind, value):
    with pytest.raises(KernelError, match="Git对象直接引用") as rejected:
        _parse(kind, value)
    assert rejected.value.code == "git_object_references_invalid"


@pytest.mark.parametrize("kind", ["tree", "commit"])
def test_subclass_duck_type_and_uninitialized_material_are_rejected(kind):
    body = b"" if kind == "tree" else _commit_body("sha1")
    valid = _material(kind, "sha1", body)

    class MaterialSubclass(GitObjectMaterial):
        pass

    class Duck:
        object_type = valid.object_type
        object_id = valid.object_id
        object_format = valid.object_format
        body = valid.body

    values = [
        MaterialSubclass(kind, valid.object_id, "sha1", body),
        Duck(),
        object.__new__(GitObjectMaterial),
    ]
    for value in values:
        with pytest.raises(KernelError) as rejected:
            _parse(kind, value)
        assert rejected.value.code == "git_object_references_invalid"


@pytest.mark.parametrize("kind", ["tree", "commit"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("object_type", True),
        ("object_type", "blob"),
        ("object_id", "0" * 40),
        ("object_id", False),
        ("object_format", "sha256"),
        ("object_format", True),
        ("body", b"changed-body"),
        ("body", bytearray()),
        ("body", False),
    ],
)
def test_material_frozen_bypass_is_revalidated_before_parsing(kind, field, value):
    body = b"" if kind == "tree" else _commit_body("sha1")
    material = _material(kind, "sha1", body)
    object.__setattr__(material, field, value)
    with pytest.raises(KernelError) as rejected:
        _parse(kind, material)
    assert rejected.value.code == "git_object_references_invalid"


@pytest.mark.parametrize("kind", ["tree", "commit"])
@pytest.mark.parametrize("error_type", [TurnCancelled, asyncio.CancelledError, RuntimeError])
@pytest.mark.parametrize("phase", ["entry", "middle", "last"])
def test_checkpoint_cancellation_and_other_errors_propagate_same_exception(kind, error_type, phase):
    body = (
        (_entry("sha1", name=b"a") + _entry("sha1", name=b"b"))
        if kind == "tree"
        else _commit_body("sha1", extensions=b"x-fixture value\n continued")
    )
    material = _material(kind, "sha1", body)
    calls = []
    _parse(kind, material, checkpoint=lambda: calls.append(None))
    target = {"entry": 1, "middle": 2, "last": len(calls)}[phase]
    count = 0
    error = error_type("synthetic-checkpoint-signal")

    def check():
        nonlocal count
        count += 1
        if count == target:
            raise error

    with pytest.raises(error_type) as caught:
        _parse(kind, material, checkpoint=check)
    assert caught.value is error and count == target


@pytest.fixture
def native_repo(tmp_path, record_testsuite_property):
    git = Path(shutil.which("git") or "")
    assert git.is_file(), "差分夹具需要当前PATH中的Git，不跳过实际对象格式验证"
    root = tmp_path / "synthetic-native-repo"
    root.mkdir(mode=0o700)
    home = tmp_path / "synthetic-home"
    home.mkdir(mode=0o700)
    env = {
        "PATH": str(git.parent) + os.pathsep + os.defpath,
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
        "GIT_AUTHOR_NAME": "Reference Fixture",
        "GIT_AUTHOR_EMAIL": "test@harnessix.invalid",
        "GIT_COMMITTER_NAME": "Reference Fixture",
        "GIT_COMMITTER_EMAIL": "test@harnessix.invalid",
        "GIT_AUTHOR_DATE": "1700000000 +0000",
        "GIT_COMMITTER_DATE": "1700000000 +0000",
    }

    def command(*args, body=None):
        result = subprocess.run(
            (str(git), *args),
            cwd=root,
            env=env,
            input=body,
            capture_output=True,
            timeout=20,
            check=True,
        )
        return result.stdout

    # 研究源码版本不等于运行版本；能力由后续真实SHA-1/SHA-256对象差分求证。
    version = command("--version").strip()
    assert version.startswith(b"git version ")
    record_testsuite_property("git_version", version.decode("ascii"))
    return command


@pytest.mark.parametrize("version", (b"2.53.0", b"2.55.0", b"2.53.0.windows.1"))
def test_native_fixture_uses_path_git_and_records_actual_version(tmp_path, monkeypatch, version):
    git = tmp_path / "git"
    git.touch()
    monkeypatch.setattr(shutil, "which", lambda name: str(git) if name == "git" else None)
    calls = []
    properties = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=b"git version " + version + b"\n")

    monkeypatch.setattr(subprocess, "run", run)
    native_repo.__wrapped__(tmp_path, lambda key, value: properties.append((key, value)))
    assert calls[0][0] == (str(git), "--version")
    assert calls[0][1]["check"] is True
    assert calls[0][1]["timeout"] == 20
    assert calls[0][1]["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
    assert properties == [("git_version", "git version " + version.decode())]


def test_native_fixture_missing_git_fails_instead_of_skipping(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(AssertionError, match="当前PATH"):
        native_repo.__wrapped__(tmp_path, lambda key, value: None)


@pytest.mark.parametrize("object_format", _FORMATS)
def test_real_git_mktree_binary_modes_sort_and_nonutf8_differential(native_repo, object_format):
    git = native_repo
    git("init", "--bare", "--object-format=" + object_format)
    blob = git("hash-object", "-w", "--stdin", body=b"synthetic\0blob").strip()
    empty = git("mktree", body=b"").strip()
    commit = git("commit-tree", empty.decode(), body=b"synthetic root\n").strip()
    rows = [
        (b"100644", b"blob", blob, b" " + _NAME),
        (b"100644", b"blob", blob, b"a.c"),
        (b"040000", b"tree", empty, b"a"),
        (b"100755", b"blob", blob, b"exec"),
        (b"100644", b"blob", blob, b"raw-\xff"),
        (b"120000", b"blob", blob, b"symlink"),
        (b"160000", b"commit", commit, b"submodule"),
    ]
    raw = b"".join(
        mode + b" " + kind + b" " + oid + b"\t" + name + b"\0" for mode, kind, oid, name in rows
    )
    oid = git("mktree", "-z", body=raw).strip()
    body = git("cat-file", "tree", oid.decode())
    material = _material("tree", object_format, body)
    assert material.object_id.encode() == oid
    entries = _parse("tree", material, len(rows))
    listed = git("ls-tree", "-z", oid.decode()).split(b"\0")[:-1]
    expected = []
    for line in listed:
        metadata, name = line.split(b"\t", 1)
        mode, kind, child_oid = metadata.split(b" ")
        expected.append((mode.lstrip(b"0").decode(), name, kind.decode(), child_oid.decode()))
    assert [
        (entry.mode, entry.name, entry.child.object_type, entry.child.object_id)
        for entry in entries
    ] == expected
    assert [entry.name for entry in entries][:3] == [b" " + _NAME, b"a.c", b"a"]
    leading = _entry(object_format, mode=b"100644 ", raw_oid=bytes.fromhex(blob.decode()))
    assert leading in body
    leading_tree = git("mktree", "-z", body=listed[0] + b"\0").strip()
    assert git("cat-file", "tree", leading_tree.decode()) == leading
    assert git("ls-tree", "-z", leading_tree.decode()) == listed[0] + b"\0"
    assert (
        _parse("tree", _material("tree", object_format, git("cat-file", "tree", empty.decode())), 0)
        == ()
    )


@pytest.mark.parametrize("object_format", _FORMATS)
def test_real_git_root_merge_and_message_reference_differential(native_repo, object_format):
    git = native_repo
    git("init", "--bare", "--object-format=" + object_format)
    tree = git("mktree", body=b"").strip()
    roots = [
        git("commit-tree", tree.decode(), body=message).strip()
        for message in (b"first\n", b"second\n")
    ]
    message = b"tree invalid\nparent invalid\n"
    merged = git(
        "commit-tree", tree.decode(), "-p", roots[1].decode(), "-p", roots[0].decode(), body=message
    ).strip()
    for oid, parents in ((roots[0], ()), (merged, (roots[1], roots[0]))):
        body = git("cat-file", "commit", oid.decode())
        material = _material("commit", object_format, body)
        assert material.object_id.encode() == oid
        references = _parse("commit", material, len(parents))
        assert references.tree.object_id.encode() == tree
        assert tuple(parent.object_id.encode() for parent in references.parents) == parents
        assert git("rev-list", "--parents", "-n", "1", oid.decode()).split() == [oid, *parents]
