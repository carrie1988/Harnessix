"""探针透明性与低敏负对照；不伪造真实 Owner 或 Windows 验收结果。"""

from __future__ import annotations

import ast
import asyncio
import inspect
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from tests.product_config import git_minimum_commit_probe as probe_module

ASYNC_PHASES = ("start", "stage", "send", "close", "wait", "complete", "receipt", "outer_exit")
SYNC_PHASES = ("exit_gate", "raw", "proof", "cleanup")
ROOT = Path(__file__).resolve().parents[2]
CANARY = "不得记录的正文路径环境密钥异常参数"


@pytest.fixture
def active_operation():
    probe = probe_module.Probe("A")
    operation = probe_module.Operation(probe, data={"kind": "read"})
    probe.operations.append(operation)
    token = probe_module._ACTIVE.set(operation)
    try:
        yield probe, operation
    finally:
        probe_module._ACTIVE.reset(token)


@pytest.mark.parametrize("phase", ASYNC_PHASES)
async def test_async_wrapper_calls_once_and_preserves_argument_return_identity(
    active_operation, phase
):
    probe, _ = active_operation
    positional, keyword, returned = object(), object(), object()
    calls = []

    async def original(*args, **kwargs):
        calls.append((args, kwargs))
        return returned

    wrapped = probe_module._async_wrapper(original, phase)
    assert await wrapped(positional, marker=keyword) is returned
    assert len(calls) == 1
    assert calls[0][0][0] is positional and calls[0][1]["marker"] is keyword
    assert wrapped.__wrapped__ is original
    assert probe.events[-1]["outcome"] == "return"


@pytest.mark.parametrize("phase", SYNC_PHASES)
def test_sync_wrapper_calls_once_and_preserves_argument_return_identity(active_operation, phase):
    _, _ = active_operation
    positional, keyword, returned = object(), object(), object()
    calls = []

    def original(*args, **kwargs):
        calls.append((args, kwargs))
        return returned

    wrapped = probe_module._sync_wrapper(original, phase)
    assert wrapped(positional, marker=keyword) is returned
    assert len(calls) == 1
    assert calls[0][0][0] is positional and calls[0][1]["marker"] is keyword


