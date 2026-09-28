"""证明模型可见操作组合与原Workspace Patch解码边界一致。"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from harnessix.agent.approvals import tool_fingerprint
from harnessix.delivery.trusted_action import workspace_patch_descriptor
from harnessix.delivery.trusted_action_contracts import WorkspacePatchInput
from harnessix.domain.models import ToolDescriptor
from harnessix.models._anthropic_mapping import build_request as anthropic_request
from harnessix.models._chat_mapping import build_request as chat_request
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from tests.contracts.provider import model_request

_OMITTED = object()


@pytest.mark.parametrize("operation", ["create", "replace", "delete"])
def test_operation_field_matrix_matches_original_runtime_decoder(operation: str) -> None:
    """省略、null、合法值与非法值分别验证；不把必填字段设为默认值。"""

    validator = Draft202012Validator(workspace_patch_descriptor().input_schema)
    for sha, content, mode in itertools.product(
        [_OMITTED, None, "a" * 64, "invalid"],
        [_OMITTED, None, "", "new\n"],
        [_OMITTED, None, 420, 493, 777, True, "420"],
    ):
        file: dict[str, object] = {"operation": operation, "path": "a.py"}
        for key, value in (("expected_sha256", sha), ("content", content), ("mode", mode)):
            if value is not _OMITTED:
                file[key] = value
        proposal = {"files": [file]}
        try:
            WorkspacePatchInput.model_validate_json(json.dumps(proposal))
            decoded = True
        except ValidationError:
            decoded = False
        assert validator.is_valid(proposal) is decoded, file


def test_public_schema_exposes_each_operation_required_fields() -> None:
    schema = workspace_patch_descriptor().input_schema
    Draft202012Validator.check_schema(schema)
    file_schema = schema["$defs"]["WorkspacePatchFile"]
    assert file_schema["additionalProperties"] is False
    branches = {
        branch["properties"]["operation"]["const"]: branch for branch in file_schema["oneOf"]
    }
    assert set(branches) == {"create", "replace", "delete"}
    assert set(branches["create"]["required"]) == {"content", "mode"}
    assert set(branches["replace"]["required"]) == {"expected_sha256", "content", "mode"}
    assert set(branches["delete"]["required"]) == {"expected_sha256"}


def test_patch_descriptor_explains_complete_content_and_decimal_modes() -> None:
    description = workspace_patch_descriptor().description
    assert all(
        word in description for word in ("create", "replace", "delete", "mode", "420", "493")
    )
    assert "完整" in description and "Windows" in description
    assert "必填" in description and "十进制" in description


@pytest.mark.parametrize("adapter", ["chat", "anthropic"])
def test_actual_provider_payload_preserves_operation_schema(adapter: str) -> None:
    descriptor = workspace_patch_descriptor()
    request = model_request().model_copy(update={"tools": (descriptor,)})
    if adapter == "chat":
        body, _ = chat_request(request, OpenAIChatConfig(model="test", api_key_env="TEST_KEY"))
        published = body["tools"][0]["function"]["parameters"]
    else:
        body, _ = anthropic_request(request, AnthropicConfig(model="test", api_key_env="TEST_KEY"))
        published = body["tools"][0]["input_schema"]
    assert published == descriptor.input_schema
    assert "oneOf" in published["$defs"]["WorkspacePatchFile"]


def test_operation_schema_does_not_change_legacy_valid_json_or_tool_version() -> None:
    legacy = ToolDescriptor.model_validate_json(
        (
            Path(__file__).parent / "fixtures/workspace-patch-descriptor-pre-discovery-v1.json"
        ).read_bytes()
    )
    current = workspace_patch_descriptor()
    assert (
        tool_fingerprint(legacy)
        == "e8b12d190a387e5e35d891d4ea47f8a51fe858e0e6331e0740062ee94b76aeca"
    )
    assert current.name == legacy.name and current.version == legacy.version
    assert tool_fingerprint(current) != tool_fingerprint(legacy)
    assert current.effect_class == legacy.effect_class
    assert current.requires_approval and current.requires_idempotency
    assert current.supports_reconciliation and not current.supports_parallel_calls
    for operation in ("create", "replace", "delete"):
        file: dict[str, object] = {"operation": operation, "path": "a.py"}
        if operation != "create":
            file["expected_sha256"] = "a" * 64
        if operation != "delete":
            file.update(content="", mode=420)
        proposal = {"files": [file]}
        assert Draft202012Validator(legacy.input_schema).is_valid(proposal)
        decoded = WorkspacePatchInput.model_validate_json(json.dumps(proposal))
        canonical = decoded.model_dump(mode="json")
        assert decoded == WorkspacePatchInput.model_validate_json(json.dumps(canonical))
        assert canonical["files"][0]["mode"] == (None if operation == "delete" else 420)
