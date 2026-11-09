"""共用DTO必须保持同一类、已有冻结Schema和任意入口的独立导入。"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.execution import public_tool_contracts
from harnessix.execution.public_tool_contracts import (
    MAX_WORKSPACE_PATCH_DIFF_PREVIEW_BYTES,
    WORKSPACE_PATCH_DIFF_PREVIEW_MARKER,
    PublicWorkspacePatchOutput,
    PublicWorkspacePatchOutputV2,
)

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
        ("PublicWorkspacePatchOutput", "workspace-patch-output-v1.schema.json"),
        ("PublicWorkspacePatchOutputV2", "workspace-patch-output-v2.schema.json"),
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


DIFF = "diff --harnessix a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"


def validate(model, body):
    """生产边界以严格JSON模式验证（UUID字符串、数组）。"""
    return model.model_validate_json(json.dumps(body, ensure_ascii=False))


def patch_v2() -> dict:
    return {
        "spec_version": "harnessix.workspace-patch-output/v2",
        "transaction_id": "00000000-0000-4000-8000-000000000001",
        "files": 2,
        "state": "published",
        "origin": "execution",
        "diff_sha256": hashlib.sha256(DIFF.encode()).hexdigest(),
        "changes": [
            {"path": "a.py", "content_sha256": "a" * 64},
            {"path": "gone.txt", "content_sha256": None},
        ],
        "diff_preview": {"text": DIFF, "total_bytes": len(DIFF), "truncated": False},
    }


def truncated_preview(shown: str, total: int) -> dict:
    return {
        "text": shown + WORKSPACE_PATCH_DIFF_PREVIEW_MARKER,
        "total_bytes": total,
        "truncated": True,
    }


def test_patch_output_v2_keeps_the_five_fields_and_adds_per_file_digests_and_preview():
    checked = validate(PublicWorkspacePatchOutputV2, patch_v2())
    assert [change.content_sha256 for change in checked.changes] == ["a" * 64, None]
    assert checked.diff_preview.text == DIFF
    five = {
        key: value
        for key, value in patch_v2().items()
        if key not in {"spec_version", "changes", "diff_preview"}
    }
    validate(PublicWorkspacePatchOutput, five)
    with pytest.raises(ValidationError):
        validate(PublicWorkspacePatchOutput, patch_v2())
    with pytest.raises(ValidationError):
        validate(PublicWorkspacePatchOutputV2, five)


def test_patch_output_v2_accepts_a_truncated_preview_with_the_fixed_marker():
    body = patch_v2()
    body["diff_preview"] = truncated_preview("x" * 100 + "\n", 10_000)
    validate(PublicWorkspacePatchOutputV2, body)
    limit = MAX_WORKSPACE_PATCH_DIFF_PREVIEW_BYTES - len(
        WORKSPACE_PATCH_DIFF_PREVIEW_MARKER.encode()
    )
    body["diff_preview"] = truncated_preview("x" * limit, 10_000)
    validate(PublicWorkspacePatchOutputV2, body)


@pytest.mark.parametrize(
    "case",
    [
        "count",
        "duplicate-path",
        "bad-digest",
        "extra-change-field",
        "full-preview-wrong-sha",
        "full-preview-wrong-size",
        "truncated-without-marker",
        "truncated-not-shorter",
        "oversize",
        "multibyte-oversize",
        "missing-preview",
        "extra",
        "spec-version",
    ],
)
def test_patch_output_v2_rejects_inconsistent_or_unbounded_bodies(case):
    body = copy.deepcopy(patch_v2())
    limit = MAX_WORKSPACE_PATCH_DIFF_PREVIEW_BYTES
    if case == "count":
        body["files"] = 3
    elif case == "duplicate-path":
        body["changes"][1]["path"] = "a.py"
    elif case == "bad-digest":
        body["changes"][0]["content_sha256"] = "A" * 64
    elif case == "extra-change-field":
        body["changes"][0]["expected_sha256"] = "a" * 64
    elif case == "full-preview-wrong-sha":
        body["diff_preview"]["text"] = DIFF.replace("new", "NEW")
    elif case == "full-preview-wrong-size":
        body["diff_preview"]["total_bytes"] += 1
    elif case == "truncated-without-marker":
        body["diff_preview"] = {"text": "x\n", "total_bytes": 10_000, "truncated": True}
    elif case == "truncated-not-shorter":
        body["diff_preview"] = truncated_preview("x" * 10, 10)
    elif case == "oversize":
        body["diff_preview"] = truncated_preview("x" * limit, 10 * limit)
    elif case == "multibyte-oversize":
        body["diff_preview"] = truncated_preview("中" * (limit // 3), 10 * limit)
    elif case == "missing-preview":
        del body["diff_preview"]
    elif case == "extra":
        body["artifact_path"] = "/private"
    else:
        body["spec_version"] = "harnessix.workspace-patch-output/v3"
    with pytest.raises(ValidationError):
        validate(PublicWorkspacePatchOutputV2, body)
