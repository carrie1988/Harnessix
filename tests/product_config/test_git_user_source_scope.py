"""只验证原生来源 Scope 的资源所有权与全集登记，不代替原 U 认证。"""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config import git_user_source_scope as module


def fixture_scope(monkeypatch):
    opened, closed, verified = [], [], []

    @contextmanager
    def pin(*args, checkpoint):
        checkpoint()
        sources = SimpleNamespace(verify=lambda check: (check(), verified.append(len(verified))))
        opened.append(sources)
        try:
            yield sources
        finally:
            closed.append(sources)

    monkeypatch.setattr(module, "pin_git_user_source_files", pin)
    monkeypatch.setattr(module, "product_git_user_observation_fingerprint", lambda value: value)
    return module.GitUserSourceScope(), opened, closed, verified


def test_all_verified_sources_remain_until_original_scope_exit(monkeypatch):
    scope, opened, closed, verified = fixture_scope(monkeypatch)
    root = Path("/synthetic-source")
    with scope:
        first = scope.pin(root, root, root, lambda: None)
        second = scope.pin(root, root, root, lambda: None)
        scope.retain("first", first)
        scope.retain("second", second)
        scope.require("first", lambda: None)
        scope.require("second", lambda: None)
        assert len(verified) == 3 and not closed
    assert first is second and len(closed) == len(opened) == 1
    assert scope._sources is None and not scope._verified


def test_repeated_u_keeps_one_source_and_does_not_accumulate_native_handles(monkeypatch):
    scope, opened, closed, verified = fixture_scope(monkeypatch)
    root = Path("/synthetic-source")
    with scope:
        first = scope.pin(root, root, root, lambda: None)
        second = scope.pin(root, root, root, lambda: None)
        scope.retain("same", first)
        scope.retain("same", second)
        assert not closed
        scope.require("same", lambda: None)
        assert len(opened) == 1 and len(verified) == 2
    assert first is second and closed == [first] and len(opened) == 1


@pytest.mark.parametrize("phase", ["inactive", "unverified", "foreign", "closed"])
def test_invalid_source_ownership_refuses_without_granting_proof(monkeypatch, phase):
    scope, opened, closed, verified = fixture_scope(monkeypatch)
    root = Path("/synthetic-source")
    with pytest.raises(KernelError) as caught:
        if phase == "inactive":
            scope.pin(root, root, root, lambda: None)
        else:
            with scope:
                source = scope.pin(root, root, root, lambda: None)
                if phase == "unverified":
                    scope.require("missing", lambda: None)
                elif phase == "foreign":
                    scope.retain("unknown", object())
                else:
                    scope.retain("known", source)
            scope.require("known", lambda: None)
    assert caught.value.code == "git_user_observation_changed"
    assert len(opened) == len(closed) and not verified


@pytest.mark.parametrize("registered", [False, True])
def test_original_exception_after_capture_closes_discarded_child_result(monkeypatch, registered):
    scope, opened, closed, _ = fixture_scope(monkeypatch)
    root = Path("/synthetic-source")
    original = TimeoutError("original")
    with pytest.raises(TimeoutError) as caught:
        with scope:
            source = scope.pin(root, root, root, lambda: None)
            if registered:
                scope.retain("known", source)
            raise original
    assert caught.value is original
    assert len(opened) == len(closed) == 1
    assert scope._sources is None and not scope._verified


def test_source_scope_refuses_switching_repository_roots(monkeypatch):
    scope, opened, closed, _ = fixture_scope(monkeypatch)
    root = Path("/synthetic-source")
    with scope:
        scope.pin(root, root, root, lambda: None)
        with pytest.raises(KernelError) as caught:
            scope.pin(root, root, root / "other-admin", lambda: None)
        assert caught.value.code == "git_user_observation_changed"
    assert len(opened) == len(closed) == 1
