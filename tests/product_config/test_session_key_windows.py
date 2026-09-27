"""Windows实际用户DPAPI、DACL和硬链接；非Windows明确跳过，不当作验收。"""

from __future__ import annotations

import os
import subprocess

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config.session_key_dpapi import transform
from harnessix.product_config.session_key_store import load_session_key

pytestmark = pytest.mark.skipif(os.name != "nt", reason="需要真实Windows原生API")


def test_native_user_dpapi_roundtrip_and_corrupt_ciphertext_are_not_plaintext_fallback():
    value = b"native-user-key-fixture" * 3
    sealed = transform(value, unprotect=False)
    assert sealed != value and value not in sealed
    assert transform(sealed, unprotect=True) == value
    damaged = bytearray(sealed)
    damaged[len(damaged) // 2] ^= 0x80
    with pytest.raises(KernelError) as caught:
        transform(bytes(damaged), unprotect=True)
    assert caught.value.code == "publication_key_unavailable"


def test_native_key_is_private_ciphertext_and_reopens_with_same_user(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    first, second = load_session_key(root), load_session_key(root)
    try:
        raw = (root / "session-auth/key.v1").read_bytes()
        assert raw.startswith(b"HXKW\x01") and bytes(first.key) not in raw
        assert (first.store_id, first.key_id) == (second.store_id, second.key_id)
        assert first.key == second.key
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("target", ["key.v1", "."])
def test_native_world_read_acl_is_rejected_without_silent_repair(tmp_path, target):
    root = tmp_path / "state"
    root.mkdir()
    load_session_key(root).close()
    key = root / "session-auth/key.v1"
    original = key.read_bytes()
    result = subprocess.run(
        ["icacls", str(root / "session-auth" / target), "/grant", "*S-1-1-0:(R)", "/q"],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    with pytest.raises(KernelError) as caught:
        load_session_key(root)
    assert caught.value.code == "publication_key_unavailable"
    assert key.read_bytes() == original


def test_native_hardlinked_key_is_rejected(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    load_session_key(root).close()
    key = root / "session-auth/key.v1"
    original = key.read_bytes()
    os.link(key, tmp_path / "alias")
    with pytest.raises(KernelError) as caught:
        load_session_key(root)
    assert caught.value.code == "publication_key_unavailable"
    assert key.read_bytes() == original


def test_native_junction_key_directory_never_enrolls_target(tmp_path):
    root, target = tmp_path / "state", tmp_path / "target"
    root.mkdir()
    target.mkdir()
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / "session-auth"), str(target)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    with pytest.raises(KernelError) as caught:
        load_session_key(root)
    assert caught.value.code == "publication_key_unavailable"
    assert not list(target.iterdir())
