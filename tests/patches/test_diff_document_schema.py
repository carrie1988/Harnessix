import hashlib
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from harnessix.patches.diff_document_contracts import (
    BatchDiffDocument,
    BatchDiffDocumentOptions,
    BatchDiffRecord,
)


@pytest.mark.parametrize(
    "name,model,expected",
    [
        (
            "batch-diff-document",
            BatchDiffDocument,
            "6a4b1ffe712dfb3fc45fee9c35d404d24d3ce7d344689089d2274e7ff76d5930",
        ),
        (
            "batch-diff-document-options",
            BatchDiffDocumentOptions,
            "b96d3f8ee125795eb234035864b879a3f5f2721f3c209db48a2b7995cf4fcae0",
        ),
        (
            "batch-diff-record",
            BatchDiffRecord,
            "289ef336117a0f7f295ae7e4390f6ab79cacb29a83908d195692e8ab0f0dc2c7",
        ),
    ],
)
def test_frozen_document_contract(name, model, expected):
    path = Path(__file__).parents[2] / "spec" / f"{name}-v1.schema.json"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
    assert json.loads(path.read_text()) == TypeAdapter(model).json_schema()
