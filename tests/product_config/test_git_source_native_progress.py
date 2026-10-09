"""Source 显式分离真实读取与已验真事实计算，保留控制异常的每层身份。"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import WorkspaceFileVersion, WorkspaceMutation
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.product_config import git_delivery_source as module
from harnessix.product_config import git_user_observation as user_module
from harnessix.product_config.git_native_control import protected_git_control
from harnessix.product_config.git_parent_contracts import ProductGitDeliverySourceV2
from harnessix.product_config.workspace_patch_source_contracts import (
    WorkspacePatchSourceReference,
    product_git_delivery_source_digest,
)
from harnessix.workspace import snapshot as native_module
from harnessix.workspace import snapshot_v2 as snapshot_module
from harnessix.workspace.native_observation_io import NativeReadOperation, UpstreamCheckpointError
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from tests.workspace.test_snapshot_v2_native_progress import case as case

ERRORS = ("cancel", "task-cancel", "timeout", "os", "kernel", "upstream", "nested")


def _error(name):
    if name == "cancel":
        return TurnCancelled("first cancellation")
    if name == "task-cancel":
        return asyncio.CancelledError("first task cancellation")
    if name == "timeout":
        return TimeoutError("original deadline")
    if name == "os":
        return OSError("checkpoint, not native I/O")
    if name == "kernel":
        return KernelError("workspace_observation_failed", "checkpoint same code")
    if name == "upstream":
        return UpstreamCheckpointError(OSError("already marked"))
    return UpstreamCheckpointError(UpstreamCheckpointError(OSError("nested original")))


class _Probe:
    def __init__(self):
        self.phase = "history"
        self.trace = []
        self.cas_reads = []
        self.failure = None
        self.error = None
        self.full_marker = None
        self.failure_call = 1
        self.control = GitAuthenticationControl(self.local, self.full)

    def local(self):
        self.record("local")

    def full(self):
        self.record("full")

    def record(self, mode):
        self.trace.append((mode, self.phase))
        if (
            self.failure == (mode, self.phase)
            and self.trace.count(self.failure) == self.failure_call
        ):
            raise self.error

    def protected(self):
        def full():
            try:
                self.control()
            except BaseException as error:
                self.full_marker = UpstreamCheckpointError(error)
                raise self.full_marker from None

        return protected_git_control(self.control, full)


@pytest.fixture
def observed(case, monkeypatch):
    root, snapshot, blobs = case
    probe = _Probe()
    saved = {}
    original_history = snapshot_module.read_workspace_parent_closure
    original_capture = snapshot_module.capture_snapshot_facts
    original_encode = snapshot_module._encode_snapshot
    original_native_check = NativeReadOperation.checkpoint

    def history(*args, **kwargs):
        probe.phase = "history"
        result = original_history(*args, **kwargs)
        probe.phase = "entry"
        return result

    def capture(*args, **kwargs):
        saved["checkpoint"] = kwargs["checkpoint"]
        probe.phase = "native"
        try:
            return original_capture(*args, **kwargs)
        finally:
            probe.phase = "exit"

    def native_check(operation):
        previous = probe.phase
        probe.phase = "native-read"
        try:
            return original_native_check(operation)
        finally:
            probe.phase = previous

    def encode(*args, **kwargs):
        probe.phase = "encode"
        return original_encode(*args, **kwargs)

    def read(digest):
        probe.trace.append(("cas", probe.phase))
        probe.cas_reads.append(digest)
        return blobs[digest]

    monkeypatch.setattr(snapshot_module, "read_workspace_parent_closure", history)
    monkeypatch.setattr(snapshot_module, "capture_snapshot_facts", capture)
    monkeypatch.setattr(snapshot_module, "_encode_snapshot", encode)
    monkeypatch.setattr(NativeReadOperation, "checkpoint", native_check)
    ports = WorkspaceSnapshotPorts(
        write_blob=lambda *_: pytest.fail("只读 Source 不得追加 CAS"),
        read_blob=read,
    )
    return root, snapshot, blobs, probe, ports, saved


@pytest.mark.parametrize("protected", [False, True])
def test_exact_source_layers_history_and_encoding_but_all_cas_stay_full(observed, protected):
    root, snapshot, blobs, probe, ports, _ = observed
    before = dict(blobs)
    control = probe.protected() if protected else probe.control
    module._verify_final_snapshot(snapshot, root, control, ports)
    assert ("full", "entry") in probe.trace and ("full", "exit") in probe.trace
    assert ("local", "native") in probe.trace and ("local", "native-read") in probe.trace
    assert ("local", "encode") in probe.trace and ("local", "history") in probe.trace
    assert [mode for mode, phase in probe.trace if phase == "encode"][-1] == "full"
    assert all(phase == "history" for mode, phase in probe.trace if mode == "cas")
    assert all(
        mode in {"full", "local", "cas"} for mode, phase in probe.trace if phase == "history"
    )
    for index, (mode, _) in enumerate(probe.trace):
        if mode == "cas":
            assert probe.trace[index - 1] == probe.trace[index + 1] == ("full", "history")
    assert ("full", "native") not in probe.trace
    assert ("full", "native-read") not in probe.trace
    assert before == blobs


@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize(
    "failure,call",
    [
        (("local", "history"), 1),
        (("full", "exit"), 2),
        (("local", "encode"), 1),
        (("full", "encode"), 1),
    ],
)
@pytest.mark.parametrize("error_name", ERRORS)
def test_pure_history_and_encoding_preserve_original_control_error(
    observed, protected, failure, call, error_name
):
    root, snapshot, blobs, probe, ports, _ = observed
    error = _error(error_name)
    probe.failure, probe.error = failure, error
    probe.failure_call = call
    control = probe.protected() if protected else probe.control
    before = dict(blobs)
    with pytest.raises(BaseException) as caught:
        module._verify_final_snapshot(snapshot, root, control, ports)
    if protected:
        assert type(caught.value) is UpstreamCheckpointError and caught.value.error is error
        if failure[0] == "full":
            assert caught.value is probe.full_marker
    else:
        assert caught.value is error
    assert probe.trace[-1] == failure and blobs == before


@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("error_name", ERRORS)
@pytest.mark.parametrize(
    "failure",
    [
        ("full", "entry"),
        ("local", "native"),
        ("local", "native-read"),
        ("local", "exit"),
        ("full", "exit"),
    ],
)
def test_first_error_identity_and_nested_layers_never_reach_encode(
    observed,
    protected,
    error_name,
    failure,
):
    root, snapshot, blobs, probe, ports, _ = observed
    before = dict(blobs)
    error = _error(error_name)
    probe.failure, probe.error = failure, error
    control = probe.protected() if protected else probe.control
    with pytest.raises(BaseException) as caught:
        module._verify_final_snapshot(snapshot, root, control, ports)
    if protected:
        assert type(caught.value) is UpstreamCheckpointError
        assert caught.value.error is error
        if failure[0] == "full":
            assert caught.value is probe.full_marker
    else:
        assert caught.value is error
    assert probe.trace[-1] == failure
    assert all(phase != "encode" for _, phase in probe.trace)
    if failure[1] != "exit":
        assert all(phase != "exit" for _, phase in probe.trace)
    assert blobs == before


@pytest.mark.parametrize("error_name", ERRORS)
def test_native_body_error_is_not_mistaken_for_cm_owned_marker(observed, monkeypatch, error_name):
    root, snapshot, _, probe, ports, _ = observed
    error = _error(error_name)

    def observe(*args, **kwargs):
        raise error

    monkeypatch.setattr(native_module._PosixRoot, "observe", observe)
    with pytest.raises(BaseException) as caught:
        module._verify_final_snapshot(snapshot, root, probe.control, ports)
    assert caught.value is error
    assert ("full", "exit") not in probe.trace
    assert all(phase != "encode" for _, phase in probe.trace)


@pytest.mark.parametrize("changed", ["owner", "resource"])
@pytest.mark.parametrize("timing", ["cas", "capture-entry"])
def test_historical_cas_is_full_and_changed_binding_is_rejected_before_capture(
    observed,
    monkeypatch,
    changed,
    timing,
):
    root, snapshot, _, probe, ports, saved = observed
    bindings = {"owner": object(), "resource": object()}
    original_bindings = dict(bindings)
    error = KernelError("git_source_owner_changed", changed)

    def full():
        probe.trace.append(("full", probe.phase))
        if bindings != original_bindings:
            raise error

    control = GitAuthenticationControl(probe.local, full)
    original_history = snapshot_module.read_workspace_parent_closure

    def history(*args, **kwargs):
        result = original_history(*args, **kwargs)
        if timing == "capture-entry":
            bindings[changed] = object()
        return result

    def read(digest):
        assert probe.phase == "history"
        control()
        body = ports.read_blob(digest)
        if timing == "cas":
            bindings[changed] = object()
        return body

    monkeypatch.setattr(snapshot_module, "read_workspace_parent_closure", history)
    with pytest.raises(KernelError) as caught:
        module._verify_final_snapshot(
            snapshot,
            root,
            control,
            WorkspaceSnapshotPorts(ports.write_blob, read),
        )
    assert caught.value is error and saved == {}
    assert probe.trace[-1] == ("full", "history" if timing == "cas" else "entry")
    assert not any(mode == "local" and phase != "history" for mode, phase in probe.trace)


def _frozen_final_verifier():
    directory = os.environ.get("HARNESSIX_NATIVE_FROZEN_DIR")
    if directory is None:
        pytest.skip("冻结旧实现 oracle 仅在显式提供原件时运行")
    path = Path(directory) / "src/harnessix/product_config/git_delivery_source.py"
    tree = ast.parse(path.read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_verify_final_snapshot"
    )
    namespace = dict(vars(module))
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[function.name]


def _source_structure(case, monkeypatch):
    """原合同和原生事实；归属选择桩仅服务回调轨迹，不是授权验收。"""
    root, snapshot, blobs = case
    reference = WorkspacePatchSourceReference(
        turn_id=uuid4(),
        call_id=uuid4(),
        transaction_id=uuid4(),
        route_fingerprint="a" * 64,
        transaction_fingerprint="b" * 64,
    )
    before = WorkspaceFileVersion(presence="absent", size=0)
    versions = {}
    for observation in snapshot.resources:
        if observation.kind == "file":
            body = (root / observation.path).read_bytes()
            after = WorkspaceFileVersion(
                presence="file",
                sha256=hashlib.sha256(body).hexdigest(),
                size=len(body),
                mode=0o644,
            )
            versions[observation.path] = before, after
    mutations = tuple(
        WorkspaceMutation(path=path, before=first, after=last)
        for path, (first, last) in sorted(versions.items())
    )
    thread = SimpleNamespace(thread_id=uuid4(), workspace=str(root))
    candidate = ProductGitDeliverySourceV2.model_construct(
        thread_id=thread.thread_id,
        workspace=snapshot,
        patches=(reference,),
        mutations=mutations,
        digest="0" * 64,
    )
    source = ProductGitDeliverySourceV2(
        **candidate.model_dump(exclude={"digest"}),
        digest=product_git_delivery_source_digest(candidate),
    )
    owned = SimpleNamespace(
        reference=reference,
        record=SimpleNamespace(
            plan=SimpleNamespace(source=snapshot),
        ),
    )

    def selection(thread, targets, router, transactions, checkpoint):
        checkpoint()
        return (owned,)

    def merge(owned, checkpoint):
        checkpoint()
        return versions

    monkeypatch.setattr(module, "_owned_selection", selection)
    monkeypatch.setattr(module, "_merge_versions", merge)
    return thread, source


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
def test_complete_unknown_source_verifier_native_cas_and_file_trace_matches_frozen(
    case,
    observed,
    monkeypatch,
    kind,
):
    root, _, blobs, probe, ports, _ = observed
    thread, source = _source_structure(case, monkeypatch)
    directory = os.environ.get("HARNESSIX_NATIVE_FROZEN_DIR")
    if directory is None:
        pytest.skip("完整旧函数 oracle 需要显式冻结原件")
    path = Path(directory) / "src/harnessix/product_config/git_delivery_source.py"
    functions = [
        node
        for node in ast.parse(path.read_text()).body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"verify_git_delivery_source", "_verify_final_snapshot"}
    ]
    namespace = dict(vars(module))
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)

    class Proxy:
        def __call__(self):
            probe.full()

    class Subclass(GitAuthenticationControl):
        pass

    callback = {
        "function": probe.full,
        "proxy": Proxy(),
        "subclass": Subclass(probe.local, probe.full),
    }[kind]
    before = dict(blobs)
    probe.phase = "before"
    namespace["verify_git_delivery_source"](
        thread,
        source,
        None,
        None,
        checkpoint=callback,
        snapshot_ports=ports,
    )
    old_trace = tuple(probe.trace)
    probe.trace.clear()
    probe.phase = "before"
    module.verify_git_delivery_source(
        thread,
        source,
        None,
        None,
        checkpoint=callback,
        snapshot_ports=ports,
    )
    assert tuple(probe.trace) == old_trace
    assert before == blobs and Path(thread.workspace) == root


def test_complete_exact_source_layers_file_io_and_keeps_full_snapshot_boundaries(
    case,
    observed,
    monkeypatch,
):
    root, _, blobs, probe, ports, _ = observed
    thread, source = _source_structure(case, monkeypatch)
    original = module._read_existing

    def read(*args, **kwargs):
        probe.phase = "file"
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "_read_existing", read)
    before = dict(blobs)
    module.verify_git_delivery_source(
        thread,
        source,
        None,
        None,
        checkpoint=probe.control,
        snapshot_ports=ports,
    )
    assert ("local", "native-read") in probe.trace
    assert ("local", "file") in probe.trace and ("full", "file") in probe.trace
    assert probe.trace.count(("full", "entry")) == 2
    # 原生出口以及随后的编码入口各认证一次，两次完整 Source 复核均保留。
    assert probe.trace.count(("full", "exit")) == 4
    # 保留原四次编码/尾部认证；首个文件段入口再完整认证一次。
    assert probe.trace.count(("full", "encode")) == 5
    assert before == blobs and (root / source.mutations[0].path).read_bytes() == b"native facts\n"


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
def test_unknown_callback_preserves_frozen_complete_trace_and_verifier_arguments(
    observed,
    monkeypatch,
    kind,
):
    root, snapshot, _, probe, ports, _ = observed
    frozen = _frozen_final_verifier()

    class Proxy:
        def __call__(self):
            probe.full()

    class Subclass(GitAuthenticationControl):
        pass

    callback = {
        "function": probe.full,
        "proxy": Proxy(),
        "subclass": Subclass(probe.local, probe.full),
    }[kind]
    frozen(snapshot, root, callback, ports)
    expected = tuple(probe.trace)
    probe.trace.clear()
    calls = []
    original = module.verify_workspace_snapshot_v2

    def verify(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "verify_workspace_snapshot_v2", verify)
    module._verify_final_snapshot(snapshot, root, callback, ports)
    assert tuple(probe.trace) == expected
    assert calls == [((snapshot, root), {"checkpoint": callback, "read_blob": ports.read_blob})]


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
@pytest.mark.parametrize("error_name", ERRORS)
def test_unknown_first_error_keeps_original_object_and_no_new_calls(
    observed,
    kind,
    error_name,
):
    root, snapshot, _, probe, ports, _ = observed
    error = _error(error_name)

    def fail():
        probe.trace.append(("unknown", probe.phase))
        raise error

    class Proxy:
        def __call__(self):
            fail()

    class Subclass(GitAuthenticationControl):
        pass

    callback = {"function": fail, "proxy": Proxy(), "subclass": Subclass(fail, fail)}[kind]
    with pytest.raises(BaseException) as caught:
        module._verify_final_snapshot(snapshot, root, callback, ports)
    assert caught.value is error and probe.trace == [("unknown", "history")]


def test_saved_local_checkpoint_is_full_after_capture(observed):
    root, snapshot, _, probe, ports, saved = observed
    module._verify_final_snapshot(snapshot, root, probe.control, ports)
    probe.trace.clear()
    saved["checkpoint"]()
    assert probe.trace == [("full", "encode")]


@pytest.mark.asyncio
async def test_actual_other_task_native_capture_falls_back_to_full(observed):
    root, snapshot, _, probe, ports, _ = observed
    # 控制在实际当前 Task 创建，而非伪造 _task 字段。
    control = GitAuthenticationControl(probe.local, probe.full)

    async def child():
        module._verify_final_snapshot(snapshot, root, control, ports)

    await asyncio.create_task(child())
    assert ("full", "entry") in probe.trace and ("full", "exit") in probe.trace
    assert ("full", "native-read") in probe.trace
    assert all(mode != "local" for mode, _ in probe.trace)


def test_actual_other_thread_native_capture_falls_back_to_full(observed):
    root, snapshot, _, probe, ports, _ = observed
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(module._verify_final_snapshot, snapshot, root, probe.control, ports).result(5)
    assert ("full", "entry") in probe.trace and ("full", "native-read") in probe.trace
    assert all(mode != "local" for mode, _ in probe.trace)


@pytest.mark.parametrize("owner", ["task", "thread"])
@pytest.mark.asyncio
async def test_foreign_source_pure_port_matches_old_trace_and_each_full_failure(
    observed, monkeypatch, owner
):
    root, snapshot, _, probe, ports, _ = observed
    original = module.verify_workspace_snapshot_v2
    layered = False
    failure_call = None
    full_calls = 0
    error = KernelError("workspace_closure_corrupt", "original foreign control")

    def verify(*args, **kwargs):
        if not layered:
            kwargs.pop("pure_progress", None)
        return original(*args, **kwargs)

    def full():
        nonlocal full_calls
        full_calls += 1
        probe.full()
        if full_calls == failure_call:
            raise error

    control = GitAuthenticationControl(probe.local, full)
    monkeypatch.setattr(module, "verify_workspace_snapshot_v2", verify)

    def run(enabled, fail_at):
        nonlocal layered, failure_call, full_calls
        layered, failure_call, full_calls = enabled, fail_at, 0
        probe.trace.clear()
        probe.cas_reads.clear()
        probe.phase = "history"
        try:
            module._verify_final_snapshot(snapshot, root, control, ports)
        except BaseException as caught:
            assert fail_at is not None and caught is error
            return tuple(probe.trace), tuple(probe.cas_reads)
        assert fail_at is None
        return tuple(probe.trace), tuple(probe.cas_reads)

    async def invoke(enabled, fail_at=None):
        if owner == "thread":
            return await asyncio.to_thread(run, enabled, fail_at)

        async def child():
            return run(enabled, fail_at)

        return await asyncio.create_task(child())

    expected = await invoke(False)
    assert await invoke(True) == expected
    assert all(mode != "local" for mode, _ in expected[0])
    assert len(expected[1]) == 2
    for fail_at in range(1, sum(mode == "full" for mode, _ in expected[0]) + 1):
        assert await invoke(True, fail_at) == await invoke(False, fail_at)


@pytest.mark.parametrize("owner", ["task", "thread"])
@pytest.mark.asyncio
async def test_saved_active_capture_token_foreign_owner_revokes_local_path(observed, owner):
    root, snapshot, _, probe, ports, _ = observed
    control = GitAuthenticationControl(probe.local, probe.full)
    with module._native_snapshot_progress(control) as checkpoint:
        checkpoint()
        if owner == "task":

            async def child():
                checkpoint()

            await asyncio.create_task(child())
        else:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(checkpoint).result(5)
        checkpoint()
    assert probe.trace == [("full", "history"), ("local", "history")] + [("full", "history")] * 4


@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("error_name", ERRORS)
def test_exact_final_file_read_preserves_original_error_even_windows_style_conversion(
    observed,
    monkeypatch,
    protected,
    error_name,
):
    root, snapshot, _, probe, ports, _ = observed
    error = _error(error_name)
    probe.phase, probe.failure, probe.error = "file", ("full", "file"), error
    control = probe.protected() if protected else probe.control
    calls = []

    def read(root, path, platform, *, checkpoint):
        calls.append((path, platform))
        try:
            checkpoint()
        except OSError:
            raise KernelError("workspace_observation_failed", "Windows原转换边界") from None
        return b"unexpected", 0o644

    monkeypatch.setattr(module, "_read_existing", read)
    with pytest.raises(BaseException) as caught:
        module._read_source_file(root, snapshot.resources[-1].path, "windows", control)
    if protected:
        assert caught.value is probe.full_marker and caught.value.error is error
    else:
        assert caught.value is error
    # exact 文件段入口完整认证失败时，不应开始原生读取。
    assert calls == [] and probe.trace == [("full", "file")]


def test_unknown_file_read_preserves_original_argument_and_error_conversion(observed, monkeypatch):
    root, snapshot, _, probe, _, _ = observed
    error = OSError("legacy callback")

    def check():
        raise error

    def read(root, path, platform, *, checkpoint):
        assert checkpoint is check
        try:
            checkpoint()
        except OSError:
            raise KernelError("workspace_observation_failed", "原转换") from None

    monkeypatch.setattr(module, "_read_existing", read)
    with pytest.raises(KernelError) as caught:
        module._read_source_file(root, snapshot.resources[-1].path, "windows", check)
    assert caught.value.code == "workspace_observation_failed"
    assert probe.trace == []


@pytest.mark.skipif(os.name != "posix", reason="实际 POSIX 文件读取及异常转换边界")
@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("error_name", ERRORS)
def test_actual_posix_final_file_read_keeps_each_original_error_layer(
    observed,
    protected,
    error_name,
):
    root, snapshot, _, probe, _, _ = observed
    error = _error(error_name)
    probe.phase, probe.failure, probe.error = "native-read", ("full", "native-read"), error
    control = probe.protected() if protected else probe.control
    before = (root / snapshot.resources[-1].path).read_bytes()
    with pytest.raises(BaseException) as caught:
        module._read_source_file(root, snapshot.resources[-1].path, "posix", control)
    if protected:
        assert caught.value is probe.full_marker and caught.value.error is error
    else:
        assert caught.value is error
    assert probe.trace == [("full", "native-read")]
    assert (root / snapshot.resources[-1].path).read_bytes() == before


@pytest.mark.parametrize("error_name", ERRORS)
@pytest.mark.parametrize("failure", [("full", "entry"), ("local", "native-read"), ("full", "exit")])
def test_actual_user_source_callers_single_unwrap_retains_original_nested_identity(
    observed,
    monkeypatch,
    error_name,
    failure,
):
    """运行原 User Source 闭包 AST；不把结构测试冒充完整 SDK 验收。"""
    root, snapshot, _, probe, ports, _ = observed
    error = _error(error_name)
    probe.failure, probe.error = failure, error
    thread = SimpleNamespace(workspace=str(root))

    def verify(thread, source, router, transactions, *, checkpoint, snapshot_ports):
        module._verify_final_snapshot(source.workspace, root, checkpoint, snapshot_ports)

    monkeypatch.setattr(user_module, "verify_git_delivery_source", verify)
    tree = ast.parse(Path(user_module.__file__).read_text())
    caller = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "verify_source"
    )
    namespace = {
        **vars(user_module),
        "actual": SimpleNamespace(thread=thread),
        "source": SimpleNamespace(workspace=snapshot),
        "router": None,
        "transactions": None,
        "check": probe.control,
        "snapshot_ports": ports,
    }
    exec(
        compile(ast.Module(body=[caller], type_ignores=[]), user_module.__file__, "exec"), namespace
    )
    with pytest.raises(BaseException) as caught:
        namespace[caller.name]()
    # 边界只解自己新增的一层；Full/Local 的原嵌套错误对象均不能递归解包。
    assert caught.value is error
    assert probe.trace[-1] == failure
