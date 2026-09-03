from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.artifacts.contracts import (
    ArtifactPage,
    ArtifactPolicy,
    ArtifactRef,
    ReadArtifactInput,
)
from harnessix.domain.models import utc_now
from harnessix.tools.search_contracts import ArchivedGlobOutput, ArchivedGrepOutput


@pytest.mark.parametrize(
    "name,model,frozen",
    [
        (
            "artifact-ref",
            ArtifactRef,
            "d1945eb40800c40093e763266c8e5a36855b6c00295fdbd8a1731bb8903a506f",
        ),
        (
            "artifact-policy",
            ArtifactPolicy,
            "66feb581a6c734af8d39da49213185aa5066c0c531a9770d863bdb52d36662b5",
        ),
        (
            "read-artifact-input",
            ReadArtifactInput,
            "153964e26bab2951839fb7318312d3a20566f182efbf1993946d3a6a393ae211",
        ),
        (
            "read-artifact-output",
            ArtifactPage,
            "0ac493b51701b3d69a91a472cc466ac2c7e9074e984223a695c2b8fb90869345",
        ),
        (
            "archived-glob-output",
            ArchivedGlobOutput,
            "acda1498a694ed7d7981beca4c552824ea3826c378f735e86361c1ac3f893c15",
        ),
        (
            "archived-grep-output",
            ArchivedGrepOutput,
            "14f4451b3bb6dba4416932c64f9fc5440cda2938918881b432d6ed735ee875b6",
        ),
    ],
)
def test_frozen_artifact_schema(name, model, frozen):
    path = Path(__file__).parents[2] / "spec" / f"{name}-v1.schema.json"
    assert json.loads(path.read_text()) == model.model_json_schema()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == frozen


@pytest.mark.parametrize(
    "field,value",
    [
        ("ttl_seconds", 59),
        ("ttl_seconds", 604801),
        ("ttl_seconds", True),
        ("max_live_bytes", 0),
        ("max_live_bytes", "1024"),
        ("max_turn_count", 1001),
        ("max_turn_bytes", 0),
        ("max_manifests", 100001),
        ("extra", 1),
    ],
)
def test_policy_is_strict_and_bounded(field, value):
    with pytest.raises(ValidationError):
        ArtifactPolicy(**{field: value})


@pytest.mark.parametrize(
    "change",
    [
        {"thread_id": str(uuid4())},
        {"offset": True},
        {"limit": 201},
        {"limit": "1"},
        {"artifact_id": "../../s.db"},
    ],
)
def test_read_input_rejects_scope_and_coercion(change):
    data = {"artifact_id": str(uuid4()), **change}
    with pytest.raises(ValidationError):
        ReadArtifactInput.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    "text,offset,next_offset",
    [
        ("{}", 0, 1),
        ("{}\n", 0, None),
        ("", 0, 1),
        ("{}\n", 0, 2),
        ("{}\n", 2, None),
        ("{}\n", 0, True),
        ("中" * 9000 + "\n", 0, 1),
    ],
)
def test_page_checks_complete_record_byte_and_cursor_bounds(text, offset, next_offset):
    ref = ArtifactRef(
        artifact_id=uuid4(),
        sha256="0" * 64,
        size_bytes=6,
        records=2,
        complete=True,
        expires_at=utc_now(),
    )
    with pytest.raises(ValidationError):
        ArtifactPage(artifact=ref, text=text, offset=offset, next_offset=next_offset)
