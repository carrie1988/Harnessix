"""Darwin只缓存静态FFI绑定；每次ACL、errno和FD仍独立重新查询。"""

from __future__ import annotations

import ctypes
import errno
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config import session_key_posix as backend


@pytest.fixture
def api(monkeypatch):
    clear = getattr(backend, "_darwin_acl_library", None)
    if clear is not None:
        clear.cache_clear()
    allocations, queries, freed = [], [], []
    replies = [(None, errno.ENOENT)]

    def query(descriptor, kind):
        queries.append((descriptor, kind))
        pointer, error = replies[-1]
        ctypes.set_errno(error)
        return pointer

    def release(pointer):
        freed.append(pointer)
        return 0

    library = SimpleNamespace(acl_get_fd_np=query, acl_free=release)

    def load(name, *, use_errno):
        allocations.append((name, use_errno))
        return library

    monkeypatch.setattr(backend.ctypes, "CDLL", load)
    monkeypatch.setattr(backend, "sys", SimpleNamespace(platform="darwin"))
    # 其他平台的errno模块可能没有Darwin常量；仅补模拟ABI，不改变生产分支。
    monkeypatch.setattr(backend.errno, "ENOATTR", getattr(errno, "ENOATTR", 93), raising=False)
    yield SimpleNamespace(
        allocations=allocations, queries=queries, freed=freed, replies=replies, library=library
    )
    clear = getattr(backend, "_darwin_acl_library", None)
    if clear is not None:
        clear.cache_clear()


def test_static_library_is_bound_once_but_every_descriptor_is_queried(api):
    backend._private_acl(7)
    backend._private_acl(8)
    backend._private_acl(7)
    assert api.allocations == [(None, True)]
    assert api.queries == [(7, 0x100), (8, 0x100), (7, 0x100)]
    assert api.library.acl_get_fd_np.argtypes == [ctypes.c_int, ctypes.c_int]
    assert api.library.acl_get_fd_np.restype is ctypes.c_void_p
    assert api.library.acl_free.argtypes == [ctypes.c_void_p]
    assert api.library.acl_free.restype is ctypes.c_int


def test_acl_added_after_success_is_rejected_and_freed(api):
    backend._private_acl(7)
    api.replies.append((123, 0))
    with pytest.raises(KernelError) as caught:
        backend._private_acl(7)
    assert caught.value.code == "publication_key_unavailable"
    assert api.queries == [(7, 0x100), (7, 0x100)]
    assert api.freed == [123]


@pytest.mark.parametrize("error", [0, errno.EIO, errno.EPERM])
def test_unknown_errno_after_success_is_rejected(api, error):
    backend._private_acl(7)
    api.replies.append((None, error))
    with pytest.raises(KernelError) as caught:
        backend._private_acl(7)
    assert caught.value.code == "publication_key_unavailable"
    assert len(api.queries) == 2


@pytest.mark.parametrize("error", [errno.ENOENT, getattr(errno, "ENOATTR", 93)])
def test_original_absent_acl_errnos_are_freshly_accepted(api, error):
    api.replies.append((None, error))
    ctypes.set_errno(errno.EIO)
    backend._private_acl(7)
    assert api.queries == [(7, 0x100)]


@pytest.mark.parametrize("previous_errno", [errno.ENOENT, getattr(errno, "ENOATTR", 93)])
def test_null_acl_without_errno_cannot_reuse_previous_absence(api, previous_errno):
    backend._private_acl(7)

    def silent_query(descriptor, kind):
        api.queries.append((descriptor, kind))
        return None

    # 新查询不写errno时必须拒绝，不能沿用前次“无ACL”的线程错误码。
    api.library.acl_get_fd_np = silent_query
    ctypes.set_errno(previous_errno)
    with pytest.raises(KernelError) as caught:
        backend._private_acl(7)
    assert caught.value.code == "publication_key_unavailable"
    assert api.queries == [(7, 0x100), (7, 0x100)]


def test_backend_failure_does_not_cache_success(api, monkeypatch):
    original = backend.ctypes.CDLL
    attempts = []

    def load(name, *, use_errno):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("backend unavailable")
        return original(name, use_errno=use_errno)

    monkeypatch.setattr(backend.ctypes, "CDLL", load)
    with pytest.raises(OSError):
        backend._private_acl(7)
    backend._private_acl(7)
    assert len(attempts) == 2 and api.queries == [(7, 0x100)]


def test_other_platform_does_not_bind_darwin_api(api, monkeypatch):
    monkeypatch.setattr(backend, "sys", SimpleNamespace(platform="linux"))
    backend._private_acl(7)
    assert not api.allocations and not api.queries
