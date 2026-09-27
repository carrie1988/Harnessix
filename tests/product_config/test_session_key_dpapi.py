"""DPAPI调用合同替身：只验ABI使用、标志和回收，不能替代Windows真实API。"""

from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config import session_key_dpapi as api


@pytest.mark.parametrize("unprotect", [False, True])
@pytest.mark.parametrize("case", ["success", "failed", "oversized"])
def test_dpapi_uses_user_scope_and_clears_native_owned_buffer(monkeypatch, unprotect, case):
    buffer = ctypes.create_string_buffer(b"mock-owned-result")
    calls, frees = [], []

    def native(source, description, entropy, reserved, prompt, flags, target):
        assert description is reserved is prompt is None
        assert flags == 0x4  # UI_FORBIDDEN且没有LOCAL_MACHINE。
        assert ctypes.cast(source, ctypes.POINTER(api._Blob)).contents.size == 9
        assert ctypes.cast(entropy, ctypes.POINTER(api._Blob)).contents.size > 0
        calls.append(1)
        out = ctypes.cast(target, ctypes.POINTER(api._Blob)).contents
        out.data = ctypes.addressof(buffer)
        out.size = 8193 if case == "oversized" else len(buffer) - 1
        return 0 if case == "failed" else 1

    crypt = SimpleNamespace(CryptProtectData=native, CryptUnprotectData=native)
    kernel = SimpleNamespace(LocalFree=lambda value: frees.append(value))
    monkeypatch.setattr(api, "_api", lambda: (crypt, kernel))
    if case == "success":
        assert api.transform(b"input-key", unprotect=unprotect) == b"mock-owned-result"
        assert not any(buffer.raw)
    else:
        with pytest.raises(KernelError) as caught:
            api.transform(b"input-key", unprotect=unprotect)
        assert caught.value.code == "publication_key_unavailable"
    assert calls == [1] and len(frees) == 1
