"""固定有界stdout验证解析拒绝；不作为Git业务成功证据。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.delivery.git_repository_recipe import (
    GitRepositoryRead,
    common_directory_recipe,
    drive_git_repository_recipe,
    repository_root_recipe,
    safe_configuration_recipe,
)
from tests.delivery.git_repository_recipe_support import (
    _BINDING_ORDER,
    _BLOB_A,
    _BLOB_B,
    _COMMON,
    _FILTER,
    _HEAD,
    _HEAD_TREE,
    _INCLUDE,
    _ROOT,
    _SAFE_ORDER,
    _SPARSE,
    _STATUS,
    _TREE,
    _VERSION,
    _assert_reads,
    _binding_recipe,
    _FixedRead,
    _noop,
    _reject,
    _tree_record,
)
from tests.delivery.git_repository_recipe_support import (
    parser_root as parser_root,
)


def test_read_request_is_immutable_and_does_not_expose_paths(parser_root: Path) -> None:
    request = GitRepositoryRead(parser_root, _ROOT)
    assert request.accepted == (0,)
    assert str(parser_root) not in repr(request)
    assert "rev-parse" not in repr(request)
    with pytest.raises(FrozenInstanceError):
        request.accepted = (0, 1)


@pytest.mark.parametrize("supplied_kind", ["relative", "missing", "file", "nul", "newline"])
def test_root_invalid_supplied_path_rejected_before_read(
    parser_root: Path, supplied_kind: str
) -> None:
    file = parser_root / "file"
    file.write_bytes(b"not a directory")
    supplied = {
        "relative": Path("relative"),
        "missing": parser_root / "missing",
        "file": file,
        "nul": str(parser_root) + "\0",
        "newline": str(parser_root) + "\n",
    }[supplied_kind]
    read = _FixedRead(parser_root)
    _reject(repository_root_recipe(supplied, checkpoint=_noop), read, "git_repository_invalid")
    assert read.reads == []


@pytest.mark.parametrize("reported_kind", ["parent", "sibling", "missing", "multiple", "nul"])
def test_root_rejects_other_or_malformed_reported_path(
    parser_root: Path, reported_kind: str
) -> None:
    sibling = parser_root.parent / "sibling"
    sibling.mkdir()
    values = {
        "parent": str(parser_root.parent),
        "sibling": str(sibling),
        "missing": str(parser_root / "missing"),
        "multiple": f"{parser_root}\n{sibling}",
        "nul": str(parser_root) + "\0",
    }
    read = _FixedRead(parser_root, {_ROOT: values[reported_kind].encode("utf-8") + b"\n"})
    _reject(repository_root_recipe(parser_root, checkpoint=_noop), read, "git_repository_invalid")
    _assert_reads(read.reads, parser_root, (_ROOT,))


@pytest.mark.parametrize("reported", [b"", b"\n", b".\n"], ids=["empty", "newline", "relative"])
def test_root_empty_or_relative_output_cannot_use_process_cwd(
    parser_root: Path, monkeypatch: pytest.MonkeyPatch, reported: bytes
) -> None:
    monkeypatch.chdir(parser_root)
    read = _FixedRead(parser_root, {_ROOT: reported})
    _reject(repository_root_recipe(parser_root, checkpoint=_noop), read, "git_repository_invalid")


@pytest.mark.parametrize("entry", ["root", "common", "version"])
def test_invalid_utf8_git_text_rejected(parser_root: Path, entry: str) -> None:
    read = _FixedRead(
        parser_root, {{"root": _ROOT, "common": _COMMON, "version": _VERSION}[entry]: b"\xff\n"}
    )
    recipes = {
        "root": repository_root_recipe(parser_root, checkpoint=_noop),
        "common": common_directory_recipe(parser_root, checkpoint=_noop),
        "version": _binding_recipe(parser_root),
    }
    _reject(recipes[entry], read, "git_output_invalid")
    for name, unused in recipes.items():
        if name != entry:
            unused.close()


def test_common_missing_directory_rejected(parser_root: Path) -> None:
    read = _FixedRead(parser_root, {_COMMON: b"missing/common\n"})
    _reject(common_directory_recipe(parser_root, checkpoint=_noop), read, "git_repository_invalid")
    _assert_reads(read.reads, parser_root, (_COMMON,))


@pytest.mark.parametrize(
    "reported",
    [
        b"",
        b"\n",
        b".git/\0objects\n",
        b".git/\x01objects\n",
        b".git/\x7fobjects\n",
        b".git\n.git\n",
    ],
    ids=["empty", "newline", "nul", "control", "delete-control", "multiple-lines"],
)
def test_common_malformed_output_rejected_before_resolution(
    parser_root: Path, reported: bytes
) -> None:
    read = _FixedRead(parser_root, {_COMMON: reported})
    _reject(common_directory_recipe(parser_root, checkpoint=_noop), read, "git_repository_invalid")
    _assert_reads(read.reads, parser_root, (_COMMON,))


@pytest.mark.parametrize("kind", ["file", "missing"])
def test_binding_non_directory_common_object_rejected(parser_root: Path, kind: str) -> None:
    reported = parser_root / "not-common"
    if kind == "file":
        reported.write_bytes(b"not a directory")
    read = _FixedRead(parser_root, {_COMMON: str(reported).encode("utf-8") + b"\n"})
    code = "git_binding_changed" if kind == "file" else "git_repository_invalid"
    _reject(_binding_recipe(parser_root), read, code)
    _assert_reads(read.reads, parser_root, _BINDING_ORDER[:9])


@pytest.mark.parametrize("command", [_HEAD, _HEAD_TREE], ids=["head", "head-tree"])
@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"a" * 39,
        b"a" * 41,
        b"a" * 63,
        b"a" * 65,
        b"A" * 40,
        b"g" * 40,
        b"a" * 40 + b"\r\n",
        b" " + b"a" * 40,
        b"a" * 40 + b"\n\n",
        b"a" * 40 + b"\0",
        b"\xff" * 40,
        b"a" * 4096,
    ],
    ids=[
        "empty",
        "sha1-short",
        "sha1-long",
        "sha256-short",
        "sha256-long",
        "uppercase",
        "not-hex",
        "crlf",
        "space",
        "two-lines",
        "nul",
        "utf8-invalid",
        "very-long",
    ],
)
def test_invalid_oid_rejected_without_followup_reads(
    parser_root: Path, command: tuple[str, ...], body: bytes
) -> None:
    read = _FixedRead(parser_root, {command: body})
    _reject(_binding_recipe(parser_root), read, "git_output_invalid")
    _assert_reads(read.reads, parser_root, _BINDING_ORDER[: _BINDING_ORDER.index(command) + 1])


@pytest.mark.parametrize(("head_length", "tree_length"), [(40, 64), (64, 40)])
def test_mixed_oid_lengths_rejected_by_original_binding_contract(
    parser_root: Path, head_length: int, tree_length: int
) -> None:
    read = _FixedRead(parser_root, {_HEAD: b"a" * head_length, _HEAD_TREE: b"b" * tree_length})
    recipe = _binding_recipe(parser_root)
    with pytest.raises(ValidationError, match="Git对象格式与OID长度不一致"):
        drive_git_repository_recipe(recipe, read, checkpoint=_noop)
    assert recipe.gi_frame is None


@pytest.mark.parametrize("version", [b"", b"git version " + b"x" * 129], ids=["empty", "long"])
def test_invalid_version_rejected_by_original_contract(parser_root: Path, version: bytes) -> None:
    read = _FixedRead(parser_root, {_VERSION: version})
    recipe = _binding_recipe(parser_root)
    with pytest.raises(ValidationError):
        drive_git_repository_recipe(recipe, read, checkpoint=_noop)
    assert recipe.gi_frame is None


@pytest.mark.parametrize("status", [b"? untracked\0", b"1 modified\0", b"\0", b"\xff"])
def test_any_nonempty_status_rejected_before_common_dir(parser_root: Path, status: bytes) -> None:
    read = _FixedRead(parser_root, {_STATUS: status})
    _reject(_binding_recipe(parser_root), read, "delivery_dirty_conflict")
    _assert_reads(read.reads, parser_root, _BINDING_ORDER[:8])


@pytest.mark.parametrize(
    ("command", "body", "code", "count"),
    [
        (_INCLUDE, b"include.path\nexternal\0", "git_config_unsupported", 1),
        (_INCLUDE, b"\xff", "git_config_unsupported", 1),
        (_FILTER, b"filter.test.clean\nfalse\0", "git_filter_unsupported", 2),
        (_FILTER, b"\0", "git_filter_unsupported", 2),
        (_SPARSE, b"true\n", "git_sparse_checkout_unsupported", 3),
    ],
)
def test_unsafe_configuration_stops_at_first_refusal(
    parser_root: Path, command: tuple[str, ...], body: bytes, code: str, count: int
) -> None:
    read = _FixedRead(parser_root, {command: body})
    _reject(safe_configuration_recipe(parser_root, checkpoint=_noop), read, code)
    _assert_reads(read.reads, parser_root, _SAFE_ORDER[:count])


@pytest.mark.parametrize(
    "tree",
    [
        b"not-a-record\0",
        b"100644\tfile\0",
        b"100644 blob\tfile\0",
        b"100644 \xff " + b"a" * 40 + b"\tfile\0",
        _tree_record(b"bad\xff.txt"),
    ],
    ids=["no-tab", "missing-kind-oid", "missing-oid", "non-ascii-metadata", "non-utf8-path"],
)
def test_unparseable_tree_rejected(parser_root: Path, tree: bytes) -> None:
    read = _FixedRead(parser_root, {_TREE: tree})
    _reject(safe_configuration_recipe(parser_root, checkpoint=_noop), read, "git_tree_invalid")
    _assert_reads(read.reads, parser_root, _SAFE_ORDER)


@pytest.mark.parametrize(
    "tree",
    [
        b"999999 blob " + b"a" * 40 + b"\tfile\0",
        b"100644 tree " + b"a" * 40 + b"\tfile\0",
        b"100644 blob not-an-oid\tfile\0",
        _tree_record(b""),
        _tree_record(b"file")[:-1],
    ],
    ids=["invalid-mode", "wrong-kind", "invalid-oid", "empty-path", "unterminated-record"],
)
def test_malformed_tree_metadata_cannot_complete_safety_check(
    parser_root: Path, tree: bytes
) -> None:
    read = _FixedRead(parser_root, {_TREE: tree})
    _reject(safe_configuration_recipe(parser_root, checkpoint=_noop), read, "git_tree_invalid")
    _assert_reads(read.reads, parser_root, _SAFE_ORDER)


@pytest.mark.parametrize("oid", ["", "-p", "a" * 39, "a" * 65, "A" * 40])
def test_malformed_attribute_oid_rejected_before_cat_file(parser_root: Path, oid: str) -> None:
    read = _FixedRead(parser_root, {_TREE: _tree_record(b".gitattributes", oid=oid)})
    _reject(safe_configuration_recipe(parser_root, checkpoint=_noop), read, "git_tree_invalid")
    _assert_reads(read.reads, parser_root, _SAFE_ORDER)


@pytest.mark.parametrize(
    "tree",
    [
        b"160000 commit " + b"a" * 40 + b"\tvendor\0",
        b"100644 commit " + b"a" * 40 + b"\tvendor\0",
        _tree_record(b".gitmodules"),
        _tree_record(b"nested/.GITMODULES"),
        _tree_record(b".lfsconfig"),
        _tree_record(b"nested/.LFSCONFIG"),
    ],
    ids=["gitlink", "commit-kind", "gitmodules", "nested-gitmodules", "lfs", "nested-lfs"],
)
def test_submodule_and_lfs_tree_control_planes_rejected(parser_root: Path, tree: bytes) -> None:
    read = _FixedRead(parser_root, {_TREE: tree})
    _reject(safe_configuration_recipe(parser_root, checkpoint=_noop), read, "git_tree_unsupported")
    _assert_reads(read.reads, parser_root, _SAFE_ORDER)


@pytest.mark.parametrize(
    "body", [b"*.txt filter=lfs\n", b"*.txt FILTER=test\n", b"*.txt working-tree-encoding=UTF-16\n"]
)
def test_all_attribute_blobs_read_before_late_attribute_refusal(
    parser_root: Path, body: bytes
) -> None:
    tree = _tree_record(b".gitattributes") + _tree_record(b"nested/.GITATTRIBUTES", oid=_BLOB_B)
    first, second = ("cat-file", "blob", _BLOB_A), ("cat-file", "blob", _BLOB_B)
    read = _FixedRead(parser_root, {_TREE: tree, first: b"*.txt text\n", second: body})
    _reject(
        safe_configuration_recipe(parser_root, checkpoint=_noop), read, "git_attributes_unsupported"
    )
    _assert_reads(read.reads, parser_root, (*_SAFE_ORDER, first, second))


@pytest.mark.parametrize("control", ["lfs", "submodule"])
@pytest.mark.parametrize("unsafe_attribute", [True, False], ids=["converting", "safe"])
def test_attribute_read_preserves_original_tree_refusal_precedence(
    parser_root: Path, control: str, unsafe_attribute: bool
) -> None:
    later = (
        _tree_record(b".lfsconfig")
        if control == "lfs"
        else (b"160000 commit " + b"b" * 40 + b"\tvendor\0")
    )
    attribute = ("cat-file", "blob", _BLOB_A)
    body = b"*.txt filter=lfs\n" if unsafe_attribute else b"*.txt text\n"
    read = _FixedRead(
        parser_root, {_TREE: _tree_record(b".gitattributes") + later, attribute: body}
    )
    expected = "git_attributes_unsupported" if unsafe_attribute else "git_tree_unsupported"
    _reject(safe_configuration_recipe(parser_root, checkpoint=_noop), read, expected)
    _assert_reads(read.reads, parser_root, (*_SAFE_ORDER, attribute))


@pytest.mark.parametrize("kind", ["file", "dangling-link"])
def test_alternates_rejected_before_configuration_binding(parser_root: Path, kind: str) -> None:
    alternates = parser_root / ".git/objects/info/alternates"
    if kind == "file":
        alternates.write_bytes(b"")
    else:
        alternates.symlink_to(parser_root / "missing-objects")
    read = _FixedRead(parser_root)
    _reject(_binding_recipe(parser_root), read, "git_alternates_unsupported")
    _assert_reads(read.reads, parser_root, _BINDING_ORDER[:9])
