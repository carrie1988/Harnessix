"""双平台模拟原生安全元数据；验证密钥精确合同与非密钥继承合同，不替代原生验收。"""

from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config.session_key_windows_security import PrivateKeySecurity
from harnessix.workspace.windows_private_security import (
    PrivateStateSecurity,
    _AclSize,
    _AllowedAce,
)


def _security(
    *,
    state=False,
    owner="user",
    protected=True,
    flags=0,
    principals=("user", "system"),
    kind=0,
    mask=0x1F01FF,
    null_acl=False,
):
    security = object.__new__(PrivateStateSecurity if state else PrivateKeySecurity)
    security.sid, security.default_owner = "user", "administrators"
    security._error = lambda: KernelError("fixture_acl_denied", "拒绝非私有元数据")
    identities = {index + 1: role for index, role in enumerate(principals)}
    aces = [
        _AllowedAce(
            kind=kind,
            flags=flags[index] if isinstance(flags, tuple) else flags,
            size=12,
            mask=mask,
            sid=index + 1,
        )
        for index in range(len(principals))
    ]
    freed = []

    def security_info(_handle, _type, _flags, actual_owner, _group, acl, _sacl, descriptor):
        ctypes.cast(actual_owner, ctypes.POINTER(ctypes.c_void_p))[0] = 100
        ctypes.cast(acl, ctypes.POINTER(ctypes.c_void_p))[0] = 0 if null_acl else 200
        ctypes.cast(descriptor, ctypes.POINTER(ctypes.c_void_p))[0] = 300
        return 0

    def control(_descriptor, output, revision):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint16))[0] = 0x1000 if protected else 0
        return True

    def acl_info(_acl, output, _size, _class):
        ctypes.cast(output, ctypes.POINTER(_AclSize)).contents.count = len(aces)
        return True

    def ace_info(_acl, index, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(aces[index])
        return True

    def sid_text(value):
        address = value.value if isinstance(value, ctypes.c_void_p) else value
        role = owner if address == 100 else identities[ctypes.c_uint32.from_address(address).value]
        return "S-1-5-18" if role == "system" else role

    security._sid_text = sid_text
    security.api = SimpleNamespace(
        GetSecurityInfo=security_info,
        GetSecurityDescriptorControl=control,
        GetAclInformation=acl_info,
        GetAce=ace_info,
    )
    security.kernel = SimpleNamespace(LocalFree=lambda pointer: freed.append(pointer.value))
    return security, freed


@pytest.mark.parametrize("protected,flags", [(True, 0), (True, 3), (False, 16), (False, 19)])
@pytest.mark.parametrize("owner", ["user", "administrators"])
def test_state_accepts_only_two_private_explicit_or_inherited_aces(protected, flags, owner):
    security, freed = _security(state=True, owner=owner, protected=protected, flags=flags)
    security.verify(10)
    assert freed == [300]


@pytest.mark.parametrize(
    "changes",
    [
        {"owner": "other"},
        {"null_acl": True},
        {"flags": (0, 3)},
        {"principals": ("user", "system", "administrators")},
        {"principals": ("user", "administrators")},
        {"principals": ("user", "everyone")},
        {"principals": ("user", "user")},
        {"principals": ("user",)},
        {"principals": ()},
        {"kind": 1},
        {"mask": 0x120089},
        {"flags": 8},
        {"flags": 16},
        {"protected": False},
        {"protected": False, "flags": 3},
        {"flags": 31},
    ],
)
def test_state_rejects_public_or_unsupported_forms_and_frees_descriptor(changes):
    security, freed = _security(state=True, **changes)
    with pytest.raises(KernelError):
        security.verify(10)
    assert freed == [300]


@pytest.mark.parametrize(
    "changes", [{"owner": "administrators"}, {"flags": 3}, {"protected": False, "flags": 16}]
)
def test_key_never_adopts_the_state_inheritance_or_default_owner_exception(changes):
    security, freed = _security(**changes)
    with pytest.raises(KernelError):
        security.verify(10)
    assert freed == [300]


def test_key_original_exact_contract_remains_valid():
    security, freed = _security()
    security.verify(10)
    assert freed == [300]


@pytest.mark.parametrize(
    "owner,protected,flags", [("administrators", True, 3), ("user", False, 19), ("user", True, 0)]
)
def test_running_root_requires_explicit_user_owner_and_inheritable_protected_acl(
    owner, protected, flags
):
    security, freed = _security(state=True, owner=owner, protected=protected, flags=flags)
    with pytest.raises(KernelError):
        security.verify_root(10, inheritable=True)
    assert freed == [300]


def test_explicit_private_running_root_is_valid():
    security, freed = _security(state=True, flags=3)
    security.verify_root(10, inheritable=True)
    assert freed == [300]


@pytest.mark.parametrize(
    "relative", ["session-auth/key.v1", "SESSION-AUTH/key.v1", "Session-Auth/.lock"]
)
def test_key_port_selection_is_case_insensitive_on_windows(relative):
    from harnessix.product_config.state_backup_files import PrivateStateTree

    tree = object.__new__(PrivateStateTree)
    tree._windows, tree._windows_keys = object(), object()
    assert tree._windows_port(relative) is tree._windows_keys
