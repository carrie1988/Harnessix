"""原生只读根端口的进度边界；合成 full 次数只验证机制，不是 SLO。"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery import git_authentication_control as controls
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.product_config import git_baseline as baseline
from harnessix.workspace import snapshot
from harnessix.workspace.native_observation_io import NativeReadOperation, UpstreamCheckpointError
from harnessix.workspace.snapshot_capture import capture_snapshot_facts


class _Probe:
    def __init__(self):
        self.trace = []
        self.hook = lambda phase, count: None
        self.control = GitAuthenticationControl(self.local, self.full)

    def record(self, phase):
        self.trace.append(phase)
        self.hook(phase, self.trace.count(phase))

    def local(self):
        self.record("local")

    def full(self):
        self.record("full")


def _io(control):
    return controls.io_git_authentication(control)


@pytest.fixture
def root_source(tmp_path):
    if os.name != "posix":
        pytest.skip("该夹具核验真实 POSIX 捕获；Windows 原生端口需独立验收")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "untouched").write_bytes(b"readonly child")
    for number in range(12):
        (tmp_path / str(number)).write_bytes(b"readonly member")
    facts = capture_snapshot_facts(
        tmp_path,
        cwd=".",
        resources=(),
        external_roots=None,
        platform="posix",
        checkpoint=lambda: None,
    )
    return tmp_path, SimpleNamespace(workspace=SimpleNamespace(**facts.scope))


def _tree(root):
    return {
        str(path.relative_to(root)): (
            path.stat().st_ino,
            path.stat().st_mode,
            path.stat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
        for path in (root, *root.rglob("*"))
    }


def test_io_api_preserves_v2_creation_fields_and_private_slots():
    assert hasattr(controls, "io_git_authentication")
    assert hasattr(GitAuthenticationControl, "io_progress")
    probe = _Probe()
    assert probe.control.contract_version == "harnessix.git-authentication-control/v2"
    assert GitAuthenticationControl.__slots__ == ("__origin", "__segment", "__dict__")
    assert set(probe.control.__dict__) == {"_local_check", "_authenticate", "_task", "_thread"}


def test_real_root_capture_uses_io_progress_and_is_readonly(root_source, monkeypatch):
    root, source = root_source
    before, probe, calls = _tree(root), _Probe(), []

    def capture(path, **kwargs):
        assert path == root
        assert {key: value for key, value in kwargs.items() if key != "checkpoint"} == {
            "cwd": ".",
            "resources": (),
            "external_roots": None,
            "platform": "posix",
        }
        calls.append(path)
        return capture_snapshot_facts(path, **kwargs)

    monkeypatch.setattr(baseline, "capture_snapshot_facts", capture)
    assert baseline._root_binding_matches(source, root, probe.control)
    assert calls == [root] and before == _tree(root)
    assert probe.trace[0] == probe.trace[-1] == "full"
    assert probe.trace.count("full") == 2
    assert probe.trace.count("local") > len(tuple(root.iterdir()))
    probe.control()
    probe.control()
    assert probe.trace[-2:] == ["full", "full"]  # 普通父调用没有认证缓存。


@pytest.mark.parametrize("field", ["workspace_id", "root_path_digest", "root_identity"])
def test_all_three_root_binding_fields_reject_drift(root_source, field):
    root, source = root_source
    assert baseline._root_binding_matches(source, root, _Probe().control)
    setattr(source.workspace, field, "0" * 64)
    probe = _Probe()
    assert not baseline._root_binding_matches(source, root, probe.control)
    assert probe.trace.count("full") == 2 and probe.control._segment is None


def _error(kind):
    return {
        "kernel": lambda: KernelError("git_test_control", "marker"),
        "cancel": lambda: asyncio.CancelledError("marker"),
        "turn-cancel": TurnCancelled,
        "timeout": lambda: TimeoutError("marker"),
        "oserror": lambda: OSError("marker"),
        "upstream": lambda: UpstreamCheckpointError(KernelError("git_test_inner", "marker")),
        "nested": lambda: UpstreamCheckpointError(UpstreamCheckpointError(OSError("marker"))),
    }[kind]()


@pytest.mark.parametrize(
    "kind", ["kernel", "cancel", "turn-cancel", "timeout", "oserror", "upstream", "nested"]
)
@pytest.mark.parametrize("boundary", ["entry", "native-outer", "native-directory", "tail", "exit"])
def test_real_boundary_failure_identity_and_no_result(root_source, monkeypatch, kind, boundary):
    """入口/段尾/出口不解包；native 内部沿旧 controlled/NativeReadOperation 解包。"""
    root, source = root_source
    probe, error, state = _Probe(), _error(kind), {"where": "outside"}
    original_directory = snapshot.observe_directory

    def directory(*args, **kwargs):
        state["where"] = "native-directory"
        try:
            return original_directory(*args, **kwargs)
        finally:
            state["where"] = "capture"

    def capture(*args, **kwargs):
        state["where"] = "native-outer"
        facts = capture_snapshot_facts(*args, **kwargs)
        state["where"] = "tail"
        return facts

    def fail(phase, count):
        current = ("entry" if count == 1 else "exit") if phase == "full" else state["where"]
        if current == boundary:
            raise error
        if current == "native-outer":
            state["where"] = "capture"

    probe.hook = fail
    monkeypatch.setattr(snapshot, "observe_directory", directory)
    monkeypatch.setattr(baseline, "capture_snapshot_facts", capture)
    expected = error
    delivered = []
    with pytest.raises(type(expected)) as caught:
        delivered.append(baseline._root_binding_matches(source, root, probe.control))
    assert caught.value is expected and delivered == []
    assert probe.control._segment is None
    assert probe.trace.count("full") == (2 if boundary == "exit" else 1)


@pytest.mark.parametrize("stop", ["cancel", "deadline", "lock-generation"])
def test_native_members_consume_same_parent_local_controls(root_source, monkeypatch, stop):
    """真实目录读取，合成锁代际；不声称真实宿主锁/SDK 已验收。"""
    root, source = root_source
    cancel, clock, generation = CancelToken(), [0], [7]
    error = KernelError("git_local_control_invalid", "local marker")

    def local():
        cancel.checkpoint()
        if clock[0] >= 10 or generation[0] != 7:
            raise error

    control = GitAuthenticationControl(local, local)
    original_directory = snapshot.observe_directory

    def directory(*args, **kwargs):
        if stop == "cancel":
            cancel.cancel()
        elif stop == "deadline":
            clock[0] = 10
        else:
            generation[0] = 8
        return original_directory(*args, **kwargs)

    monkeypatch.setattr(snapshot, "observe_directory", directory)
    with pytest.raises(TurnCancelled if stop == "cancel" else KernelError) as caught:
        baseline._root_binding_matches(source, root, control)
    if stop != "cancel":
        assert caught.value is error
    assert control._segment is None


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_original_native_read_operation_protection_still_runs(root_source, monkeypatch, stop):
    root, source = root_source
    original_init = NativeReadOperation.__init__
    operations = []

    def stopped_init(self, checkpoint):
        original_init(self, checkpoint)
        operations.append(self)
        if stop == "cancel":
            self.stopped.set()
        else:
            self.deadline = 0

    monkeypatch.setattr(NativeReadOperation, "__init__", stopped_init)
    with pytest.raises(TurnCancelled if stop == "cancel" else KernelError) as caught:
        baseline._root_binding_matches(source, root, _Probe().control)
    assert len(operations) == 1
    if stop == "timeout":
        assert caught.value.code == "workspace_timeout"  # 原生局部错误转换，不是父异常。


# 7d7489dc 的原 _root_binding_matches 源码 oracle，独立于新 helper。
_FROZEN_ROOT_SOURCE = '''
def _root_binding_matches(
    source: ProductGitDeliverySource | ProductGitDeliverySourceV2,
    root: Path,
    checkpoint: Callable[[], None],
) -> bool:
    """借原事实捕获端口校验相同根；目录逐项消费父取消和共同期限。"""

    def controlled() -> None:
        try:
            checkpoint()
        except UpstreamCheckpointError:
            raise
        except BaseException as error:
            raise UpstreamCheckpointError(error) from None

    try:
        facts = capture_snapshot_facts(
            root,
            cwd=".",
            resources=(),
            external_roots=None,
            platform=source.workspace.platform,
            checkpoint=controlled,
        )
    except UpstreamCheckpointError as error:
        raise error.error from None
    return all(
        facts.scope[name] == getattr(source.workspace, name)
        for name in ("workspace_id", "root_path_digest", "root_identity")
    )
'''


def _unknown(kind, record):
    def forbidden(*args, **kwargs):
        pytest.fail("未知 checkpoint 的进度属性不得读取或调用")

    class Proxy:
        io_progress = pure = staticmethod(forbidden)

        def __call__(self):
            record()

    class Subclass(GitAuthenticationControl):
        io_progress = pure = staticmethod(forbidden)

    def function():
        record()

    function.io_progress = forbidden
    return {"function": function, "proxy": Proxy(), "subclass": Subclass(forbidden, record)}[kind]


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
def test_unknown_helper_yields_original_without_extra_calls(kind):
    trace = []
    checkpoint = _unknown(kind, lambda: trace.append("full"))
    with _io(checkpoint) as check:
        assert check is checkpoint and trace == []
        check()
    error = _error("cancel")
    with pytest.raises(type(error)) as caught:
        with _io(checkpoint):
            raise error
    assert caught.value is error and trace == ["full"]


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
@pytest.mark.parametrize("failure_at", [0, 1, 3, 6, 18])
@pytest.mark.parametrize("error_kind", ["kernel", "cancel", "upstream"])
def test_unknown_root_trace_and_identity_match_frozen_source(
    root_source,
    kind,
    failure_at,
    error_kind,
):
    root, source = root_source
    namespace = {
        "capture_snapshot_facts": capture_snapshot_facts,
        "UpstreamCheckpointError": UpstreamCheckpointError,
    }
    exec(compile(_FROZEN_ROOT_SOURCE, "frozen-7d7489dc-root", "exec"), namespace)
    error, results = _error(error_kind), []
    for match in (namespace["_root_binding_matches"], baseline._root_binding_matches):
        trace = []

        def record(events=trace):
            events.append("full")
            if len(events) == failure_at:
                raise error

        try:
            result = match(source, root, _unknown(kind, record))
        except BaseException as caught:
            result = caught
        results.append((trace, result))
    assert results[0][0] == results[1][0]
    if failure_at:
        expected = error.error if error_kind == "upstream" else error
        assert results[0][1] is results[1][1] is expected
    else:
        assert results[0][1] is results[1][1] is True


@pytest.mark.parametrize("failure", ["none", "body", "local", "tail", "exit"])
def test_saved_check_is_revoked_on_success_or_first_failure(failure):
    probe, marker = _Probe(), _error("cancel")
    fault = {"local": ("local", 1), "tail": ("local", 2), "exit": ("full", 2)}.get(failure)

    def fail(phase, count):
        if (phase, count) == fault:
            raise marker

    probe.hook = fail

    def run():
        with _io(probe.control) as check:
            saved.append(check)
            check()
            if failure == "body":
                raise marker

    saved = []
    if failure == "none":
        run()
    else:
        with pytest.raises(type(marker)) as caught:
            run()
        assert caught.value is marker
    assert probe.control._segment is None
    probe.hook = lambda phase, count: None
    probe.trace.clear()
    saved[0]()
    assert probe.trace == ["full"]


def _foreign(probe, saved):
    origin = probe.control._origin
    saved()
    with _io(probe.control) as check:
        assert check.__self__ is probe.control
        check()
    assert probe.control._origin is origin


def test_active_saved_check_and_foreign_thread_never_rebind():
    probe = _Probe()
    with _io(probe.control) as saved:
        saved()
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(copy_context().run, _foreign, probe, saved).result(timeout=2)
        saved()
    assert probe.trace == ["full", "local"] + ["full"] * 7


@pytest.mark.asyncio
async def test_active_saved_check_and_foreign_task_never_rebind():
    probe = _Probe()
    with _io(probe.control) as saved:
        saved()

        async def foreign():
            _foreign(probe, saved)

        await asyncio.create_task(foreign(), context=copy_context())
        saved()
    assert probe.trace == ["full", "local"] + ["full"] * 7


@pytest.mark.parametrize("inner_kind", ["pure", "io"])
def test_nested_segments_share_revocation_and_never_restore_outer(inner_kind):
    probe = _Probe()
    inner_segment = controls.pure_git_authentication if inner_kind == "pure" else _io
    with _io(probe.control) as outer:
        outer()
        with inner_segment(probe.control) as inner:
            inner()
            outer()
            inner()
        outer()
    assert probe.trace == ["full", "local", "full", "local"] + ["full"] * 7


def test_full_call_revokes_before_authenticate_reentry():
    probe, saved = _Probe(), []

    def reenter(phase, count):
        if phase == "full" and count == 2:
            saved[0]()

    probe.hook = reenter
    with _io(probe.control) as check:
        saved.append(check)
        check()
        probe.control()
        check()
    assert probe.trace == ["full", "local"] + ["full"] * 5


def test_instance_method_shadows_cannot_replace_class_direct_progress():
    probe = _Probe()

    def forbidden(*args, **kwargs):
        pytest.fail("实例方法遮蔽不得执行")

    for name in ("io_progress", "pure", "_segments_check", "_binding", "__call__"):
        probe.control.__dict__[name] = forbidden
    with _io(probe.control) as check:
        check()
    assert probe.trace == ["full", "local", "local", "full"]


@pytest.mark.parametrize("field", ["_local_check", "_authenticate", "_task", "_thread"])
@pytest.mark.parametrize("phase", ["entry", "local", "exit"])
def test_creator_binding_drift_rejects_without_running_replacement(field, phase):
    probe = _Probe()
    when = {"entry": ("full", 1), "local": ("local", 1), "exit": ("full", 2)}[phase]

    def forbidden():
        pytest.fail("漂移的创建回调不得执行")

    def drift(current, count):
        if (current, count) == when:
            replacement = forbidden if field in ("_local_check", "_authenticate") else object()
            setattr(probe.control, field, replacement)

    probe.hook = drift
    with pytest.raises(KernelError) as caught:
        with _io(probe.control) as check:
            check()
    assert caught.value.code == "git_authentication_control_invalid"
    assert probe.control._segment is None
