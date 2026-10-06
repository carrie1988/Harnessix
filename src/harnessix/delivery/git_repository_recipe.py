"""Git仓库固定读取配方；同步领域与受控异步产品共用校验，不执行进程。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_contracts import (
    GitRepositoryBinding,
    git_repository_binding_digest,
    validate_git_object_id,
)
from harnessix.delivery.git_identity import _identity, _path_sha256, _path_text
from harnessix.workspace.contracts import PlatformKind

_OID = re.compile(rb"^(?:[0-9a-f]{40}|[0-9a-f]{64})\n?$")
type GitRepositoryRecipe[T] = Generator[GitRepositoryRead, bytes, T]


@dataclass(frozen=True, slots=True)
class GitRepositoryRead:
    """模块内部生成的固定读取；不是模型参数、执行计划或批准凭证。"""

    cwd: Path = field(repr=False)
    arguments: tuple[str, ...] = field(repr=False)
    accepted: tuple[int, ...] = (0,)


def _text(body: bytes) -> str:
    try:
        return body.decode("utf-8", errors="strict")
    except UnicodeError:
        raise KernelError("git_output_invalid", "Git输出不是有效UTF-8") from None


def _oid(body: bytes) -> str:
    if _OID.fullmatch(body) is None:
        raise KernelError("git_output_invalid", "Git对象OID输出无效")
    return body.decode("ascii").strip()


def repository_root_recipe(
    supplied: str | Path, *, checkpoint: Callable[[], None]
) -> GitRepositoryRecipe[Path]:
    """先核验本机路径，再通过固定查询确认精确根；不接受子目录冒充根。"""
    checkpoint()
    try:
        path = Path(supplied)
        if not path.is_absolute() or any(ord(character) < 32 for character in str(path)):
            raise OSError
        root = path.resolve(strict=True)
        if not root.is_dir() or root.is_symlink():
            raise OSError
    except (OSError, RuntimeError):
        raise KernelError("git_repository_invalid", "路径不是精确Git仓库根") from None
    reported = _text((yield GitRepositoryRead(root, ("rev-parse", "--show-toplevel")))).strip()
    checkpoint()
    try:
        if (
            not reported
            or any(ord(character) < 32 or ord(character) == 127 for character in reported)
            or not Path(reported).is_absolute()
            or _path_text(Path(reported).resolve(strict=True)) != _path_text(root)
        ):
            raise OSError
    except (OSError, RuntimeError):
        raise KernelError("git_repository_invalid", "路径不是精确Git仓库根") from None
    return root


def common_directory_recipe(
    repository: Path, *, checkpoint: Callable[[], None]
) -> GitRepositoryRecipe[Path]:
    """保留Git报告的实际commonDir，不从根路径猜测.git目录。"""
    checkpoint()
    value = _text((yield GitRepositoryRead(repository, ("rev-parse", "--git-common-dir")))).strip()
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise KernelError("git_repository_invalid", "Git common directory无效")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = repository / candidate
    checkpoint()
    try:
        return candidate.resolve(strict=True)
    except OSError:
        raise KernelError("git_repository_invalid", "Git common directory无效") from None


def _attribute_oids(tree: bytes, checkpoint: Callable[[], None]) -> Iterator[str]:
    """按原条目顺序暂停并读属性，保留多重异常时的首次拒绝语义。"""
    if tree and not tree.endswith(b"\0"):
        raise KernelError("git_tree_invalid", "Git tree条目无法解析")
    for record in tree.split(b"\0"):
        checkpoint()
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split(" ", 2)
            path = raw_path.decode("utf-8", errors="strict")
        except (UnicodeError, ValueError):
            raise KernelError("git_tree_invalid", "Git tree条目无法解析") from None
        name = path.rsplit("/", 1)[-1].casefold()
        if mode == "160000" or kind == "commit" or name in {".gitmodules", ".lfsconfig"}:
            raise KernelError("git_tree_unsupported", "Git submodule或LFS控制面在0.7不受支持")
        try:
            validate_git_object_id(oid)
            if (mode, kind) not in {("100644", "blob"), ("100755", "blob"), ("120000", "blob")}:
                raise ValueError
            if not path:
                raise ValueError
        except ValueError:
            raise KernelError("git_tree_invalid", "Git tree条目无法解析") from None
        if name == ".gitattributes":
            yield oid


def safe_configuration_recipe(
    repository: Path, *, checkpoint: Callable[[], None]
) -> GitRepositoryRecipe[None]:
    """复用原include/filter/sparse/tree/attributes拒绝语义；不扩大配置能力。"""
    checkpoint()
    included = yield GitRepositoryRead(
        repository,
        ("config", "--local", "--no-includes", "--null", "--get-regexp", r"^include(If)?\."),
        (0, 1),
    )
    if included:
        raise KernelError("git_config_unsupported", "Git本地外部include配置在0.7不受支持")
    checkpoint()
    filters = yield GitRepositoryRead(
        repository,
        ("config", "--includes", "--null", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"),
        (0, 1),
    )
    if filters:
        raise KernelError("git_filter_unsupported", "Git可执行filter在0.7不受支持")
    checkpoint()
    sparse = yield GitRepositoryRead(
        repository, ("config", "--bool", "--get", "core.sparseCheckout"), (0, 1)
    )
    if sparse.strip() == b"true":
        raise KernelError("git_sparse_checkout_unsupported", "Git sparse checkout在0.7不受支持")
    checkpoint()
    tree = yield GitRepositoryRead(repository, ("ls-tree", "-r", "-z", "--full-tree", "HEAD"))
    for oid in _attribute_oids(tree, checkpoint):
        checkpoint()
        body = yield GitRepositoryRead(repository, ("cat-file", "blob", oid))
        lowered = body.lower()
        if b"filter" in lowered or b"working-tree-encoding" in lowered:
            raise KernelError("git_attributes_unsupported", "Git attributes包含转换规则")


def repository_binding_recipe(
    supplied: str | Path,
    workspace_id: str,
    *,
    platform: PlatformKind,
    executable_identity: str,
    implementation_digest: str,
    checkpoint: Callable[[], None],
) -> GitRepositoryRecipe[GitRepositoryBinding]:
    """完整读取后构造原合同；摘要不是原Session认证、业务批准或原子快照。"""
    repository = yield from repository_root_recipe(supplied, checkpoint=checkpoint)
    root_identity = _identity(repository, directory=True)
    yield from safe_configuration_recipe(repository, checkpoint=checkpoint)
    checkpoint()
    head = _oid((yield GitRepositoryRead(repository, ("rev-parse", "--verify", "HEAD^{commit}"))))
    tree = _oid((yield GitRepositoryRead(repository, ("rev-parse", "--verify", "HEAD^{tree}"))))
    object_format = "sha1" if len(head) == 40 else "sha256"
    checkpoint()
    status = yield GitRepositoryRead(
        repository, ("status", "--porcelain=v2", "--untracked-files=all", "-z")
    )
    if status:
        raise KernelError("delivery_dirty_conflict", "Git来源仓库不是干净状态")
    common = yield from common_directory_recipe(repository, checkpoint=checkpoint)
    common_identity = _identity(common, directory=True)
    alternates = common / "objects/info/alternates"
    if alternates.exists() or alternates.is_symlink():
        raise KernelError("git_alternates_unsupported", "Git alternates在0.7不受支持")
    checkpoint()
    config = yield GitRepositoryRead(
        repository, ("config", "--includes", "--null", "--list", "--show-origin")
    )
    version = _text((yield GitRepositoryRead(repository, ("version",)))).strip()
    checkpoint()
    if (
        root_identity != _identity(repository, directory=True)
        or common_identity != _identity(common, directory=True)
        or alternates.exists()
        or alternates.is_symlink()
    ):
        raise KernelError("git_repository_changed", "Git仓库绑定已经变化")
    candidate = GitRepositoryBinding.model_construct(
        _fields_set=None,
        platform=platform,
        workspace_id=workspace_id,
        root_path_sha256=_path_sha256(repository),
        root_identity=root_identity,
        common_directory_sha256=_path_sha256(common),
        common_directory_identity=common_identity,
        head_oid=head,
        head_tree_oid=tree,
        object_format=object_format,
        status_sha256=hashlib.sha256(status).hexdigest(),
        config_sha256=hashlib.sha256(config).hexdigest(),
        git_executable_identity=executable_identity,
        git_version=version,
        implementation_digest=implementation_digest,
        digest="0" * 64,
    )
    return GitRepositoryBinding(
        **candidate.model_dump(exclude={"digest"}),
        digest=git_repository_binding_digest(candidate),
    )


def drive_git_repository_recipe[T](
    recipe: GitRepositoryRecipe[T],
    read: Callable[[GitRepositoryRead], bytes],
    *,
    checkpoint: Callable[[], None],
) -> T:
    """旧同步入口的唯一驱动；校验在配方中，效果与异常仍由原Runner负责。"""
    try:
        checkpoint()
        try:
            request = next(recipe)
        except StopIteration as completed:
            return cast(T, completed.value)
        while True:
            checkpoint()
            body = read(request)
            checkpoint()
            try:
                request = recipe.send(body)
            except StopIteration as completed:
                return cast(T, completed.value)
    finally:
        recipe.close()
