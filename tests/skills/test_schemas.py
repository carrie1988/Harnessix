from __future__ import annotations

import json
from pathlib import Path

from harnessix.skills.contracts import (
    SkillAccessEvent,
    SkillCatalogSnapshot,
    SkillContent,
    SkillDiscoveryIssue,
    SkillLoadInput,
    SkillManifestSnapshot,
    SkillNameConflict,
    SkillResourceContent,
    SkillResourceReadInput,
    SkillSourceSnapshot,
)


def test_committed_skill_schemas_match_runtime_contracts() -> None:
    root = Path(__file__).parents[2] / "spec"
    expected = {
        "skill-source-snapshot-v1.schema.json": SkillSourceSnapshot.model_json_schema(),
        "skill-manifest-snapshot-v1.schema.json": SkillManifestSnapshot.model_json_schema(),
        "skill-discovery-issue-v1.schema.json": SkillDiscoveryIssue.model_json_schema(),
        "skill-name-conflict-v1.schema.json": SkillNameConflict.model_json_schema(),
        "skill-catalog-snapshot-v1.schema.json": SkillCatalogSnapshot.model_json_schema(),
        "skill-load-input-v1.schema.json": SkillLoadInput.model_json_schema(),
        "skill-resource-read-input-v1.schema.json": SkillResourceReadInput.model_json_schema(),
        "skill-content-v1.schema.json": SkillContent.model_json_schema(),
        "skill-resource-content-v1.schema.json": SkillResourceContent.model_json_schema(),
        "skill-access-event-v1.schema.json": SkillAccessEvent.model_json_schema(),
    }
    for name, schema in expected.items():
        assert json.loads((root / name).read_text(encoding="utf-8")) == schema
