"""真实Git合同、物理身份及配方驱动取消与关闭边界。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.git import GitDeliveryRuntime, git_delivery_implementation_digest
from harnessix.delivery.git_repository_recipe import (
    GitRepositoryRead,
    GitRepositoryRecipe,
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
    _HEAD,
    _ROOT,
    _SAFE_ORDER,
    _STATUS,
    _TREE,
    _VERSION,
    _WORKSPACE,
    _assert_reads,
    _binding_recipe,
    _commit_file,
    _expected_binding,
    _FailCheckpoint,
    _FixedRead,
    _noop,
    _probe_recipe,
    _RealRead,
    _reject,
    _tree_record,
)
from tests.delivery.git_repository_recipe_support import (
    parser_root as parser_root,
)
from tests.delivery.git_repository_recipe_support import (
    repository as repository,
)
from tests.delivery.git_repository_recipe_support import (
    runtime as runtime,
)
from tests.delivery.test_git import _run


def test_driver_returns_only_real_generator_return_value_and_closes(parser_root: Path) -> None:
    events: list = []
    read = _FixedRead(parser_root)
    recipe = _probe_recipe(events, parser_root)
    checkpoint = _FailCheckpoint(100, AssertionError("checkpoint limit"))
    result = drive_git_repository_recipe(recipe, read, checkpoint=checkpoint)
    assert result == b".git\n"
    assert events == [("received", read.responses[_ROOT]), "closed"]
    assert checkpoint.calls == 5 and recipe.gi_frame is None
    _assert_reads(read.reads, parser_root, (_ROOT, _COMMON))


@pytest.mark.parametrize(
    "error_type",
    [
        KernelError,
        OSError,
        RuntimeError,
        ValueError,
        StopIteration,
        asyncio.CancelledError,
        KeyboardInterrupt,
    ],
)
def test_driver_read_exception_is_not_completion_and_closes(
    parser_root: Path, error_type: type[BaseException]
) -> None:
    error = (
        KernelError("read_denied", "读取拒绝") if error_type is KernelError else error_type("read")
    )
    events: list = []
    recipe = _probe_recipe(events, parser_root)

    def fail(_: GitRepositoryRead) -> bytes:
        raise error

    with pytest.raises(error_type) as denied:
        drive_git_repository_recipe(recipe, fail, checkpoint=_noop)
    assert denied.value is error
    assert events == ["closed"] and recipe.gi_frame is None


@pytest.mark.parametrize("error_type", [KernelError, OSError, StopIteration])
def test_driver_recipe_exception_is_not_rewritten_and_closes(
    parser_root: Path, error_type: type[BaseException]
) -> None:
    error = (
        KernelError("recipe_denied", "配方拒绝")
        if error_type is KernelError
        else error_type("stop")
    )
    events: list = []

    def failing_recipe() -> GitRepositoryRecipe:
        try:
            yield GitRepositoryRead(parser_root, _ROOT)
            raise error
        finally:
            events.append("closed")

    recipe = failing_recipe()
    expected = RuntimeError if error_type is StopIteration else error_type
    with pytest.raises(expected) as denied:
        drive_git_repository_recipe(recipe, _FixedRead(parser_root), checkpoint=_noop)
    assert (denied.value.__cause__ if error_type is StopIteration else denied.value) is error
    assert events == ["closed"] and recipe.gi_frame is None


def test_driver_empty_recipe_returns_its_value_without_read(parser_root: Path) -> None:
    sentinel = object()

    def completed_recipe() -> GitRepositoryRecipe:
        yield from ()
        return sentinel

    recipe = completed_recipe()
    read = _FixedRead(parser_root)
    assert drive_git_repository_recipe(recipe, read, checkpoint=_noop) is sentinel
    assert read.reads == [] and recipe.gi_frame is None


@pytest.mark.parametrize("at", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("error_type", [StopIteration, asyncio.CancelledError, KernelError])
def test_driver_checkpoint_exception_never_masquerades_as_completion(
    parser_root: Path, at: int, error_type: type[BaseException]
) -> None:
    error = (
        KernelError("cancelled", "检查拒绝") if error_type is KernelError else error_type("stop")
    )
    checkpoint = _FailCheckpoint(at, error)
    read = _FixedRead(parser_root)
    events: list = []
    recipe = _probe_recipe(events, parser_root)
    with pytest.raises(error_type) as denied:
        drive_git_repository_recipe(recipe, read, checkpoint=checkpoint)
    assert denied.value is error and recipe.gi_frame is None
    assert checkpoint.calls == at
    assert len(read.reads) == {1: 0, 2: 0, 3: 1, 4: 1, 5: 2}[at]
    assert ("closed" in events) == (at != 1)


@pytest.mark.parametrize("entry", ["root", "common", "safe", "binding"])
@pytest.mark.parametrize("at", [1, 2])
def test_recipe_checkpoint_stopiteration_is_a_failure_not_return(
    parser_root: Path, entry: str, at: int
) -> None:
    error = StopIteration("not a recipe result")
    checkpoint = _FailCheckpoint(at, error)
    factories = {
        "root": repository_root_recipe,
        "common": common_directory_recipe,
        "safe": safe_configuration_recipe,
        "binding": _binding_recipe,
    }
    recipe = factories[entry](parser_root, checkpoint=checkpoint)
    with pytest.raises(RuntimeError, match="generator raised StopIteration") as denied:
        drive_git_repository_recipe(recipe, _FixedRead(parser_root), checkpoint=_noop)
    assert denied.value.__cause__ is error and recipe.gi_frame is None


@pytest.mark.parametrize("at", [5, 45, 204])
@pytest.mark.parametrize("error_type", [asyncio.CancelledError, StopIteration])
def test_tree_record_loop_checks_cancellation_without_more_reads(
    parser_root: Path, at: int, error_type: type[BaseException]
) -> None:
    error = error_type("loop cancelled")
    checkpoint = _FailCheckpoint(at, error)
    tree = b"".join(_tree_record(f"file-{index}".encode()) for index in range(200))
    read = _FixedRead(parser_root, {_TREE: tree})
    recipe = safe_configuration_recipe(parser_root, checkpoint=checkpoint)
    expected = RuntimeError if error_type is StopIteration else error_type
    with pytest.raises(expected) as denied:
        drive_git_repository_recipe(recipe, read, checkpoint=_noop)
    assert (denied.value.__cause__ if error_type is StopIteration else denied.value) is error
    assert checkpoint.calls == at and recipe.gi_frame is None
    _assert_reads(read.reads, parser_root, _SAFE_ORDER)


def test_attribute_loop_checks_cancellation_before_next_blob(parser_root: Path) -> None:
    tree = _tree_record(b".gitattributes") + _tree_record(b"nested/.gitattributes", oid=_BLOB_B)
    error = asyncio.CancelledError("attribute cancelled")
    checkpoint = _FailCheckpoint(8, error)
    first = ("cat-file", "blob", _BLOB_A)
    read = _FixedRead(parser_root, {_TREE: tree, first: b"*.txt text\n"})
    recipe = safe_configuration_recipe(parser_root, checkpoint=checkpoint)
    with pytest.raises(asyncio.CancelledError) as denied:
        drive_git_repository_recipe(recipe, read, checkpoint=_noop)
    assert denied.value is error and recipe.gi_frame is None
    _assert_reads(read.reads, parser_root, (*_SAFE_ORDER, first))


def test_binding_final_checkpoint_stopiteration_cannot_return_binding(parser_root: Path) -> None:
    error = StopIteration("not a binding")
    checkpoint = _FailCheckpoint(13, error)
    read = _FixedRead(parser_root)
    recipe = _binding_recipe(parser_root, checkpoint=checkpoint)
    with pytest.raises(RuntimeError, match="generator raised StopIteration") as denied:
        drive_git_repository_recipe(recipe, read, checkpoint=_noop)
    assert denied.value.__cause__ is error and recipe.gi_frame is None
    _assert_reads(read.reads, parser_root, _BINDING_ORDER)


@pytest.mark.parametrize("repository", ["sha1", "sha256"], indirect=True)
def test_real_git_binding_preserves_all_original_fields_and_command_order(
    repository: Path, runtime: GitDeliveryRuntime
) -> None:
    read = _RealRead(runtime)
    recipe = _binding_recipe(
        repository,
        executable_identity=runtime._git.identity,
        implementation_digest=git_delivery_implementation_digest(),
    )
    binding = drive_git_repository_recipe(recipe, read, checkpoint=_noop)
    assert binding.model_dump() == _expected_binding(runtime, repository, read)
    assert binding == runtime.bind_repository(repository, _WORKSPACE)
    _assert_reads(read.reads, repository, _BINDING_ORDER)
    assert _run(repository, *_STATUS) == b"" and recipe.gi_frame is None


@pytest.mark.parametrize("entry", ["root", "common", "safe"])
def test_real_git_original_entrypoints_use_exact_shared_fixed_commands(
    repository: Path, runtime: GitDeliveryRuntime, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    original = runtime._git.run
    reads: list[GitRepositoryRead] = []

    def record(cwd: Path, arguments: tuple[str, ...], *, accepted: tuple[int, ...] = (0,)):
        reads.append(GitRepositoryRead(cwd, arguments, accepted))
        return original(cwd, arguments, accepted=accepted)

    monkeypatch.setattr(runtime._git, "run", record)
    calls = {
        "root": runtime._repository_root,
        "common": runtime._common_directory,
        "safe": runtime._reject_unsafe_configuration,
    }
    expected = {"root": repository, "common": repository / ".git", "safe": None}
    orders = {"root": (_ROOT,), "common": (_COMMON,), "safe": _SAFE_ORDER}
    assert calls[entry](repository) == expected[entry]
    _assert_reads(reads, repository, orders[entry])


def test_real_git_public_binding_uses_exact_original_command_order(
    repository: Path, runtime: GitDeliveryRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = runtime._git.run
    reads: list[GitRepositoryRead] = []

    def record(cwd: Path, arguments: tuple[str, ...], *, accepted: tuple[int, ...] = (0,)):
        reads.append(GitRepositoryRead(cwd, arguments, accepted))
        return original(cwd, arguments, accepted=accepted)

    monkeypatch.setattr(runtime._git, "run", record)
    binding = runtime.bind_repository(repository, _WORKSPACE)
    assert binding.head_oid == _run(repository, *_HEAD).decode().strip()
    _assert_reads(reads, repository, _BINDING_ORDER)


def test_real_git_subdirectory_cannot_masquerade_as_repository_root(
    repository: Path, runtime: GitDeliveryRuntime
) -> None:
    child = repository / "child"
    child.mkdir()
    read = _RealRead(runtime)
    _reject(repository_root_recipe(child, checkpoint=_noop), read, "git_repository_invalid")
    _assert_reads(read.reads, child, (_ROOT,))


def test_real_git_linked_worktree_preserves_actual_common_directory(
    repository: Path, runtime: GitDeliveryRuntime, tmp_path: Path
) -> None:
    linked = tmp_path / "linked"
    _run(repository, "worktree", "add", "--detach", str(linked), "HEAD")
    linked = linked.resolve()
    read = _RealRead(runtime)
    result = drive_git_repository_recipe(
        common_directory_recipe(linked, checkpoint=_noop), read, checkpoint=_noop
    )
    assert result == (repository / ".git").resolve()
    assert result != linked / ".git"
    _assert_reads(read.reads, linked, (_COMMON,))


@pytest.mark.parametrize("dirty", ["tracked", "untracked", "staged"])
def test_real_git_dirty_repository_rejected(
    repository: Path, runtime: GitDeliveryRuntime, dirty: str
) -> None:
    if dirty == "untracked":
        (repository / "untracked.txt").write_bytes(b"new\n")
    else:
        (repository / "modify.txt").write_bytes(b"changed\n")
        if dirty == "staged":
            _run(repository, "add", "modify.txt")
    read = _RealRead(runtime)
    _reject(_binding_recipe(repository), read, "delivery_dirty_conflict")
    _assert_reads(read.reads, repository, _BINDING_ORDER[:8])


@pytest.mark.parametrize(
    ("key", "value", "code", "count"),
    [
        ("include.path", "external", "git_config_unsupported", 1),
        ("includeIf.gitdir:unmatched/.path", "external", "git_config_unsupported", 1),
        ("filter.test.clean", "false", "git_filter_unsupported", 2),
        ("filter.test.smudge", "false", "git_filter_unsupported", 2),
        ("filter.test.process", "false", "git_filter_unsupported", 2),
        ("core.sparseCheckout", "true", "git_sparse_checkout_unsupported", 3),
    ],
)
def test_real_git_unsafe_local_configuration_rejected(
    repository: Path,
    runtime: GitDeliveryRuntime,
    tmp_path: Path,
    key: str,
    value: str,
    code: str,
    count: int,
) -> None:
    if value == "external":
        external = tmp_path / "external.config"
        external.write_text("[core]\n\tignorecase = false\n", encoding="utf-8")
        value = str(external)
    _run(repository, "config", key, value)
    read = _RealRead(runtime)
    _reject(safe_configuration_recipe(repository, checkpoint=_noop), read, code)
    _assert_reads(read.reads, repository, _SAFE_ORDER[:count])


@pytest.mark.parametrize(
    ("name", "body", "code"),
    [
        (".gitmodules", b'[submodule "vendor"]\n', "git_tree_unsupported"),
        ("nested/.lfsconfig", b"[lfs]\n", "git_tree_unsupported"),
        (".gitattributes", b"*.txt filter=lfs\n", "git_attributes_unsupported"),
        (
            "nested/.gitattributes",
            b"*.txt working-tree-encoding=UTF-16\n",
            "git_attributes_unsupported",
        ),
    ],
)
def test_real_git_tree_control_planes_rejected(
    repository: Path, runtime: GitDeliveryRuntime, name: str, body: bytes, code: str
) -> None:
    _commit_file(repository, name, body)
    read = _RealRead(runtime)
    _reject(safe_configuration_recipe(repository, checkpoint=_noop), read, code)
    assert read.outputs[_TREE]
    assert _STATUS not in read.outputs


def test_real_git_submodule_gitlink_rejected(repository: Path, runtime: GitDeliveryRuntime) -> None:
    oid = _run(repository, *_HEAD).decode().strip()
    _run(repository, "update-index", "--add", "--cacheinfo", f"160000,{oid},vendor")
    _run(repository, "commit", "-qm", "gitlink fixture")
    read = _RealRead(runtime)
    _reject(safe_configuration_recipe(repository, checkpoint=_noop), read, "git_tree_unsupported")
    assert b"160000 commit " in read.outputs[_TREE]
    _assert_reads(read.reads, repository, _SAFE_ORDER)


@pytest.mark.parametrize("control", ["lfs", "submodule"])
@pytest.mark.parametrize("unsafe_attribute", [True, False], ids=["converting", "safe"])
def test_real_git_attribute_refusal_precedes_later_tree_control_plane(
    repository: Path, runtime: GitDeliveryRuntime, control: str, unsafe_attribute: bool
) -> None:
    body = b"*.txt filter=lfs\n" if unsafe_attribute else b"*.txt text\n"
    _commit_file(repository, ".gitattributes", body)
    if control == "lfs":
        _commit_file(repository, ".lfsconfig", b"[lfs]\n")
    else:
        oid = _run(repository, *_HEAD).decode().strip()
        _run(repository, "update-index", "--add", "--cacheinfo", f"160000,{oid},vendor")
        _run(repository, "commit", "-qm", "late gitlink fixture")
    attribute_oid = _run(repository, "rev-parse", "HEAD:.gitattributes").decode().strip()
    read = _RealRead(runtime)
    expected = "git_attributes_unsupported" if unsafe_attribute else "git_tree_unsupported"
    _reject(safe_configuration_recipe(repository, checkpoint=_noop), read, expected)
    _assert_reads(read.reads, repository, (*_SAFE_ORDER, ("cat-file", "blob", attribute_oid)))


def test_real_git_benign_attribute_blobs_preserve_original_read_order(
    repository: Path, runtime: GitDeliveryRuntime
) -> None:
    _commit_file(repository, ".gitattributes", b"*.txt text\n")
    _commit_file(repository, "nested/.gitattributes", b"*.sh -text\n")
    first = _run(repository, "rev-parse", "HEAD:.gitattributes").decode().strip()
    second = _run(repository, "rev-parse", "HEAD:nested/.gitattributes").decode().strip()
    read = _RealRead(runtime)
    result = drive_git_repository_recipe(
        safe_configuration_recipe(repository, checkpoint=_noop), read, checkpoint=_noop
    )
    assert result is None
    _assert_reads(
        read.reads,
        repository,
        (*_SAFE_ORDER, ("cat-file", "blob", first), ("cat-file", "blob", second)),
    )
    assert _run(repository, *_STATUS) == b""


def test_real_git_alternates_rejected(
    repository: Path, runtime: GitDeliveryRuntime, tmp_path: Path
) -> None:
    alternate_objects = tmp_path / "alternate-objects"
    alternate_objects.mkdir()
    (repository / ".git/objects/info/alternates").write_text(
        str(alternate_objects), encoding="utf-8"
    )
    read = _RealRead(runtime)
    _reject(_binding_recipe(repository), read, "git_alternates_unsupported")
    _assert_reads(read.reads, repository, _BINDING_ORDER[:9])


@pytest.mark.parametrize("target", ["root", "common", "late-alternates"])
def test_real_git_physical_binding_changes_rejected_at_final_check(
    repository: Path, runtime: GitDeliveryRuntime, target: str
) -> None:
    read = _RealRead(runtime)

    def change_after_version(request: GitRepositoryRead) -> bytes:
        body = read(request)
        if request.arguments == _VERSION:
            if target == "late-alternates":
                (repository / ".git/objects/info/alternates").write_bytes(b"")
            else:
                path = repository if target == "root" else repository / ".git"
                path.rename(path.with_name(path.name + "-original"))
                path.mkdir()
        return body

    _reject(_binding_recipe(repository), change_after_version, "git_repository_changed")
    _assert_reads(read.reads, repository, _BINDING_ORDER)
