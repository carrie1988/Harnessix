import hashlib
import json
from pathlib import Path

import pytest

from harnessix.patches.contracts import PatchManifest, PatchProposal


@pytest.mark.parametrize(
    "name,model,frozen",
    [
        (
            "patch-proposal",
            PatchProposal,
            "96a5a5ed3d7374925e27e0faf2a5be71353f59526bde86f7f5eca86e63aca663",
        ),
        (
            "patch-manifest",
            PatchManifest,
            "533159e0ecc55d06d1f18b8bd04528e44aad36bea9a63bb4fdde8c272d08d850",
        ),
    ],
)
def test_frozen_patch_plan_schema(name, model, frozen):
    path = Path(__file__).parents[2] / "spec" / f"{name}-v1.schema.json"
    assert json.loads(path.read_text()) == model.model_json_schema()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == frozen
