"""定义视图的显式目录版本、默认兼容及验证先于I/O。"""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.tools import files
from harnessix.tools.contracts import ReadFileInputV4, ReadFileViewOutput
from harnessix.tools.runtime import CodingToolRuntime
from tests.tools.test_files import call, execute


async def test_definition_catalog_is_explicit_and_preserves_text_payload(tmp_path):
    body = b"def target(value):\r\n    return value\r\n"
    (tmp_path / "source.py").write_bytes(body)
    async with CodingToolRuntime(tmp_path) as legacy:
        old = await execute(legacy, path="source.py")
        old_call = call(legacy, "read_file", path="source.py")
        old_descriptors = {d.name: d for d in legacy.definitions()}
    async with CodingToolRuntime(tmp_path, enable_definition_views=True) as candidate:
        descriptors = {d.name: d for d in candidate.definitions()}
        assert descriptors["read_file"].version.startswith("3.")
        assert old_descriptors["read_file"].version.startswith("2.")
        assert descriptors["read_file"].input_schema == ReadFileInputV4.model_json_schema()
        assert all(descriptors[n] == d for n, d in old_descriptors.items() if n != "read_file")
        with pytest.raises(KernelError):
            await candidate.execute(old_call, CancelToken())
        text = await execute(candidate, path="source.py", view="text")
        definition = await execute(
            candidate, path="source.py", view="definition", anchor="def target(value):"
        )
    assert text.output == old.output and "definition" not in text.output
    assert definition.outcome == "succeeded"
    output = ReadFileViewOutput.model_validate_json(json.dumps(definition.output))
    assert output.definition.header.text == "def target(value):\r\n"
    assert output.text == "    return value\r\n"
    assert output.content_sha256 == hashlib.sha256(body).hexdigest()
    assert (tmp_path / "source.py").read_bytes() == body


@pytest.mark.parametrize("invalid", [None, 1, "true", []])
def test_definition_flag_is_validated_before_opening_workspace(tmp_path, invalid):
    with pytest.raises(KernelError) as error:
        CodingToolRuntime(tmp_path / "missing", enable_definition_views=invalid)
    assert error.value.code == "tool_definition_policy_invalid"


@pytest.mark.parametrize(
    "arguments",
    [
        {"view": "definition"},
        {"view": "definition", "anchor": "def x", "lines_before": 1},
        {"view": "definition", "anchor": "def x", "start_line": 2},
        {"view": "auto", "anchor": "def x"},
        {"view": True},
    ],
)
async def test_invalid_definition_input_never_reads_source(tmp_path, monkeypatch, arguments):
    def forbidden(*_args):
        pytest.fail("非法合同不得读取文件")

    monkeypatch.setattr(files, "read_file_snapshot", forbidden)
    async with CodingToolRuntime(tmp_path, enable_definition_views=True) as runtime:
        result = await execute(runtime, path="source.py", **arguments)
    assert result.outcome == "failed" and result.error.code == "tool_invalid_arguments"


@pytest.mark.parametrize("target", ["hidden.py", "linked.py", "../outside.py"])
async def test_definition_view_cannot_bypass_workspace_authority(tmp_path, target):
    (tmp_path / "hidden.py").write_text("def target():\n    return 1\n")
    (tmp_path / "linked.py").symlink_to("hidden.py")
    async with CodingToolRuntime(
        tmp_path, denied_paths=("hidden.py",), enable_definition_views=True
    ) as runtime:
        result = await execute(runtime, path=target, view="definition", anchor="def target")
    assert result.outcome == "failed" and result.output is None


def test_definition_schemas_are_frozen_without_modifying_old_contract():
    root = Path(__file__).parents[2] / "spec"
    for name, model in (
        ("read-file-input-v4.schema.json", ReadFileInputV4),
        ("read-file-view-output.schema.json", ReadFileViewOutput),
    ):
        assert json.loads((root / name).read_text()) == model.model_json_schema()
    with pytest.raises(ValidationError):
        ReadFileInputV4(path="x.py", view="definition")
