"""共用配方测试夹具；模拟输出只用于解析和拒绝边界。"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Generator
from contextlib import ExitStack
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.git import GitDeliveryRuntime, git_delivery_implementation_digest
from harnessix.delivery.git_repository_recipe import (
    GitRepositoryRead,
    GitRepositoryRecipe,
    drive_git_repository_recipe,
    repository_binding_recipe,
)
from harnessix.delivery.git_store import SQLiteGitDeliveryStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.execution.contracts import canonical_digest
from harnessix.workspace.leases import WorkspaceLeaseStore
from tests.delivery.test_git import _git, _repository, _run

_ROOT = ("rev-parse", "--show-toplevel")


_COMMON = ("rev-parse", "--git-common-dir")


_INCLUDE = ("config", "--local", "--no-includes", "--null", "--get-regexp", r"^include(If)?\.")


_FILTER = (
    "config",
    "--includes",
    "--null",
    "--get-regexp",
    r"^filter\..*\.(clean|smudge|process)$",
)


_SPARSE = ("config", "--bool", "--get", "core.sparseCheckout")


_TREE = ("ls-tree", "-r", "-z", "--full-tree", "HEAD")


_HEAD = ("rev-parse", "--verify", "HEAD^{commit}")


_HEAD_TREE = ("rev-parse", "--verify", "HEAD^{tree}")


_STATUS = ("status", "--porcelain=v2", "--untracked-files=all", "-z")


_CONFIG = ("config", "--includes", "--null", "--list", "--show-origin")


_VERSION = ("version",)


_SAFE_ORDER = (_INCLUDE, _FILTER, _SPARSE, _TREE)


_BINDING_ORDER = (_ROOT, *_SAFE_ORDER, _HEAD, _HEAD_TREE, _STATUS, _COMMON, _CONFIG, _VERSION)


_WORKSPACE = "1" * 64


_EXECUTABLE = "2" * 64


_IMPLEMENTATION = "3" * 64


_BLOB_A = "a" * 40


_BLOB_B = "b" * 40


def _noop() -> None:
    pass


def _binding_recipe(
    root: Path,
    *,
    checkpoint: Callable[[], None] = _noop,
    executable_identity: str = _EXECUTABLE,
    implementation_digest: str = _IMPLEMENTATION,
) -> GitRepositoryRecipe:
    return repository_binding_recipe(
        root,
        _WORKSPACE,
        platform="windows" if os.name == "nt" else "posix",
        executable_identity=executable_identity,
        implementation_digest=implementation_digest,
        checkpoint=checkpoint,
    )


def _tree_record(path: bytes, *, oid: str = _BLOB_A) -> bytes:
    return b"100644 blob " + oid.encode("ascii") + b"\t" + path + b"\0"


def _assert_reads(reads: list[GitRepositoryRead], root: Path, order: tuple) -> None:
    assert tuple(request.arguments for request in reads) == order
    assert all(request.cwd == root.resolve() for request in reads)
    for request in reads:
        expected = (0, 1) if request.arguments in {_INCLUDE, _FILTER, _SPARSE} else (0,)
        assert request.accepted == expected


class _FixedRead:
    """仅向拒绝测试发送有界输出，并保留全部固定读取记录。"""

    def __init__(self, root: Path, overrides: dict[tuple[str, ...], bytes] | None = None):
        self.reads: list[GitRepositoryRead] = []
        self.responses = {
            _ROOT: str(root.resolve()).encode("utf-8") + b"\n",
            _INCLUDE: b"",
            _FILTER: b"",
            _SPARSE: b"",
            _TREE: b"",
            _HEAD: b"a" * 40 + b"\n",
            _HEAD_TREE: b"b" * 40 + b"\n",
            _STATUS: b"",
            _COMMON: b".git\n",
            _CONFIG: b"",
            _VERSION: b"git version fixture\n",
        }
        self.responses.update(overrides or {})

    def __call__(self, request: GitRepositoryRead) -> bytes:
        self.reads.append(request)
        assert request.arguments in self.responses, ("出现未授权的后续读取", request.arguments)
        body = self.responses[request.arguments]
        assert len(body) <= 1024 * 1024
        return body


class _RealRead:
    def __init__(self, runtime: GitDeliveryRuntime):
        self.runtime = runtime
        self.reads: list[GitRepositoryRead] = []
        self.outputs: dict[tuple[str, ...], bytes] = {}

    def __call__(self, request: GitRepositoryRead) -> bytes:
        self.reads.append(request)
        body = self.runtime._git.run(
            request.cwd, request.arguments, accepted=request.accepted
        ).stdout
        self.outputs[request.arguments] = body
        return body


class _FailCheckpoint:
    def __init__(self, at: int, error: BaseException):
        self.at = at
        self.error = error
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        if self.calls == self.at:
            raise self.error


@pytest.fixture
def parser_root(tmp_path: Path) -> Path:
    root = tmp_path / "parser-only"
    (root / ".git/objects/info").mkdir(parents=True)
    return root.resolve()


@pytest.fixture
def repository(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    root = tmp_path / "真实仓库"
    if getattr(request, "param", "sha1") == "sha1":
        _repository(root)
    else:
        root.mkdir()
        _run(root, "init", "--object-format=sha256", "-q")
        _run(root, "config", "user.name", "Harnessix Test")
        _run(root, "config", "user.email", "test@harnessix.invalid")
        _run(root, "config", "core.autocrlf", "false")
        _commit_file(root, "modify.txt", b"before\n")
    return root.resolve()


@pytest.fixture
def runtime(tmp_path: Path) -> Generator[GitDeliveryRuntime, None, None]:
    with ExitStack() as stack:
        workspace = stack.enter_context(SQLiteWorkspaceTransactionStore(tmp_path / "workspace"))
        git_store = stack.enter_context(SQLiteGitDeliveryStore(tmp_path / "git-state"))
        leases = stack.enter_context(WorkspaceLeaseStore(tmp_path / "leases.db"))
        yield GitDeliveryRuntime(workspace, git_store, leases, _git())


def _reject[T](recipe: GitRepositoryRecipe[T], read: Callable, code: str) -> KernelError:
    with pytest.raises(KernelError) as denied:
        drive_git_repository_recipe(recipe, read, checkpoint=_noop)
    assert denied.value.code == code
    assert recipe.gi_frame is None
    return denied.value


def _probe_recipe(events: list, root: Path) -> GitRepositoryRecipe[bytes]:
    try:
        first = yield GitRepositoryRead(root, _ROOT)
        events.append(("received", first))
        second = yield GitRepositoryRead(root, _COMMON)
        return second
    finally:
        events.append("closed")


def _physical_identity(path: Path) -> str:
    info = path.lstat()
    return canonical_digest(
        {
            "path": os.path.normcase(str(path.resolve()))
            if os.name == "nt"
            else str(path.resolve()),
            "device": info.st_dev,
            "inode": info.st_ino,
            "mode_type": info.st_mode & 0o170000,
        }
    )


def _expected_binding(runtime: GitDeliveryRuntime, root: Path, read: _RealRead) -> dict:
    common = (root / ".git").resolve()
    fields = {
        "spec_version": "harnessix.git-repository-binding/v1",
        "platform": "windows" if os.name == "nt" else "posix",
        "workspace_id": _WORKSPACE,
        "root_path_sha256": hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest(),
        "root_identity": _physical_identity(root),
        "common_directory_sha256": hashlib.sha256(
            os.path.normcase(str(common)).encode()
        ).hexdigest(),
        "common_directory_identity": _physical_identity(common),
        "head_oid": _run(root, *_HEAD).decode().strip(),
        "head_tree_oid": _run(root, *_HEAD_TREE).decode().strip(),
        "object_format": _run(root, "rev-parse", "--show-object-format").decode().strip(),
        "status_sha256": hashlib.sha256(b"").hexdigest(),
        "config_sha256": hashlib.sha256(read.outputs[_CONFIG]).hexdigest(),
        "git_executable_identity": runtime._git.identity,
        "git_version": _run(root, *_VERSION).decode().strip(),
        "implementation_digest": git_delivery_implementation_digest(),
    }
    return {**fields, "digest": canonical_digest(fields)}


def _commit_file(root: Path, name: str, body: bytes) -> None:
    path = root / name
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_bytes(body)
    _run(root, "add", "--", name)
    _run(root, "commit", "-qm", "control plane fixture")
