import hashlib
import json
from pathlib import Path

import pytest

from harnessix.patches.batch_approval_contracts import (
    ManagedPatchBatchApproval,
    ManagedPatchBatchPlan,
)
from harnessix.patches.batch_contracts import PatchBatchManifest, PatchBatchProposal
from harnessix.patches.batch_run_contracts import BatchExecutionResult, BatchRunRecord
from harnessix.patches.bridge_contracts import ManagedPatchCallPlan, ManagedPatchOutput
from harnessix.patches.contracts import PatchManifest, PatchProposal
from harnessix.patches.diff_contracts import PatchBatchDiff, PatchDiffOptions
from harnessix.patches.managed_contracts import CopyManifest, PatchRecord


@pytest.mark.parametrize(
    "name,model,frozen",
    [
        (
            "managed-patch-batch-run",
            BatchRunRecord,
            "ba8333292fefb9798faceb1d3c6e53d50ba2076fea92c6f5f95fd05a2efded20",
        ),
        (
            "managed-patch-batch-result",
            BatchExecutionResult,
            "fa81e299344e970de00c3d9dcd495ae532459c51877ad6c3e6b1089aae7b5f10",
        ),
        (
            "managed-patch-batch-plan",
            ManagedPatchBatchPlan,
            "1a64d775c1451ab5a870a7ec23e308efe8be7e33881851ae5cd2de61b67f855b",
        ),
        (
            "managed-patch-batch-approval",
            ManagedPatchBatchApproval,
            "854cd82d3e5a1e773f219fde676aa07e5a24bb86e82ab0215d59c388b00dd8f8",
        ),
        (
            "patch-batch-proposal",
            PatchBatchProposal,
            "25ec75edf9019b1527509e7f37934ed509615137e414842a80461f43a4142bb5",
        ),
        (
            "patch-batch-manifest",
            PatchBatchManifest,
            "01bf8b7827f67f9ae175c9745fba16a470282f64dafaa6f8395ecedb2d39dd8b",
        ),
        (
            "patch-batch-diff",
            PatchBatchDiff,
            "2503ec39a29357c4fa903cd9d5ea0d108bf5b1276bab11820a1484306278ede9",
        ),
        (
            "patch-diff-options",
            PatchDiffOptions,
            "092ead1868e05e12c372a50c2fba0070b0fda6f0e4187e3c751b9c736d608d22",
        ),
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
        (
            "managed-copy-manifest",
            CopyManifest,
            "ae163cace0167213b90eaa0042aa73da82b45b346813db58aaae6a3e463433fc",
        ),
        (
            "managed-patch-record",
            PatchRecord,
            "d9f281f110cb51a770c3eb87b88a2f96ec4a70f6f2ae6e33a55ed03ca2a576fc",
        ),
        (
            "managed-patch-call-plan",
            ManagedPatchCallPlan,
            "9004dc1128e5f9245e45297598d63fc38dc6a7392be713718b6cdce78f8ee444",
        ),
        (
            "managed-patch-output",
            ManagedPatchOutput,
            "eda2efd9a6f6b21b183d6229a12726afd5602bfc072d95cd4be29dcbadc486f7",
        ),
    ],
)
def test_frozen_patch_plan_schema(name, model, frozen):
    path = Path(__file__).parents[2] / "spec" / f"{name}-v1.schema.json"
    assert json.loads(path.read_text()) == model.model_json_schema()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == frozen
