"""Windows元数据缺失的严格分类；密钥读取和其他原生错误不得被忽略。"""

from __future__ import annotations

import ctypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config.session_key_windows_files import WindowsKeyFiles


@pytest.fixture(params=[None, ctypes.c_void_p(-1).value])
def failed_files(request, monkeypatch):
    def make(code):
        files = object.__new__(WindowsKeyFiles)
        files.kernel = SimpleNamespace(CreateFileW=lambda *_: request.param)
        files._read_share = 1
        monkeypatch.setitem(ctypes.__dict__, "get_last_error", lambda: code)
        return files

    return make


@pytest.mark.parametrize("code", [2, 3])
def test_missing_metadata_is_typed_without_private_path(failed_files, code):
    with pytest.raises(FileNotFoundError) as caught:
        failed_files(code).open(Path("C:/private-fixture/sessions.db-shm"), metadata_only=True)
    assert caught.value.filename is None
    assert "private-fixture" not in str(caught.value)


@pytest.mark.parametrize("code", [2, 3, 5, 32, 33])
def test_key_data_read_keeps_original_fixed_refusal(failed_files, code):
    with pytest.raises(KernelError) as caught:
        failed_files(code).open(Path("C:/private-fixture/key.v1"))
    assert caught.value.code == "publication_key_unavailable"


@pytest.mark.parametrize("code", [5, 32, 33, 87])
def test_non_missing_metadata_error_is_never_lifecycle_disappearance(failed_files, code):
    with pytest.raises(KernelError) as caught:
        failed_files(code).open(Path("C:/private-fixture/sessions.db-shm"), metadata_only=True)
    assert caught.value.code == "publication_key_unavailable"


@pytest.mark.parametrize("code", [32, 33])
def test_exclusive_key_conflict_keeps_busy_code(failed_files, code):
    with pytest.raises(KernelError) as caught:
        failed_files(code).open(Path("C:/private-fixture/key.v1"), exclusive=True)
    assert caught.value.code == "publication_key_busy"
