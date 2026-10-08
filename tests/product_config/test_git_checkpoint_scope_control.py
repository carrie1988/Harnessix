"""Scope 混合构建的原控制传递；真实 CAS 不等同实际 SDK 授权。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import (
    GitAuthenticationControl,
    pure_git_authentication,
)
from harnessix.product_config import git_checkpoint_scope as assembly
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from tests.product_config.test_git_checkpoint_scope import build
from tests.product_config.test_git_checkpoint_scope import cas as cas
from tests.product_config.test_git_checkpoint_scope import case as case
from tests.support.git_delivery_observed_core import canonical, json_facts


async def test_original_creator_reaches_mixed_build_with_typed_control(case, monkeypatch):
    events, controls = [], []
    parent = GitAuthenticationControl(lambda: events.append("local"), lambda: events.append("full"))
    original = assembly._build

    def observed(*args):
        control = args[-1]
        assert type(control) is GitAuthenticationControl
        assert GitAuthenticationControl._binding(control)[2] is asyncio.current_task()
        controls.append(control)
        return original(*args)

    monkeypatch.setattr(assembly, "_build", observed)
    scope, diff = build(case, checkpoint=parent)
    assert canonical(json_facts(scope)) == canonical(json_facts(case.core.object_scope))
    assert diff.content.text == case.diff_text and events.count("local") > 100
    assert controls[0]._segment is None and parent._segment is None


@pytest.mark.parametrize("surface", ("foreign-task", "foreign-thread"))
async def test_foreign_creator_keeps_plain_full_control_without_rebinding(
    case, monkeypatch, surface
):
    events, delivered = [], []
    parent = GitAuthenticationControl(lambda: events.append("local"), lambda: events.append("full"))
    original = assembly._build

    def observed(*args):
        assert type(args[-1]) is not GitAuthenticationControl
        delivered.append(True)
        return original(*args)

    monkeypatch.setattr(assembly, "_build", observed)

    async def child():
        return build(case, checkpoint=parent)

    result = (
        await asyncio.create_task(child())
        if surface == "foreign-task"
        else await asyncio.to_thread(build, case, checkpoint=parent)
    )
    assert canonical(json_facts(result[0])) == canonical(json_facts(case.core.object_scope))
    assert delivered == [True] and events and set(events) == {"full"}


@pytest.mark.parametrize("phase", ("full", "local", "exit"))
@pytest.mark.parametrize("kind", ("value", "kernel", "upstream", "nested-upstream", "cancel"))
async def test_mapped_control_unwraps_exactly_one_new_marker(case, monkeypatch, phase, kind):
    inner = ValueError("original callback")
    marker = {
        "value": inner,
        "kernel": KernelError("original", "original callback"),
        "upstream": UpstreamCheckpointError(inner),
        "nested-upstream": UpstreamCheckpointError(UpstreamCheckpointError(inner)),
        "cancel": asyncio.CancelledError("original callback"),
    }[kind]
    armed, events = [False], []

    def local():
        events.append("local")
        if armed[0] and phase == "local":
            raise marker

    def full():
        events.append("full")
        if armed[0] and phase in {"full", "exit"}:
            raise marker

    parent = GitAuthenticationControl(local, full)

    def body(*args):
        control = args[-1]
        assert type(control) is GitAuthenticationControl
        if phase != "exit":
            armed[0] = True
        with pure_git_authentication(control) as check:
            if phase == "exit":
                check()
                armed[0] = True
            else:
                check()
        pytest.fail("failed control must not deliver a result")

    monkeypatch.setattr(assembly, "_build", body)
    with pytest.raises(type(marker)) as failure:
        build(case, checkpoint=parent)
    assert failure.value is marker and parent._segment is None


@pytest.mark.parametrize("field", ("_task", "_thread", "_local_check", "_authenticate"))
@pytest.mark.parametrize("phase", ("full", "local"))
async def test_build_adapter_never_recaptures_changed_parent_fields(
    case, monkeypatch, field, phase
):
    events = []
    parent = GitAuthenticationControl(lambda: events.append("local"), lambda: events.append("full"))

    def forbidden():
        pytest.fail("replacement callback must never execute")

    def body(*args):
        control = args[-1]
        assert type(control) is GitAuthenticationControl
        replacement = {
            "_task": object(),
            "_thread": parent._thread + 1,
            "_local_check": forbidden,
            "_authenticate": forbidden,
        }[field]
        if phase == "full":
            setattr(parent, field, replacement)
            control()
        else:
            with pure_git_authentication(control) as check:
                setattr(parent, field, replacement)
                check()
        pytest.fail("changed binding must not return")

    monkeypatch.setattr(assembly, "_build", body)
    with pytest.raises(KernelError) as failure:
        build(case, checkpoint=parent)
    assert failure.value.code == "git_authentication_control_invalid" and parent._segment is None


@pytest.mark.parametrize("kind", ("function", "proxy", "subclass"))
def test_unknown_build_control_keeps_legacy_trace_and_error_identity(case, monkeypatch, kind):
    events = []

    def full():
        events.append("full")

    def forbidden():
        pytest.fail("unknown pure must not execute")

    full.pure = forbidden

    class Proxy:
        __call__ = staticmethod(full)
        pure = staticmethod(forbidden)

    class Subclass(GitAuthenticationControl):
        @contextmanager
        def pure(self):
            pytest.fail("subclass pure must not execute")
            yield full

    callbacks = {"function": full, "proxy": Proxy(), "subclass": Subclass(forbidden, full)}
    expected = build(case, checkpoint=full)
    original = events[:]
    events.clear()
    assert build(case, checkpoint=callbacks[kind]) == expected and events == original
    marker = UpstreamCheckpointError(ValueError("original parser"))

    def fail(*args):
        raise marker

    monkeypatch.setattr(assembly, "_build", fail)
    with pytest.raises(ValueError) as failure:
        build(case, checkpoint=callbacks[kind])
    assert failure.value is marker.error
