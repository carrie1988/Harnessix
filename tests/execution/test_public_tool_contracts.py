"""共用DTO必须保持同一类、已有冻结Schema和任意入口的独立导入。"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from harnessix.execution import public_tool_contracts

ROOT = Path(__file__).resolve().parents[2]
ALIASES = (
    ("harnessix.delivery.git_contracts", "GitPushReceipt"),
    ("harnessix.mcp.contracts", "McpToolCallOutput"),
    ("harnessix.skills.contracts", "SkillContent"),
    ("harnessix.skills.contracts", "SkillResourceContent"),
    ("harnessix.delivery.trusted_action_contracts", "PublicWorkspacePatchOutput"),
)
ENTRIES = (
    "harnessix.mcp",
    "harnessix.skills",
    "harnessix.trusted_actions",
    "harnessix.product_config",
    "harnessix.delivery.git_contracts",
    "harnessix.execution.public_tool_contracts",
)


def test_owner_contract_exports_reuse_one_canonical_class():
    for module, name in ALIASES:
        assert getattr(importlib.import_module(module), name) is getattr(
            public_tool_contracts, name
        )


def test_existing_output_schemas_are_unchanged():
    for name, filename in [
        ("GitPushReceipt", "git-push-receipt-v1.schema.json"),
        ("McpToolCallOutput", "mcp-tool-call-output-v1.schema.json"),
        ("SkillContent", "skill-content-v1.schema.json"),
        ("SkillResourceContent", "skill-resource-content-v1.schema.json"),
    ]:
        expected = json.loads((ROOT / "spec" / filename).read_bytes())
        assert getattr(public_tool_contracts, name).model_json_schema() == expected


@pytest.mark.parametrize("first", range(len(ENTRIES)))
def test_fresh_import_order_never_initializes_extension_runtime_through_contracts(first):
    order = (*ENTRIES[first:], *ENTRIES[:first])
    code = (
        "import importlib; modules="
        + repr(order)
        + "; [importlib.import_module(name) for name in modules]"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, timeout=15, check=False
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
