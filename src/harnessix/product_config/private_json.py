"""用户配置目录里的小型私有JSON：只允许当前用户读写，临时文件加原子替换写入。"""

from __future__ import annotations

import json
import os
from pathlib import Path


def write_private_json(path: Path, document: object) -> None:
    """父目录按0700创建；写入失败时不留下临时文件，原文件保持不变。"""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    body = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(body + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
