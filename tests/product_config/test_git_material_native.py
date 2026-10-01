"""对象输入的配置解释及 Windows 真实共享约束；skip 不计原生证据。"""

from __future__ import annotations

import asyncio
import ctypes
import os
from contextlib import ExitStack

import pytest

from harnessix.delivery.git_material_input_contracts import GitMaterialInputError
from harnessix.delivery.git_material_native import _configuration, _Resources
from harnessix.delivery.git_material_native_windows import _api_path, _Windows
from tests.product_config import test_git_material_input as input_tests

make_process = input_tests.make_process


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("suffix", [" # 注释", " ; 注释", "#注释", ";注释"])
@pytest.mark.parametrize("quoted", [False, True])
def test_legitimate_format_and_only_outside_quote_comments(object_format, suffix, quoted):
    value = f'"{object_format}"' if quoted else object_format
    body = f"[extensions] # section\nobjectFormat = {value}{suffix}\n".encode()
    _configuration(body, object_format)


@pytest.mark.parametrize(
    "value",
    [
        '"sha#256"',
        '"sha;256"',
        '"sha256',
        'sha256"',
        '"sha256"extra',
        "sha256 # comment\ncompatObjectFormat = sha1",
    ],
)
def test_quotes_and_compatibility_cannot_hide_disallowed_format(value):
    with pytest.raises(GitMaterialInputError):
        _configuration(f"[extensions]\nobjectFormat={value}\n".encode(), "sha256")


@pytest.mark.parametrize("section", ["include", 'includeIf "gitdir:x#y"', 'filter "x#y"'])
def test_quoted_subsection_comment_never_hides_forbidden_section(section):
    with pytest.raises(GitMaterialInputError):
        _configuration(f"[{section}] # comment\npath=outside\n".encode(), "sha1")


async def test_real_sha256_repository_with_quoted_inline_comment(make_process, tmp_path):
    case = make_process(output_redaction=input_tests._Protection())
    input_tests._repository(case, tmp_path, "sha256")
    config = case.workspace / ".git/config"
    body = await asyncio.to_thread(config.read_bytes)
    assert b"objectformat = sha256" in body.lower()
    # 原Git同时解析此正式语法，重取绑定后完整C0写入不可误拒绝。
    case.runner.run(case.workspace, ("config", "extensions.objectFormat", "sha256"))
    body = await asyncio.to_thread(config.read_bytes)
    for original in (b"objectformat = sha256", b"objectFormat = sha256"):
        body = body.replace(original, b'objectformat = "sha256" # valid inline comment')
    await asyncio.to_thread(config.write_bytes, body)
    # 从原领域再次读取实际Git语义绑定，不手工构造或放宽摘要。
    binding = input_tests._repository_binding_existing(case, tmp_path)
    prepared = input_tests._prepare(
        case, binding, input_tests._material(b"comment-body", "sha256", "blob")
    )
    completion = await input_tests.process_tests._run(case, prepared)
    assert completion.input_proof.object_id == prepared.write.material.object_id


@pytest.mark.skipif(os.name != "nt", reason="需要真实 Windows NTFS 共享检查")
@pytest.mark.parametrize("directory", [False, True])
def test_windows_data_read_guard_rejects_write_rename_delete_with_metadata_control(
    tmp_path,
    directory,
):
    path = tmp_path / "guarded"
    if directory:
        path.mkdir()
    else:
        path.write_bytes(b"guarded-body")
    renamed = path.with_name("renamed")
    api = _Windows()
    with ExitStack() as metadata:
        handle = api.kernel.CreateFileW(
            _api_path(path),
            0x80,
            1,
            None,
            3,
            0x2200000,
            None,
        )
        assert handle is not None and handle != ctypes.c_void_p(-1).value
        metadata.callback(api.kernel.CloseHandle, handle)
        if not directory:
            descriptor = os.open(path, os.O_WRONLY | os.O_BINARY)
            os.close(descriptor)
        path.rename(renamed)
        renamed.rename(path)
    with _Resources() as resources:
        api.open(path, resources, directory=directory)
        if not directory:
            with pytest.raises(PermissionError):
                descriptor = os.open(path, os.O_WRONLY | os.O_BINARY)
                os.close(descriptor)
        with pytest.raises(PermissionError):
            path.rename(renamed)
        with pytest.raises(PermissionError):
            path.rmdir() if directory else path.unlink()
        assert path.exists() and not renamed.exists()
    path.rename(renamed)
    renamed.rmdir() if directory else renamed.unlink()
