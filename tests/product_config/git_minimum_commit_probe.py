"""显式加载的最低 SHA256 commit 探针：只观察原测试，不授予执行权限。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any

import pytest

from tests.product_config.git_stderr_signals import (
    _STDERR_LITERALS as _STDERR_LITERALS,
)
from tests.product_config.git_stderr_signals import (
    MAX_STDERR_SIGNAL_BYTES as MAX_STDERR_SIGNAL_BYTES,
)
from tests.product_config.git_stderr_signals import (
    _stderr_signals as _stderr_signals,
)
from tests.product_config.git_trace2_projection import (
    _Trace2RoleBinding,
    diagnostic_probes_complete,
    initialize_operation_trace2,
    project_operation_trace2,
)

SELECTORS = {
    "tests/product_config/test_git_material_input.py::"
    "test_real_complete_input_and_independently_approved_readback"
    "[empty-or-valid-minimum-commit-sha256]": "A",
    "tests/product_config/test_git_material_cas_integration.py::"
    "test_complete_cas_material_original_owner_and_independent_readback"
    "[legal-minimum-commit-sha256]": "B",
}
PREFIX = "HX_MINIMUM_COMMIT_PROBE "
MAX_EVENTS = 64
MAX_CHAIN = 8
MAX_RECORD_BYTES = 64 * 1024
_ACTIVE: ContextVar[Operation | None] = ContextVar("minimum_commit_probe", default=None)
_PROBES: pytest.StashKey[Probe] = pytest.StashKey()
_SESSION_PROBES: pytest.StashKey[list[Probe]] = pytest.StashKey()
_CODES = frozenset(
    "git_material_effect_unknown git_material_stage_changed git_material_proof_invalid "
    "git_material_input_invalid git_material_input_protected git_process_unknown "
    "git_process_timeout git_process_raw_required git_process_output_changed "
    "git_command_failed git_command_binding_changed git_executable_changed "
    "git_process_request_invalid git_material_binding_changed git_material_runtime_changed "
    "git_material_namespace_invalid process_not_owned process_control_lost "
    "process_owner_receipt_invalid process_owner_receipt_missing process_owner_receipt_changed "
    "process_owner_token_invalid process_output_corrupt process_output_protection_unavailable "
    "process_input_invalid".split()
)


def _integer(value: object) -> int | None:
    return value if type(value) is int and -(2**63) <= value < 2**64 else None


def _digest(value: object) -> str | None:
    return value if type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) else None


def _choice(value: object, choices: str) -> str | None:
    return value if type(value) is str and value in choices.split() else None


def _lease(lease: Any) -> dict[str, Any]:
    return {
        "state": _choice(lease.state, "prepared starting running stopping exited failed unknown"),
        "stop_reason": _choice(
            lease.stop_reason,
            "exited cancelled timeout closed output_limit input_limit io_error "
            "launch_failed cleanup_failed unknown host_lost",
        ),
        "returncode": _integer(lease.returncode),
        "pid": _integer(lease.pid),
        "sequence": _integer(lease.sequence),
        "provenance": "cached_lease_snapshot",
    }


def _errors(error: BaseException) -> list[dict[str, Any]]:
    from harnessix.agent.cancellation import TurnCancelled
    from harnessix.agent.errors import KernelError
    from harnessix.delivery.git_material_input_contracts import GitMaterialInputError

    trusted = {
        KernelError,
        GitMaterialInputError,
        TurnCancelled,
        asyncio.CancelledError,
        OSError,
        PermissionError,
        FileNotFoundError,
        BrokenPipeError,
        TimeoutError,
        ValueError,
        TypeError,
        RuntimeError,
        AssertionError,
    }
    native_io_types = {OSError, PermissionError, FileNotFoundError, BrokenPipeError, TimeoutError}
    result, seen = [], set()
    current: BaseException | None = error
    while current is not None and len(result) < MAX_CHAIN and id(current) not in seen:
        seen.add(id(current))
        code = None
        if type(current) in {KernelError, GitMaterialInputError}:
            code = object.__getattribute__(current, "code")
        known_code = type(code) is str and code in _CODES
        result.append(
            {
                "type": type(current).__name__ if type(current) in trusted else "OtherException",
                "code": code if known_code else None,
                "code_unrecognized": code is not None and not known_code,
                "errno": _integer(current.errno) if type(current) in native_io_types else None,
                "winerror": _integer(getattr(current, "winerror", None))
                if type(current) in native_io_types
                else None,
                "suppress_context": BaseException.__suppress_context__.__get__(current),
            }
        )
        current = BaseException.__context__.__get__(current)
    if current is not None:
        result[-1]["chain_cycle"] = id(current) in seen
        result[-1]["chain_truncated"] = len(result) == MAX_CHAIN
    return result


def _receipt(receipt: Any) -> dict[str, Any]:
    from harnessix.processes.owner_receipt import ProcessOwnerReceiptV2

    if not isinstance(receipt, ProcessOwnerReceiptV2):
        raise TypeError
    result = _lease(receipt)
    result.pop("sequence")
    result.update(
        provenance="terminal_receipt_authenticated", owner_receipt_sequence=receipt.sequence
    )
    for name in ("stdout", "stderr"):
        raw = getattr(receipt, f"raw_{name}")
        result[f"raw_{name}"] = {
            "observed_bytes": _integer(raw.observed_bytes),
            "sha256": _digest(raw.sha256),
            "eof": raw.eof is True,
        }
    return result


def _proof(proof: Any, lease: Any) -> dict[str, Any]:
    return {
        "body_bytes": _integer(proof.body_bytes),
        "body_sha256": _digest(proof.body_sha256),
        "snapshot_bytes": _integer(proof.snapshot_bytes),
        "snapshot_sha256": _digest(proof.snapshot_sha256),
        "object_id": _digest(proof.object_id),
        "object_format": _choice(proof.object_format, "sha1 sha256"),
        "object_type": _choice(proof.object_type, "blob tree commit"),
        "source_eof": proof.source_eof is True,
        "git_stdout_eof": proof.git_stdout_eof is True,
        "git_returncode": _integer(proof.git_returncode),
        "producer_pid": _integer(proof.producer_pid),
        "producer_pid_matches_lease": proof.producer_pid == lease.pid,
        "contract_valid": True,
    }


@dataclass(eq=False)
class Operation:
    probe: Probe
    prepared: Any = None
    handle: Any = None
    protection: Any = None
    finished: bool = False
    raw_index: int = 0
    post_attempted: bool = False
    data: dict[str, Any] = field(default_factory=dict)


class Probe:
    """有界内存侧车；原执行结束前不写日志或读取额外回执。"""

    def __init__(self, selector: str, trace2_mode: str = "off") -> None:
        self.selector = selector
        self.trace2_mode = trace2_mode
        self.published = False
        self.origin = time.monotonic_ns()
        self.lock = threading.RLock()
        self.events: list[dict[str, Any]] = []
        self.operations: list[Operation] = []
        self.outcomes: dict[str, str] = {}
        self.source_sha256: dict[str, str] = {}
        self.incomplete = False
        self.truncated = False
        self.installed_hooks = 0
        self.trace2_roles: tuple[_Trace2RoleBinding, ...] = ()

    def event(
        self, operation: Operation, phase: str, outcome: str, error: BaseException | None = None
    ) -> None:
        if len(self.events) >= MAX_EVENTS:
            self.truncated = True
            return
        event = {
            "operation": self.operations.index(operation),
            "phase": phase,
            "outcome": outcome,
            "elapsed_ns": time.monotonic_ns() - self.origin,
        }
        if error is not None:
            event["error_chain"] = _errors(error)
        if phase == "raw":
            event["stream"] = {1: "stdout", 2: "stderr"}.get(operation.raw_index)
        self.events.append(event)

    async def post_settlement(self) -> None:
        for operation in self.operations:
            if (
                operation.data.get("kind") == "write"
                and operation.finished
                and not operation.data.get("original_completion_authenticated")
                and not operation.post_attempted
            ):
                operation.post_attempted = True
                await _post_once(operation)

    def render(self) -> str:
        record = {
            "schema": "harnessix.minimum-commit-probe/v5",
            "selector": self.selector,
            "platform": sys.platform,
            "source_sha256": self.source_sha256,
            "outcomes": self.outcomes,
            "diagnostic_incomplete": self.incomplete,
            "diagnostic_truncated": self.truncated,
            "events": self.events,
            "installed_hooks": self.installed_hooks,
            "operations": [operation.data for operation in self.operations],
        }
        encoded = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
        if len(encoded.encode("ascii")) > MAX_RECORD_BYTES:
            encoded = json.dumps(
                {
                    "schema": record["schema"],
                    "selector": self.selector,
                    "diagnostic_incomplete": True,
                    "diagnostic_truncated": True,
                }
            )
        return PREFIX + encoded + "\n"


def _safe(probe: Probe, callback: Any, *args: Any) -> None:
    try:
        with probe.lock:
            callback(*args)
    except Exception:
        probe.incomplete = True


def _before(operation: Operation, phase: str, args: tuple[Any, ...]) -> None:
    if phase == "stage":
        operation.protection = args[1]
    elif phase == "complete":
        operation.raw_index = 0
    elif phase == "raw":
        operation.raw_index += 1


def _success(operation: Operation, phase: str, args: tuple[Any, ...], result: Any) -> None:
    data = operation.data
    if phase == "start":
        operation.handle = result
        data["lease"] = _lease(result.lease)
    elif phase in {"wait", "exit_gate"}:
        data["lease"] = _lease(result if phase == "wait" else args[0])
    elif phase == "receipt":
        data["receipt"] = _receipt(result)
    elif phase == "raw":
        stream = {1: "stdout", 2: "stderr"}.get(operation.raw_index)
        if stream is None:
            raise ValueError
        data.setdefault("raw", {})[stream] = {
            "bytes": len(args[0]),
            "sha256": _digest(args[2]),
            "eof": args[3] is True,
            "full_raw_verified": True,
        }
    elif phase == "proof":
        data["proof"] = _proof(result, operation.handle.lease)
        data["proof"]["payload_bytes"] = len(args[0])
        data["proof"]["payload_sha256"] = data.get("raw", {}).get("stdout", {}).get("sha256")
    elif phase == "complete":
        data["original_completion_authenticated"] = True
        data["lease"] = _lease(result.lease)
        if data.get("kind") == "write" and operation.probe.trace2_mode != "off":
            proof = result.input_proof
            if proof is None:
                raise ValueError("成功材料写入缺少原输入证明")
            project_operation_trace2(operation, result.stderr, proof.git_returncode)
    elif phase == "operation":
        data["original_operation_returned"] = True


def _failure_snapshot(operation: Operation) -> None:
    if operation.handle is not None:
        operation.data["lease"] = _lease(operation.handle.lease)


def _async_wrapper(original: Any, phase: str) -> Any:
    @wraps(original)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        operation = _ACTIVE.get()
        if operation is None:
            return await original(*args, **kwargs)
        probe = operation.probe
        _safe(probe, _before, operation, phase, args)
        _safe(probe, probe.event, operation, phase, "enter")
        try:
            result = await original(*args, **kwargs)
        except BaseException as error:
            _safe(probe, probe.event, operation, phase, "error", error)
            _safe(probe, _failure_snapshot, operation)
            raise
        _safe(probe, _success, operation, phase, args, result)
        _safe(probe, probe.event, operation, phase, "return")
        return result

    return wrapped


def _sync_wrapper(original: Any, phase: str) -> Any:
    @wraps(original)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        operation = _ACTIVE.get()
        if operation is None:
            return original(*args, **kwargs)
        probe = operation.probe
        _safe(probe, _before, operation, phase, args)
        _safe(probe, probe.event, operation, phase, "enter")
        try:
            result = original(*args, **kwargs)
        except BaseException as error:
            _safe(probe, probe.event, operation, phase, "error", error)
            _safe(probe, _failure_snapshot, operation)
            raise
        _safe(probe, _success, operation, phase, args, result)
        _safe(probe, probe.event, operation, phase, "return")
        return result

    return wrapped


def _initialize(operation: Operation) -> None:
    write = operation.prepared.write
    operation.data.update(
        kind="write" if write is not None else "read",
        post_status="not_needed",
        original_operation_returned=False,
    )
    if write is not None:
        initialize_operation_trace2(operation)
        request = write.request
        oid = _digest(request.expected_oid)
        operation.data["input"] = {
            "object_format": _choice(request.object_format, "sha1 sha256"),
            "object_type": _choice(request.object_type, "blob tree commit"),
            "body_bytes": _integer(request.body_bytes),
            "body_sha256": _digest(request.body_sha256),
            "expected_oid": oid,
            "target_fanout": oid[:2] if oid is not None else None,
        }


def _operation_wrapper(original: Any, probe: Probe) -> Any:
    @wraps(original)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        if len(probe.operations) >= 2:
            probe.incomplete = True
            return await original(*args, **kwargs)
        operation = Operation(probe, prepared=args[1] if len(args) > 1 else kwargs.get("prepared"))
        probe.operations.append(operation)
        _safe(probe, _initialize, operation)
        token = _ACTIVE.set(operation)
        try:
            return await _async_wrapper(original, "operation")(*args, **kwargs)
        finally:
            operation.finished = True
            _ACTIVE.reset(token)

    return wrapped


async def _post_once(operation: Operation) -> None:
    from harnessix.delivery.git_material_failure import decode_failure_observation
    from harnessix.product_config import git_delivery_process as port

    data = operation.data
    data["post_status"] = "unavailable"
    handle = operation.handle
    try:
        if handle is not None:
            data["lease"] = _lease(handle.lease)
        if handle is None or handle.lease.state != "exited" or operation.protection is None:
            operation.probe.incomplete = True
            return
        receipt = await handle._terminal_owner_receipt()  # noqa: SLF001 - 原只读验真方法
        data["post_receipt"] = _receipt(receipt)
        stdout = await handle.output("stdout")
        stderr = await handle.output("stderr")
        port._require_raw_bytes(  # noqa: SLF001 - 复用原字节守卫，不替换业务结果
            stdout,
            receipt.raw_stdout.observed_bytes,
            receipt.raw_stdout.sha256,
            receipt.raw_stdout.eof,
            limit=port._stdout_limit(operation.prepared.material),
        )
        port._require_raw_bytes(  # noqa: SLF001
            stderr,
            receipt.raw_stderr.observed_bytes,
            receipt.raw_stderr.sha256,
            receipt.raw_stderr.eof,
        )
        operation.protection.require_unmatched(stdout, stderr)
        data["post_raw"] = {
            name: {
                "bytes": raw.observed_bytes,
                "sha256": raw.sha256,
                "eof": raw.eof,
                "full_raw_verified": True,
            }
            for name, raw in (("stdout", receipt.raw_stdout), ("stderr", receipt.raw_stderr))
        }
        data["post_stderr_signals"] = _stderr_signals(stderr)
        failure_status, failure = decode_failure_observation(stderr)
        data["post_worker_failure_status"] = failure_status
        if failure_status == "valid":
            data["post_worker_failure"] = failure
        elif failure_status == "invalid":
            operation.probe.incomplete = True
        project_operation_trace2(
            operation, stderr, failure["git_returncode"] if failure is not None else None
        )
        data["post_status"] = "raw_verified_only"
        if stdout:
            proof = port.decode_proof(stdout, operation.prepared.write.request)
            data["post_proof"] = _proof(proof, handle.lease)
            data["post_proof"].update(
                payload_bytes=len(stdout), payload_sha256=receipt.raw_stdout.sha256
            )
        else:
            data["post_proof"] = {"status": "absent"}
    except Exception as error:
        data["post_error_chain"] = _errors(error)
        operation.probe.incomplete = True


def _install(monkeypatch: Any, probe: Probe) -> None:
    from harnessix.delivery import (
        git_command,
        git_material_failure,
        git_material_input_contracts,
        git_material_trace2_profile,
        git_material_worker,
    )
    from harnessix.processes import supervisor
    from harnessix.product_config import git_delivery_process as port
    from harnessix.product_config import git_material_process as material
    from tests.product_config import test_git_delivery_process as original_tests
    from tests.product_config import test_git_material_cas_integration as cas_tests
    from tests.product_config import test_git_material_input as input_tests

    async_hooks = (
        (supervisor.PosixProcessSupervisor, "start", "start"),
        (material, "prepare_material_stdin", "stage"),
        (supervisor.SupervisedProcess, "send_stdin", "send"),
        (supervisor.SupervisedProcess, "close_stdin", "close"),
        (supervisor.SupervisedProcess, "wait", "wait"),
        (port, "_complete_process", "complete"),
        (supervisor.SupervisedProcess, "_terminal_owner_receipt", "receipt"),
        (supervisor.PosixProcessSupervisor, "__aexit__", "outer_exit"),
    )
    sync_hooks = (
        (port, "_require_exit", "exit_gate"),
        (port, "_require_raw_bytes", "raw"),
        (port, "decode_proof", "proof"),
        (material.StagedGitMaterial, "remove", "cleanup"),
    )
    for owner, name, phase in async_hooks:
        monkeypatch.setattr(owner, name, _async_wrapper(getattr(owner, name), phase))
        probe.installed_hooks += 1
    for owner, name, phase in sync_hooks:
        monkeypatch.setattr(owner, name, _sync_wrapper(getattr(owner, name), phase))
        probe.installed_hooks += 1
    monkeypatch.setattr(original_tests, "_run", _operation_wrapper(original_tests._run, probe))
    probe.installed_hooks += 1
    for module in (
        git_command,
        git_material_failure,
        git_material_input_contracts,
        git_material_trace2_profile,
        git_material_worker,
        port,
        material,
        supervisor,
        original_tests,
        input_tests,
        cas_tests,
        sys.modules["tests.product_config.git_stderr_signals"],
        sys.modules["tests.product_config.git_trace2_projection"],
        sys.modules[__name__],
    ):
        probe.source_sha256[module.__name__] = hashlib.sha256(
            Path(module.__file__).read_bytes()
        ).hexdigest()


def _bind_trace2_roles(probe: Probe, basetemp: object) -> None:
    """复核原预检已准备的符号；验真全部成功后才原子发布只读内存角色。"""
    from harnessix.delivery.git_identity import _executable_identity
    from scripts.windows_git_native_branch_observation.contract import read_contract
    from scripts.windows_git_native_branch_observation.identity import check_pair
    from scripts.windows_git_native_branch_observation.preflight import selected_paths

    probe.trace2_roles = ()
    if type(basetemp) is not str or not Path(basetemp).is_absolute():
        raise ValueError("verified_trace2_role_source_unavailable")
    output = Path(basetemp).parent.resolve(strict=True)
    contract = read_contract()
    selected, paths = selected_paths()
    bindings = []
    for row in contract["pairs"]:
        executable = paths[row["role"]].resolve(strict=True)
        symbols = (output / "symbols" / row["role"] / "git.pdb").resolve(strict=True)
        if not symbols.is_relative_to(output):
            raise ValueError("verified_trace2_role_source_unavailable")
        before = _executable_identity(executable)
        result = check_pair(executable, symbols, row)
        if (
            result["matched"] is not True
            or result["role"] != row["role"]
            or _executable_identity(executable) != before
        ):
            raise ValueError("verified_trace2_role_changed")
        bindings.append(_Trace2RoleBinding(str(executable), before, result["role"]))
    if (
        len(bindings) != 2
        or {binding.role for binding in bindings} != {"wrapper", "core"}
        or len({binding.executable for binding in bindings}) != 2
        or str(selected) not in {binding.executable for binding in bindings}
    ):
        raise ValueError("verified_trace2_role_source_unavailable")
    probe.trace2_roles = tuple(bindings)


def pytest_addoption(parser: Any) -> None:
    parser.addoption(
        "--git-material-trace2",
        choices=("off", "stderr-event-v1"),
        default="off",
        help="显式批准的材料 Git Trace2 诊断模式；默认关闭",
    )


def pytest_collection_modifyitems(items: list[Any]) -> None:
    if len(items) != 2 or {item.nodeid for item in items} != set(SELECTORS):
        raise pytest.UsageError("最低commit探针必须显式选择原两个案例，不接受额外或重复执行")


@pytest.fixture(autouse=True)
async def _minimum_commit_probe(request: Any, monkeypatch: Any):
    probe = Probe(SELECTORS[request.node.nodeid], request.config.getoption("--git-material-trace2"))
    request.node.stash[_PROBES] = probe
    request.session.stash.setdefault(_SESSION_PROBES, []).append(probe)
    with monkeypatch.context() as context:
        if probe.trace2_mode != "off" and sys.platform == "win32":
            _safe(probe, _bind_trace2_roles, probe, request.config.getoption("basetemp"))
        _safe(probe, _install, context, probe)
        yield
        context.undo()
        try:
            await probe.post_settlement()
        except Exception:
            probe.incomplete = True
        finally:
            probe.trace2_roles = ()
            for operation in probe.operations:
                operation.prepared = operation.handle = operation.protection = None


def _publish(probe: Probe, stream: Any) -> bool:
    try:
        record = probe.render()
        if stream.write(record) != len(record):
            raise OSError
        stream.flush()
    except Exception:
        probe.incomplete = True
        return False
    probe.published = True
    return True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any):
    result = yield
    report = result.get_result()
    probe = item.stash.get(_PROBES, None)
    if probe is None:
        return
    _safe(probe, probe.outcomes.__setitem__, report.when, report.outcome)
    if report.when == "teardown":
        if probe.outcomes.get("call") not in {"passed", "failed"} or report.outcome != "passed":
            probe.incomplete = True
        _publish(probe, sys.stdout)


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    if session.config.getoption("--git-material-trace2", default="off") != "off":
        if (
            not diagnostic_probes_complete(session.stash.get(_SESSION_PROBES, []))
            and exitstatus == 0
        ):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
