"""MCP Server信任记录（ADR 0126）：按Server保存用户确认时的定义指纹，定义一变即视为未信任。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from harnessix.product_config.private_json import write_private_json

_SPEC_VERSION = "harnessix.mcp-trust/v1"
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")

McpTrustState = Literal["trusted", "untrusted", "changed"]


def mcp_trust_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".harnessix" / "mcp-trust.json"


def load_mcp_trust(store: Path) -> dict[str, str]:
    """文件缺失或损坏都按没有信任记录处理：宁可不启动，也不放过无法核对的记录。"""

    try:
        document = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict) or document.get("spec_version") != _SPEC_VERSION:
        return {}
    servers = document.get("servers")
    if not isinstance(servers, dict):
        return {}
    return {
        server_id: fingerprint
        for server_id, fingerprint in servers.items()
        if isinstance(server_id, str)
        and isinstance(fingerprint, str)
        and _FINGERPRINT.fullmatch(fingerprint)
    }


def mcp_trust_state(server_id: str, fingerprint: str, records: dict[str, str]) -> McpTrustState:
    recorded = records.get(server_id)
    if recorded is None:
        return "untrusted"
    return "trusted" if recorded == fingerprint else "changed"


def trust_mcp_server(store: Path, server_id: str, fingerprint: str) -> None:
    records = load_mcp_trust(store) | {server_id: fingerprint}
    write_private_json(store, {"spec_version": _SPEC_VERSION, "servers": records})


def untrust_mcp_server(store: Path, server_id: str) -> bool:
    records = load_mcp_trust(store)
    if records.pop(server_id, None) is None:
        return False
    write_private_json(store, {"spec_version": _SPEC_VERSION, "servers": records})
    return True
