"""Git P1 v2 纯段生命周期负控；不涉及真实数据库、I/O 或结果发布。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context

import pytest

from harnessix.delivery import git_authentication_control as control_module
from harnessix.delivery.git_authentication_control import (
    GitAuthenticationControl,
    pure_git_authentication,
)


class _Probe:
    def __init__(self):
        self.trace = []
        self.failures = {}
        self.control = GitAuthenticationControl(self.local, self.authenticate)

    def record(self, phase):
        self.trace.append(phase)
        error = self.failures.get((phase, self.trace.count(phase)))
        if error is not None:
            raise error

    def local(self):
        self.record("local")

    def authenticate(self):
        self.record("auth")


@pytest.fixture
def probe():
    return _Probe()


@pytest.mark.parametrize("leaves", [0, 3])
def test_exact_helper_trace_authenticates_boundaries_and_retired_checkpoint(probe, leaves):
    assert probe.control.contract_version == "harnessix.git-authentication-control/v2"
    probe.control()
    with pure_git_authentication(probe.control) as retired:
        for _ in range(leaves):
            retired()
    retired()
    assert probe.trace == ["auth", "auth"] + ["local"] * (leaves + 1) + ["auth", "auth"]


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError, TimeoutError])
@pytest.mark.parametrize(
    ("phase", "k"), [("local", k) for k in range(1, 5)] + [("auth", k) for k in range(1, 6)]
)
def test_kth_local_or_full_failure_preserves_exception_identity(probe, phase, k, error_type):
    error = error_type(f"{phase}:{k}")
    probe.failures[phase, k] = error
    with pytest.raises(error_type) as caught:
        with probe.control.pure() as checkpoint:
            for _ in range(3 if phase == "local" else 2):
                (checkpoint if phase == "local" else probe.control)()
    assert caught.value is error
    assert probe.trace == (["auth"] + ["local"] * k if phase == "local" else ["auth"] * k)


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError, TimeoutError])
def test_body_failure_skips_exit_callbacks_and_revokes_saved_checkpoint(probe, error_type):
    error = error_type("body")
    probe.failures["local", 1] = AssertionError("禁止退出局部回调覆盖首异常")
    probe.failures["auth", 2] = AssertionError("禁止退出认证回调覆盖首异常")
    with pytest.raises(error_type) as caught:
        with probe.control.pure() as retired:
            raise error
    assert caught.value is error
    assert probe.trace == ["auth"]
    with pytest.raises(AssertionError) as later:
        retired()
    assert later.value is probe.failures["auth", 2]
    assert probe.trace == ["auth", "auth"]


@pytest.mark.parametrize(("phase", "k"), [("local", 1), ("auth", 2)])
def test_saved_checkpoint_is_full_after_local_or_exit_auth_failure(probe, phase, k):
    error = RuntimeError(phase)
    probe.failures[phase, k] = error
    with pytest.raises(RuntimeError) as caught:
        with probe.control.pure() as retired:
            retired()
    assert caught.value is error
    probe.trace.clear()
    retired()
    assert probe.trace == ["auth"]


@pytest.mark.parametrize("use_outer_inside", [False, True])
def test_nested_segment_never_restores_outer_and_old_check_revokes_inner(probe, use_outer_inside):
    with probe.control.pure() as outer:
        outer()
        with probe.control.pure() as inner:
            inner()
            if use_outer_inside:
                outer()
                inner()
        outer()
    tail = ["auth"] * 7 if use_outer_inside else ["local"] + ["auth"] * 4
    assert probe.trace == ["auth", "local", "auth", "local"] + tail


@pytest.mark.parametrize("owner_function", ["_current_task", "get_ident"])
def test_active_checkpoint_rejects_injected_foreign_owner_in_copied_context(
    probe, monkeypatch, owner_function
):
    owners = (getattr(control_module, owner_function)(), object())
    owner = ContextVar("owner", default=0)
    monkeypatch.setattr(control_module, owner_function, lambda: owners[owner.get()])
    copied = copy_context()
    copied.run(owner.set, 1)
    with probe.control.pure() as checkpoint:
        checkpoint()
        copied.run(checkpoint)
        checkpoint()
    assert probe.trace == ["auth", "local"] + ["auth"] * 4


def _exercise_foreign_owner(probe, retired):
    retired()
    with probe.control.pure() as checkpoint:
        assert checkpoint.__self__ is probe.control
        checkpoint()


@pytest.mark.asyncio
async def test_real_sibling_tasks_and_copied_context_cannot_inherit_fast_checkpoint():
    probe = _Probe()
    parent = asyncio.current_task()
    with probe.control.pure() as retired:
        retired()

    async def sibling():
        assert asyncio.current_task() is not parent
        _exercise_foreign_owner(probe, retired)

    await asyncio.gather(
        *(asyncio.create_task(sibling(), context=copy_context()) for _ in range(2))
    )
    assert probe.trace == ["auth", "local", "local", "auth"] + ["auth"] * 8


def test_real_thread_and_copied_context_cannot_inherit_fast_checkpoint(probe):
    with probe.control.pure() as retired:
        retired()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(copy_context().run, _exercise_foreign_owner, probe, retired)
        future.result(timeout=2)
    assert probe.trace == ["auth", "local", "local", "auth"] + ["auth"] * 4


def test_full_auth_reentry_cannot_reuse_old_local_checkpoint():
    trace = []

    def authenticate():
        trace.append("auth")
        if trace.count("auth") == 2:
            retired()

    control = GitAuthenticationControl(lambda: trace.append("local"), authenticate)
    with control.pure() as retired:
        retired()
        control()
        retired()
    assert trace == ["auth", "local"] + ["auth"] * 5


def test_exit_auth_failure_prevents_delivering_computed_result(probe):
    error = RuntimeError("exit authentication")
    probe.failures["auth", 2] = error

    def compute():
        with pure_git_authentication(probe.control) as checkpoint:
            checkpoint()
            value = 42
        return value

    delivered = None
    with pytest.raises(RuntimeError) as caught:
        delivered = compute()
    assert caught.value is error
    assert delivered is None
    assert probe.trace == ["auth", "local", "local", "auth"]


def test_transient_external_flag_restoration_is_not_leaf_auth_equivalence():
    """只模拟外部状态标志；边界认证不保证观测段内变化后恢复，不代表逐叶完整认证。"""
    trusted, observed, trace = True, [], []
    error = RuntimeError("untrusted flag")

    def authenticate():
        trace.append("auth")
        observed.append(trusted)
        if not trusted:
            raise error

    control = GitAuthenticationControl(lambda: trace.append("local"), authenticate)
    with control.pure() as checkpoint:
        trusted = False
        checkpoint()
        trusted = True
    assert observed == [True, True]
    assert trace == ["auth", "local", "local", "auth"]
    trusted = False
    with pytest.raises(RuntimeError) as caught:
        control()
    assert caught.value is error
    assert observed == [True, True, False]


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
def test_helper_unknown_callbacks_keep_original_trace_without_trusting_pure(probe, kind):
    def forbidden_pure():
        raise AssertionError("未知回调不得自动进入分层纯段")

    def function():
        probe.control()

    function.pure = forbidden_pure

    class Proxy:
        pure = staticmethod(forbidden_pure)

        def __call__(self):
            probe.control()

    class UnknownSubclass(GitAuthenticationControl):
        pure = staticmethod(forbidden_pure)

    checkpoint = {
        "function": function,
        "proxy": Proxy(),
        "subclass": UnknownSubclass(probe.local, probe.authenticate),
    }[kind]
    with pure_git_authentication(checkpoint) as check:
        assert check is checkpoint
        check()
        check()
    error = RuntimeError("body")
    with pytest.raises(RuntimeError) as caught:
        with pure_git_authentication(checkpoint):
            raise error
    assert caught.value is error
    assert probe.trace == ["auth", "auth"]