@pytest.mark.parametrize("phase", ASYNC_PHASES)
async def test_async_wrapper_preserves_original_exception_context_without_replay(
    active_operation, phase
):
    probe, _ = active_operation
    cause = OSError(13, CANARY, CANARY)
    error = KernelError("git_material_effect_unknown", CANARY)
    error.__context__ = cause
    error.__suppress_context__ = True
    calls = 0

    async def original(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(KernelError) as raised:
        await probe_module._async_wrapper(original, phase)(object(), marker=object())
    assert raised.value is error and error.__context__ is cause and error.__suppress_context__
    assert calls == 1
    assert CANARY not in probe.render()
    chain = probe.events[-1]["error_chain"]
    assert chain[0]["code"] == "git_material_effect_unknown"
    assert chain[1]["errno"] == 13


@pytest.mark.parametrize("phase", SYNC_PHASES)
def test_sync_wrapper_preserves_original_exception_without_replay(active_operation, phase):
    error = KernelError("git_material_stage_changed", CANARY)
    calls = 0

    def original(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(KernelError) as raised:
        probe_module._sync_wrapper(original, phase)(object())
    assert raised.value is error and calls == 1


@pytest.mark.parametrize("outcome", ["return", "kernel_error", "cancelled"])
async def test_collector_oserror_never_replaces_original_result_or_cancellation(
    active_operation, monkeypatch, outcome
):
    probe, _ = active_operation
    value = object()
    error = (
        asyncio.CancelledError(CANARY)
        if outcome == "cancelled"
        else KernelError("git_material_effect_unknown", CANARY)
    )
    calls = 0

    def broken(*args):
        raise OSError(CANARY)

    async def original(*args, **kwargs):
        nonlocal calls
        calls += 1
        if outcome != "return":
            raise error
        return value

    monkeypatch.setattr(probe, "event", broken)
    monkeypatch.setattr(probe_module, "_success", broken)
    wrapped = probe_module._async_wrapper(original, "cleanup")
    if outcome == "return":
        assert await wrapped() is value
    else:
        with pytest.raises(type(error)) as raised:
            await wrapped()
        assert raised.value is error
    assert calls == 1 and probe.incomplete


def test_sync_collector_fault_preserves_cleanup_call_and_exception(active_operation, monkeypatch):
    probe, _ = active_operation
    error = OSError(CANARY)
    calls = 0

    def broken(*args):
        raise OSError(CANARY)

    def original():
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(probe, "event", broken)
    with pytest.raises(OSError) as raised:
        probe_module._sync_wrapper(original, "cleanup")()
    assert raised.value is error and calls == 1 and probe.incomplete


async def test_operation_scope_enters_child_task_and_resets_without_changing_original():
    probe = probe_module.Probe("A")
    prepared = SimpleNamespace(write=None)
    returned = object()
    seen = []

    async def child():
        seen.append(probe_module._ACTIVE.get())
        return returned

    async def original(case, *, prepared):
        assert prepared is preparation
        return await asyncio.create_task(child())

    preparation = prepared
    assert (
        await probe_module._operation_wrapper(original, probe)(object(), prepared=prepared)
        is returned
    )
    assert seen == probe.operations and probe.operations[0].finished
    assert probe_module._ACTIVE.get() is None


async def test_operation_cap_limits_collection_not_original_execution():
    probe = probe_module.Probe("A")
    prepared, result = SimpleNamespace(write=None), object()
    calls = 0

    async def original(*args):
        nonlocal calls
        calls += 1
        return result

    wrapped = probe_module._operation_wrapper(original, probe)
    for _ in range(3):
        assert await wrapped(object(), prepared) is result
    assert calls == 3 and len(probe.operations) == 2 and probe.incomplete


@pytest.mark.parametrize(
    "state", ["prepared", "starting", "running", "stopping", "failed", "unknown"]
)
async def test_post_settlement_non_exited_lease_is_unavailable_without_read_or_control(state):
    probe = probe_module.Probe("A")

    class NoReadHandle:
        lease = SimpleNamespace(
            state=state, stop_reason=None, returncode=None, pid=None, sequence=1
        )

        def __getattr__(self, name):
            raise AssertionError("禁止追加读取或控制")

    operation = probe_module.Operation(
        probe, handle=NoReadHandle(), protection=object(), finished=True, data={"kind": "write"}
    )
    probe.operations.append(operation)
    await probe.post_settlement()
    await probe.post_settlement()
    assert operation.post_attempted and operation.data["post_status"] == "unavailable"
    assert probe.incomplete


async def test_closed_handle_read_failure_is_unavailable_once_without_rebuilding_owner():
    probe = probe_module.Probe("A")

    class ClosedReadHandle:
        lease = SimpleNamespace(
            state="exited", stop_reason="exited", returncode=2, pid=33, sequence=2
        )
        calls = 0

        async def _terminal_owner_receipt(self):
            self.calls += 1
            raise KernelError("process_owner_receipt_invalid", CANARY)

        def __getattr__(self, name):
            raise AssertionError("关闭对象不得重建Owner或Store")

    handle = ClosedReadHandle()
    operation = probe_module.Operation(
        probe, handle=handle, protection=object(), finished=True, data={"kind": "write"}
    )
    probe.operations.append(operation)
    await probe.post_settlement()
    await probe.post_settlement()
    assert handle.calls == 1 and operation.data["post_status"] == "unavailable"
    assert operation.data.get("original_completion_authenticated") is None
    assert operation.data["post_error_chain"][0]["code"] == "process_owner_receipt_invalid"
    assert CANARY not in probe.render()


async def test_successful_completion_never_adds_post_read_or_cleanup():
    probe = probe_module.Probe("A")
    operation = probe_module.Operation(
        probe,
        handle=object(),
        finished=True,
        data={"kind": "write", "original_completion_authenticated": True},
    )
    probe.operations.append(operation)
    await probe.post_settlement()
    assert not operation.post_attempted


async def test_unfinished_operation_never_adds_post_read():
    probe = probe_module.Probe("A")
    operation = probe_module.Operation(probe, handle=object(), data={"kind": "write"})
    probe.operations.append(operation)
    await probe.post_settlement()
    assert not operation.post_attempted


def test_post_read_uses_only_known_terminal_methods_without_execution_or_reconstruction():
    tree = ast.parse(inspect.getsource(probe_module._post_once))
    attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert {"_terminal_owner_receipt", "output", "_require_raw_bytes", "decode_proof"} <= attributes
    assert not attributes & {"refresh", "wait", "stop", "aclose", "run", "start", "sleep", "remove"}


def test_error_chain_is_bounded_and_untrusted_types_codes_never_serialize_payload():
    error = KernelError(CANARY, CANARY)
    error.__context__ = type(CANARY, (Exception,), {})(CANARY)
    error.__context__.__context__ = error
    chain = probe_module._errors(error)
    encoded = json.dumps(chain, ensure_ascii=False)
    assert CANARY not in encoded
    assert chain[0]["code_unrecognized"] and chain[1]["type"] == "OtherException"
    assert chain[-1]["chain_cycle"]
    root = RuntimeError(CANARY)
    current = root
    for _ in range(12):
        current.__context__ = RuntimeError(CANARY)
        current = current.__context__
    assert len(probe_module._errors(root)) == probe_module.MAX_CHAIN
    assert probe_module._errors(root)[-1]["chain_truncated"]


def test_explicit_lease_projection_does_not_read_token_path_env_or_model_dump():
    lease = SimpleNamespace(
        state="exited",
        stop_reason="exited",
        returncode=2,
        pid=33,
        sequence=2,
        owner_token=CANARY,
        path=CANARY,
        environment=CANARY,
    )
    assert set(probe_module._lease(lease)) == {
        "state",
        "stop_reason",
        "returncode",
        "pid",
        "sequence",
        "provenance",
    }
    assert CANARY not in json.dumps(probe_module._lease(lease))


def test_unknown_oserror_subclass_getters_are_not_inspected():
    class UntrustedError(OSError):
        def __getattribute__(self, name):
            if name in {"errno", "winerror", "args", "filename"}:
                raise AssertionError(CANARY)
            return super().__getattribute__(name)

    record = probe_module._errors(UntrustedError(CANARY))[0]
    assert record["type"] == "OtherException"
    assert record["errno"] is None and record["winerror"] is None
    assert CANARY not in json.dumps(record)


def test_native_errno_winerror_and_unrecognized_code_preserve_only_scalars():
    error = OSError(13, CANARY, CANARY)
    error.winerror = 32
    assert probe_module._errors(error)[0]["winerror"] == 32
    kernel = KernelError("git_material_effect_unknown", CANARY)
    kernel.code = [CANARY]
    record = probe_module._errors(kernel)[0]
    assert record["code"] is None and record["code_unrecognized"]
    assert CANARY not in json.dumps(record)


def test_completion_stage_does_not_imply_outer_operation_returned(active_operation):
    _, operation = active_operation
    lease = SimpleNamespace(state="exited", stop_reason="exited", returncode=0, pid=33, sequence=2)
    probe_module._success(operation, "complete", (), SimpleNamespace(lease=lease))
    assert operation.data["original_completion_authenticated"]
    assert not operation.data.get("original_operation_returned", False)


async def test_absent_handle_and_missing_protection_never_reconstruct_or_retry():
    probe = probe_module.Probe("A")
    operation = probe_module.Operation(probe, finished=True, data={"kind": "write"})
    probe.operations.append(operation)
    await probe.post_settlement()
    await probe.post_settlement()
    assert operation.post_attempted and operation.data["post_status"] == "unavailable"
    assert probe.incomplete


def test_external_keyboard_interrupt_is_not_swallowed_by_collector(active_operation):
    probe, _ = active_operation
    error = KeyboardInterrupt()

    def interrupted():
        raise error

    with pytest.raises(KeyboardInterrupt) as raised:
        probe_module._safe(probe, interrupted)
    assert raised.value is error


def test_events_and_record_limits_are_diagnostic_only(active_operation):
    probe, operation = active_operation
    for _ in range(100):
        probe.event(operation, "cleanup", "enter")
    assert len(probe.events) == probe_module.MAX_EVENTS and probe.truncated
    probe.source_sha256 = {"trusted_module": "a" * 70000}
    record = json.loads(probe.render().removeprefix(probe_module.PREFIX))
    assert record["diagnostic_incomplete"] and record["diagnostic_truncated"]
    assert len(probe.render()) < probe_module.MAX_RECORD_BYTES


@pytest.mark.parametrize(
    "failure", ["write_error", "short_write", "flush_error", "serialize_error"]
)
def test_sink_failure_is_incomplete_without_second_write_or_business_replay(monkeypatch, failure):
    probe = probe_module.Probe("A")

    class BrokenSink:
        writes = 0

        def write(self, record):
            self.writes += 1
            if failure == "write_error":
                raise OSError(CANARY)
            return 0 if failure == "short_write" else len(record)

        def flush(self):
            if failure == "flush_error":
                raise OSError(CANARY)

    if failure == "serialize_error":

        def broken():
            raise OSError(CANARY)

        monkeypatch.setattr(probe, "render", broken)
    sink = BrokenSink()
    assert not probe_module._publish(probe, sink) and probe.incomplete
    assert sink.writes == (0 if failure == "serialize_error" else 1)


def test_safe_record_publishes_single_low_sensitivity_line():
    probe, stream = probe_module.Probe("B"), io.StringIO()
    assert probe_module._publish(probe, stream)
    assert stream.getvalue().count("\n") == 1
    record = json.loads(stream.getvalue().removeprefix(probe_module.PREFIX))
    assert record["selector"] == "B" and record["schema"] == "harnessix.minimum-commit-probe/v1"


def test_install_restores_all_thirteen_original_seams_and_import_alias(monkeypatch):
    from harnessix.processes import supervisor
    from harnessix.product_config import git_delivery_process as port
    from harnessix.product_config import git_material_process as material
    from tests.product_config import test_git_delivery_process as originals

    targets = [
        (originals, "_run"),
        (supervisor.PosixProcessSupervisor, "start"),
        (material, "prepare_material_stdin"),
        (port, "_complete_process"),
        (port, "_require_exit"),
        (port, "_require_raw_bytes"),
        (port, "decode_proof"),
        (material.StagedGitMaterial, "remove"),
        (supervisor.PosixProcessSupervisor, "__aexit__"),
    ]
    targets += [
        (supervisor.SupervisedProcess, name)
        for name in ("send_stdin", "close_stdin", "wait", "_terminal_owner_receipt")
    ]
    before = [getattr(owner, name) for owner, name in targets]
    probe = probe_module.Probe("A")
    with monkeypatch.context() as context:
        probe_module._install(context, probe)
        assert probe.installed_hooks == len(targets) == 13
        for (owner, name), original in zip(targets, before, strict=True):
            assert getattr(owner, name).__wrapped__ is original
        assert supervisor.WindowsProcessSupervisor.start is supervisor.PosixProcessSupervisor.start
    assert [getattr(owner, name) for owner, name in targets] == before


@pytest.mark.parametrize("count", [0, 1, 3])
def test_explicit_plugin_rejects_other_collection_without_mutating_items(count):
    items = [SimpleNamespace(nodeid="其他测试") for _ in range(count)]
    before = list(items)
    with pytest.raises(pytest.UsageError):
        probe_module.pytest_collection_modifyitems(items)
    assert items == before


def test_exact_two_selector_collection_and_default_plugin_not_loaded(pytestconfig):
    items = [SimpleNamespace(nodeid=nodeid) for nodeid in probe_module.SELECTORS]
    probe_module.pytest_collection_modifyitems(items)
    assert not pytestconfig.pluginmanager.hasplugin("tests.product_config.git_minimum_commit_probe")
    assert "git_minimum_commit_probe" not in (ROOT / "pyproject.toml").read_text()


def test_manual_carrier_pins_only_two_cases_and_preserves_limits_failure_and_isolation():
    workflow = (ROOT / ".github/workflows/windows-git-minimum-commit-probe.yml").read_text()
    original = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "on:\n  workflow_dispatch:\n" in workflow
    assert "permissions:\n  contents: read\n" in workflow
    for pin in (
        "1af3b93b6815bc44a9784bd300feb67ff0d1eeb3",
        "c771a70e6277c0a99b617c7a806ffedaca235ff9",
    ):
        assert pin in workflow and pin in original
    for nodeid in probe_module.SELECTORS:
        assert workflow.count(nodeid) == 1
    for required in (
        "persist-credentials: false",
        "timeout-minutes: 5",
        "Test-Path -LiteralPath $base",
        "GITHUB_RUN_ATTEMPT -ne '1'",
        "--basetemp $base",
        "--tb=no",
        "-s",
        "-rN",
        "--show-capture=no",
        "--no-header",
        "exit $LASTEXITCODE",
    ):
        assert required in workflow
    for forbidden in (
        "continue-on-error",
        "push:",
        "pull_request:",
        "schedule:",
        "Remove-Item",
        "secrets.",
        "docker",
        "--junitxml",
        "--showlocals",
        "/Users/",
    ):
        assert forbidden not in workflow
