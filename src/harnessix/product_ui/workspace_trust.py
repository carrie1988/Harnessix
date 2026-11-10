"""工作区信任：首次在某目录运行前取得用户确认，避免恶意仓库的指令文件被直接加载。

确认结果记录在用户配置目录；沙箱内命令不能读写该目录。信任一个目录即信任其子目录。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from harnessix.product_config.private_json import write_private_json

TRUST_ENVIRONMENT = "HARNESSIX_TRUST_WORKSPACE"
_SPEC_VERSION = "harnessix.trusted-workspaces/v1"


def trust_store_path() -> Path:
    return Path.home() / ".harnessix" / "trusted-workspaces.json"


def _load(store: Path) -> tuple[Path, ...]:
    """读取已信任目录；文件缺失或损坏都按没有信任记录处理。"""

    try:
        document = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    if not isinstance(document, dict) or document.get("spec_version") != _SPEC_VERSION:
        return ()
    entries = document.get("workspaces")
    if not isinstance(entries, list):
        return ()
    return tuple(
        Path(item) for item in entries if isinstance(item, str) and Path(item).is_absolute()
    )


def is_trusted(workspace: Path, store: Path) -> bool:
    resolved = workspace.resolve()
    return any(resolved.is_relative_to(entry) for entry in _load(store))


def remember_trust(workspace: Path, store: Path) -> None:
    """用临时文件加原子替换写入，文件只允许当前用户读写。"""

    entries = {str(item) for item in _load(store)} | {str(workspace.resolve())}
    write_private_json(store, {"spec_version": _SPEC_VERSION, "workspaces": sorted(entries)})


def _notice(workspace: Path) -> str:
    return (
        f"Harnessix 将在这个目录工作：{workspace}\n"
        "- 目录里的 AGENTS.md 等指令文件会被读取，并发送给你配置的模型\n"
        "- 沙箱内的命令会自动执行，并可以修改这个目录里的文件\n"
        "只信任你自己的或来源可靠的代码。"
    )


def confirm_workspace_trust(
    workspace: Path,
    *,
    assume_trusted: bool = False,
    store: Path | None = None,
    environ: Mapping[str, str] | None = None,
    interactive: bool | None = None,
    read_answer: Callable[[str], str] = input,
) -> bool:
    """已信任或本次显式信任时返回True；交互确认后记录，非交互且未信任时返回False。"""

    if not workspace.is_dir():
        # 目录不存在时交给启动预检报告，这里不输出路径。
        return True
    target = store if store is not None else trust_store_path()
    variables = os.environ if environ is None else environ
    if assume_trusted or variables.get(TRUST_ENVIRONMENT) == "1" or is_trusted(workspace, target):
        return True
    if not (sys.stdin.isatty() if interactive is None else interactive):
        print(
            "当前工作区尚未被信任。请先在终端里运行 harnessix code 确认，"
            "或给 harnessix exec 加上 --trust-workspace。",
            file=sys.stderr,
        )
        return False
    print(_notice(workspace), file=sys.stderr)
    try:
        answer = read_answer("信任此目录并继续吗？[y/N]: ")
    except (EOFError, KeyboardInterrupt):
        return False
    if answer.strip().lower() not in {"y", "yes"}:
        return False
    remember_trust(workspace, target)
    return True
