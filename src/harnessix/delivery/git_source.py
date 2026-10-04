"""Git 来源结构校验：受信解析结果仍须属于原注册集合并具有真实回链。"""

from __future__ import annotations

import stat
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_identity import _path_text


def _link_bytes(path: Path) -> bytes:
    """只读取有界普通回链文件，拒绝符号链接和特殊文件。"""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size > 4096:
        raise OSError
    return path.read_bytes()


def read_worktree_links(root: Path, common: Path) -> tuple[bytes, Path, bytes]:
    """复核来源或交付 Worktree 的 gitfile、管理目录及双向回链。"""
    gitfile = root / ".git"
    body = _link_bytes(gitfile)
    prefix = b"gitdir: "
    if not body.startswith(prefix):
        raise OSError
    admin = Path(body[len(prefix) :].decode("utf-8", errors="strict").strip())
    if not admin.is_absolute():
        admin = root / admin
    if not stat.S_ISDIR(admin.lstat().st_mode):
        raise OSError
    admin = admin.resolve(strict=True)
    if admin.parent != common / "worktrees":
        raise OSError
    commondir = _link_bytes(admin / "commondir").decode("utf-8", errors="strict").strip()
    resolved_common = (admin / commondir).resolve(strict=True)
    backlink = _link_bytes(admin / "gitdir")
    backlink_path = Path(backlink.decode("utf-8", errors="strict").strip()).resolve(strict=True)
    if resolved_common != common or backlink_path != gitfile.resolve(strict=True):
        raise OSError
    return body, admin, backlink


def verify_git_source(root: Path, common: Path, registered: set[str]) -> None:
    """来源须保留真实 Git 结构和登记；裸路径或同内容副本不构成授权。"""
    try:
        gitfile = root / ".git"
        if stat.S_ISDIR(gitfile.lstat().st_mode):
            if gitfile.resolve(strict=True) != common:
                raise OSError
        else:
            read_worktree_links(root, common)
    except (OSError, RuntimeError, UnicodeError):
        raise KernelError("git_worktree_binding_invalid", "Git来源Worktree回链无效") from None
    if _path_text(root) not in registered:
        raise KernelError("git_repository_changed", "Git来源已不在Worktree注册集合")
