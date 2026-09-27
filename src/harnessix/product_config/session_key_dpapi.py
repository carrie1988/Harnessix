"""Windows当前用户非交互DPAPI；认证失败不降级，OS输出释放前清零。"""

from __future__ import annotations

import ctypes
import os
from typing import Any

from harnessix.product_config.session_key_codec import MAX_KEY_FILE_BYTES, unavailable


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.c_void_p)]


def _api() -> tuple[Any, Any]:
    if os.name != "nt":
        raise unavailable()
    crypt = ctypes.__dict__["WinDLL"]("crypt32", use_last_error=True)
    kernel = ctypes.__dict__["WinDLL"]("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    for name in ("CryptProtectData", "CryptUnprotectData"):
        function = getattr(crypt, name)
        function.argtypes = [
            ctypes.POINTER(_Blob),
            pointer,
            ctypes.POINTER(_Blob),
            pointer,
            pointer,
            ctypes.c_uint32,
            ctypes.POINTER(_Blob),
        ]
        function.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    return crypt, kernel


def transform(body: bytes, *, unprotect: bool) -> bytes:
    """固定应用用途熵不是额外Secret；只使用用户作用域与UI_FORBIDDEN。"""
    if type(body) is not bytes or not 1 <= len(body) <= MAX_KEY_FILE_BYTES:
        raise unavailable()
    crypt, kernel = _api()
    buffer = ctypes.create_string_buffer(body, len(body))
    entropy = ctypes.create_string_buffer(b"harnessix.session-key/v1\x00")
    source = _Blob(len(body), ctypes.cast(buffer, ctypes.c_void_p))
    extra = _Blob(len(entropy) - 1, ctypes.cast(entropy, ctypes.c_void_p))
    result = _Blob()
    try:
        function = crypt.CryptUnprotectData if unprotect else crypt.CryptProtectData
        # 不使用LOCAL_MACHINE，不提供PromptStruct，也不为失败重试其他身份或明文。
        if not function(
            ctypes.byref(source), None, ctypes.byref(extra), None, None, 0x4, ctypes.byref(result)
        ):
            raise unavailable()
        if not result.data or not 1 <= result.size <= MAX_KEY_FILE_BYTES:
            raise unavailable()
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(ctypes.addressof(buffer), 0, len(buffer))
        if result.data:
            if result.size <= MAX_KEY_FILE_BYTES:
                ctypes.memset(result.data, 0, result.size)
            kernel.LocalFree(result.data)
