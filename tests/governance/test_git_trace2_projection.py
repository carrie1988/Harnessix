"""Trace2 纯内存投影、原认证前置与诊断完整性契约；不代表原生业务验收。"""

from __future__ import annotations

import ast
import inspect
import json
from types import SimpleNamespace

import pytest

from harnessix.delivery.git_material_trace2_profile import (
    TRACE2_ERROR_FORMATS,
    TRACE2_EVENT_SCHEMAS,
    TRACE2_EXPECTED_EXE,
    TRACE2_PROFILE_ID,
    TRACE2_PROFILE_SHA256,
)
from harnessix.product_config.git_delivery_process import GitProcessCompletion
from tests.product_config import git_trace2_projection as projection

CANARY = "opaque-private-path-token-argv-message-不得导出"
ARGV = tuple(f"{CANARY}-{index}" for index in range(22))
OUTPUT_FIELDS = {
    "profile_status",
    "completeness",
    "reason",
    "stage_witnesses",
    "error_format_ids",
    "return_consistency",
    "source_profile_id",
}


def _event(event_name, **fields):
    return {
        "event": event_name,
        "sid": CANARY,
        "thread": "main",
        "time": CANARY,
        "file": CANARY,
        "line": 42,
        **fields,
    }


def _wire(*events):
    return b"".join(
        json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        for event in events
    )


def _events(code=0):
    return [
        _event("version", evt="4", exe=TRACE2_EXPECTED_EXE),
        _event("start", t_abs=0.001, argv=list(ARGV)),
        _event("cmd_name", name="hash-object", hierarchy="hash-object"),
        _event("exit", t_abs=0.002, code=code),
        _event("atexit", t_abs=0.003, code=code),
    ]


def _project(body, **kwargs):
    return projection.project_git_trace2_events(body, ARGV, TRACE2_PROFILE_SHA256, **kwargs)


def _assert_finite(result):
    assert set(result) == OUTPUT_FIELDS
    assert CANARY not in json.dumps(result, ensure_ascii=False)
    assert result["source_profile_id"] in {None, TRACE2_PROFILE_ID}
    assert len(json.dumps(result)) < 2048


@pytest.mark.parametrize("code", [0, 128])
@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
def test_complete_single_git_stream_exports_only_fixed_witnesses(code, ending):
    body = _wire(*_events(code)).replace(b"\n", ending)
    result = _project(body, git_returncode=code)
    _assert_finite(result)
    assert result["profile_status"] == "MATCHED"
    assert result["completeness"] == "KNOWN" and result["reason"] == "NONE"
    assert result["stage_witnesses"] == ["ENTRY_START_MATCHED", "DISPATCH_HASH_OBJECT"]
    assert result["return_consistency"] == f"MATCHED_{'ZERO' if code == 0 else '128'}"


@pytest.mark.parametrize("literal,entry", TRACE2_ERROR_FORMATS.items())
def test_exact_source_format_identity_discards_dynamic_message_and_reporter(literal, entry):
    events = _events(128)
    events.insert(3, _event("error", fmt=literal, msg=CANARY))
    result = _project(_wire(*events), git_returncode=128)
    _assert_finite(result)
    assert result["error_format_ids"] == [entry.fixed_id]
    assert result["completeness"] == "KNOWN"


@pytest.mark.parametrize("name", TRACE2_EVENT_SCHEMAS)
def test_all_catalog_events_require_exact_official_key_types(name):
    schema = TRACE2_EVENT_SCHEMAS[name]
    values = {
        "string": CANARY,
        "integer": 1,
        "number": 0.1,
        "boolean": False,
        "string_array": [CANARY],
        "json": {"opaque": [CANARY, None, 1]},
    }
    event = {key: values[kind] for key, kind in schema.required.items()}
    event["event"] = name
    assert projection._valid_event(event)
    assert not projection._valid_event({**event, "unexpected": CANARY})
    for key in schema.required:
        assert not projection._valid_event({k: v for k, v in event.items() if k != key})
    for key, kind in {**schema.required, **schema.optional}.items():
        bad = True if kind in {"integer", "number"} else object()
        assert not projection._valid_event({**event, key: bad})


@pytest.mark.parametrize(
    "payload",
    [
        b'{"event":"version","event":"start"}\n',
        b"[]\n",
        b"null\n",
        b'{"value":NaN}\n',
        b'{"value":Infinity}\n',
        b'{"value":-Infinity}\n',
        b'{"event":"\xff"}\n',
        b'{"value":1e999}\n',
        b'{"value":123456789012345678901234567890123456789}\n',
        b'{"value":' + b"[" * 100 + b"0" + b"]" * 100 + b"}\n",
        b'{"value":{"key":1,"key":2}}\n',
    ],
)
def test_invalid_json_frames_are_unknown_without_leaking_contents(payload):
    result = _project(_wire(*_events()) + payload)
    _assert_finite(result)
    assert result["completeness"] == "UNKNOWN"
    assert result["reason"] == "MALFORMED_FRAME"


@pytest.mark.parametrize(
    "mutation",
    [
        {"evt": "5"},
        {"exe": "2.55.0"},
        {"exe": TRACE2_EXPECTED_EXE.upper()},
        {"evt": True},
        {"repo": 1},
        {"line": True},
        {"file": None},
    ],
)
def test_changed_profile_or_schema_is_not_accepted(mutation):
    events = _events()
    events[0].update(mutation)
    result = _project(_wire(*events))
    _assert_finite(result)
    assert result["completeness"] == "UNKNOWN"


@pytest.mark.parametrize("field", ["file", "line"])
def test_optional_reporter_fields_must_be_present_together(field):
    event = _events()[0]
    del event[field]
    assert not projection._valid_event(event)


@pytest.mark.parametrize("case", ["argv", "sid", "command", "terminal", "missing_start"])
def test_stream_binding_and_terminal_are_checked_without_new_authority(case):
    events = _events()
    if case == "argv":
        events[1]["argv"][0] += "changed"
    elif case == "sid":
        events[2]["sid"] += "changed"
    elif case == "command":
        events[2]["name"] = "other-command"
    elif case == "terminal":
        events[-1]["code"] = 128
    else:
        del events[1]
    result = _project(_wire(*events), git_returncode=0)
    _assert_finite(result)
    assert result["completeness"] == "UNKNOWN"


@pytest.mark.parametrize(
    "name",
    ["alias", "exec", "exec_result", "child_start", "child_exit", "child_ready", "too_many_files"],
)
def test_unexpected_execution_events_do_not_expand_permission(name):
    schema = TRACE2_EVENT_SCHEMAS[name]
    values = {
        "string": CANARY,
        "integer": 1,
        "number": 0.1,
        "boolean": False,
        "string_array": [CANARY],
    }
    event = {key: values[kind] for key, kind in schema.required.items()}
    event.update(event=name, sid=CANARY)
    result = _project(_wire(*_events(), event))
    assert result["completeness"] == "UNKNOWN"
    assert result["reason"] == "UNEXPECTED_EXECUTION_EVENT"
    _assert_finite(result)


@pytest.mark.parametrize(
    "suffix",
    [
        b"fatal: opaque private value\n",
        b"\xff human noise\n",
        b"\n",
        b"unfinished",
        _wire(_event("error", fmt="unknown-dynamic-format", msg=CANARY)),
        _wire(_event("unknown-event")),
    ],
)
def test_unclassified_mixed_stream_preserves_only_independent_finite_witnesses(suffix):
    result = _project(_wire(*_events(128)) + suffix, git_returncode=128)
    assert result["completeness"] == "UNKNOWN"
    assert result["stage_witnesses"] == ["ENTRY_START_MATCHED", "DISPATCH_HASH_OBJECT"]
    _assert_finite(result)


def test_worker_sentinel_and_original_valid_failure_frame_are_not_free_text():
    from harnessix.delivery.git_material_failure import (
        GitMaterialFailureObservation,
        encode_failure_observation,
    )
    from harnessix.delivery.git_material_input_contracts import GitMaterialInputError

    observation = GitMaterialFailureObservation(
        stage="git_validate", git_popen_returned=True, git_returncode=128
    )
    frame = encode_failure_observation(
        observation, GitMaterialInputError("git_material_git_failed")
    )
    body = _wire(*_events(128)) + b"git_material_worker_failed\n" + frame
    assert _project(body, git_returncode=128)["completeness"] == "KNOWN"
    assert _project(body + frame)["completeness"] == "UNKNOWN"


@pytest.mark.parametrize(
    "body",
    [b"x" * (1024 * 1024 + 1), b"{" + b" " * 65536 + b"}\n"],
    ids=["raw-oversize", "frame-oversize"],
)
def test_original_raw_and_frame_limits_fail_without_scanning_later_frames(body):
    result = _project(body + _wire(*_events()))
    assert result["completeness"] == "UNKNOWN" and result["reason"] == "LIMIT"
    assert result["stage_witnesses"] == []
    _assert_finite(result)


def test_event_limit_stops_before_later_error_witness():
    events = _events() + [_event("cmd_mode", name=CANARY)] * 59
    late = _event("error", fmt="refusing to create malformed object", msg=CANARY)
    assert len(events) == 64
    assert _project(_wire(*events))["completeness"] == "KNOWN"
    result = _project(_wire(*events, late))
    assert result["reason"] == "LIMIT" and result["error_format_ids"] == []


@pytest.mark.parametrize("profile", ["", "a" * 64, None, True])
def test_wrong_expected_profile_never_retains_dynamic_witnesses(profile):
    result = projection.project_git_trace2_events(_wire(*_events()), ARGV, profile)
    assert result["profile_status"] == "MISMATCH" and result["stage_witnesses"] == []
    _assert_finite(result)


def test_return_consistency_does_not_invent_errno_or_effect_knowledge():
    result = _project(_wire(*_events(128)), git_returncode=0)
    assert result["return_consistency"] == "MISMATCH"
    assert result["completeness"] == "UNKNOWN"
    unknown = _project(_wire(*_events(7)), git_returncode=7)
    assert unknown["return_consistency"] == "UNAVAILABLE"
    assert unknown["completeness"] == "UNKNOWN"


def test_projection_source_has_no_external_reads_execution_or_async_work():
    tree = ast.parse(inspect.getsource(projection))
    forbidden = {
        "open",
        "read_bytes",
        "read_text",
        "write_bytes",
        "write_text",
        "run",
        "Popen",
        "start",
        "sleep",
        "wait",
        "output",
        "_terminal_owner_receipt",
    }
    calls = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    assert not calls & forbidden
    assert not any(isinstance(node, (ast.Await, ast.AsyncFunctionDef)) for node in ast.walk(tree))


@pytest.mark.parametrize("boundary", ["none", "receipt", "stdout_raw", "stderr_raw", "protection"])
async def test_trace2_call_requires_original_raw_mac_eof_and_protection(monkeypatch, boundary):
    from tests.governance.test_git_minimum_commit_probe import _signal_post_operation
    from tests.product_config import git_minimum_commit_probe as probe_module

    probe, operation, calls = _signal_post_operation(monkeypatch, boundary, _wire(*_events(128)))
    probe.trace2_mode = "stderr-event-v1"
    operation.prepared.write = SimpleNamespace(
        request=SimpleNamespace(
            trace2_mode="stderr-event-v1",
            trace2_profile_sha256=TRACE2_PROFILE_SHA256,
            git_argv=ARGV,
        )
    )
    _bind_operation_role_for_test(operation)
    seen = []
    original = projection.project_git_trace2_events

    def project(*args, **kwargs):
        seen.append(tuple(calls))
        return original(*args, **kwargs)

    monkeypatch.setattr(projection, "project_git_trace2_events", project)
    await probe.post_settlement()
    await probe.post_settlement()
    assert len(seen) == (boundary == "none")
    if boundary == "none":
        assert seen[0][:6] == ("receipt", "stdout", "stderr", "raw", "raw", "protection")
        assert operation.data["post_git_trace2"]["profile_status"] == "MATCHED"
    else:
        assert "post_git_trace2" not in operation.data
    assert calls.count("receipt") == 1 and operation.data["original_operation_returned"] is False
    assert CANARY not in probe_module.Probe("A").render()


def _completed_probe(selector):
    from tests.product_config import git_minimum_commit_probe as probe_module

    probe = probe_module.Probe(selector, "stderr-event-v1")
    probe.published, probe.installed_hooks = True, 13
    probe.outcomes = {"call": "failed", "teardown": "passed"}
    record = _project(_wire(*_events(128)), git_returncode=128)
    probe.operations.append(SimpleNamespace(data={"kind": "write", "post_git_trace2": record}))
    return probe


@pytest.mark.parametrize(
    "boundary",
    [
        "none",
        "incomplete",
        "truncated",
        "publish",
        "hooks",
        "call",
        "teardown",
        "missing_write",
        "duplicate_write",
        "profile",
        "unknown",
        "off",
    ],
)
def test_session_integrity_gate_requires_two_published_original_complete_records(boundary):
    probes = [_completed_probe("A"), _completed_probe("B")]
    probe = probes[0]
    if boundary in {"incomplete", "truncated"}:
        setattr(probe, boundary, True)
    elif boundary == "publish":
        probe.published = False
    elif boundary == "hooks":
        probe.installed_hooks = 12
    elif boundary in {"call", "teardown"}:
        probe.outcomes[boundary] = "skipped"
    elif boundary == "missing_write":
        probe.operations.clear()
    elif boundary == "duplicate_write":
        probe.operations.append(probe.operations[0])
    elif boundary == "off":
        probe.trace2_mode = "off"
    elif boundary in {"profile", "unknown"}:
        key = "profile_status" if boundary == "profile" else "completeness"
        probe.operations[0].data["post_git_trace2"][key] = "UNAVAILABLE"
    assert projection.diagnostic_probes_complete(probes) is (boundary == "none")
    assert not projection.diagnostic_probes_complete(probes[:1])
    assert not projection.diagnostic_probes_complete([probes[0], probes[0]])


@pytest.mark.parametrize("exitstatus", [0, 1, 2, 3, 4, 5])
@pytest.mark.parametrize("mode", ["off", "stderr-event-v1"])
def test_sessionfinish_only_turns_zero_incomplete_diagnostic_into_nonzero(exitstatus, mode):
    from tests.product_config import git_minimum_commit_probe as probe_module

    session = SimpleNamespace(
        config=SimpleNamespace(getoption=lambda *args, **kwargs: mode),
        stash={},
        exitstatus=exitstatus,
    )
    probe_module.pytest_sessionfinish(session, exitstatus)
    assert session.exitstatus == (1 if mode != "off" and exitstatus == 0 else exitstatus)


def test_successful_original_completion_uses_existing_verified_bytes_without_post_reads(
    monkeypatch,
):
    from tests.product_config import git_minimum_commit_probe as probe_module

    probe = probe_module.Probe("A", "stderr-event-v1")
    lease = SimpleNamespace(state="exited", stop_reason="exited", returncode=0, pid=33, sequence=3)
    request = SimpleNamespace(
        trace2_mode="stderr-event-v1", trace2_profile_sha256=TRACE2_PROFILE_SHA256, git_argv=ARGV
    )
    operation = probe_module.Operation(
        probe,
        prepared=SimpleNamespace(write=SimpleNamespace(request=request)),
        finished=True,
        data={"kind": "write"},
    )
    result = GitProcessCompletion(
        lease=lease,
        receipt=SimpleNamespace(),
        stdout=b"",
        stderr=_wire(*_events()),
        input_proof=SimpleNamespace(git_returncode=0),
    )
    _bind_operation_role_for_test(operation)
    probe_module._success(operation, "complete", (), result)
    assert operation.data["original_completion_authenticated"] is True
    assert operation.data["post_git_trace2"]["return_consistency"] == "MATCHED_ZERO"
    assert not operation.post_attempted and not probe.incomplete
    assert not any(
        isinstance(node, ast.Await)
        for node in ast.walk(ast.parse(inspect.getsource(probe_module._success)))
    )


def test_default_off_never_calls_trace2_parser_or_reads_command_fields(monkeypatch):
    from tests.product_config import git_minimum_commit_probe as probe_module

    def forbidden(*args, **kwargs):
        pytest.fail("默认关闭不能读取或解释 Trace2")

    monkeypatch.setattr(projection, "project_git_trace2_events", forbidden)
    operation = SimpleNamespace(probe=probe_module.Probe("A"), data={})
    projection.project_operation_trace2(operation, b"private raw remains unparsed")
    assert operation.data["post_git_trace2"]["profile_status"] == "OFF"


def test_complete_known_carrier_keeps_original_failed_business_outcome():
    from tests.product_config import git_minimum_commit_probe as probe_module

    probes = [_completed_probe("A"), _completed_probe("B")]
    session = SimpleNamespace(
        config=SimpleNamespace(getoption=lambda *args, **kwargs: "stderr-event-v1"),
        stash={probe_module._SESSION_PROBES: probes},
        exitstatus=1,
    )
    before = [dict(probe.outcomes) for probe in probes]
    probe_module.pytest_sessionfinish(session, 1)
    assert session.exitstatus == 1 and [probe.outcomes for probe in probes] == before


def _bind_operation_role_for_test(operation, role="core"):
    # 单测显式模拟已验真来源；不作为PE/PDB、Owner或原生批准证据。
    request = operation.prepared.write.request
    request.git_executable_identity = "a" * 64
    factory = getattr(projection, "_Trace2RoleBinding", SimpleNamespace)
    operation.probe.trace2_roles = (
        factory(executable=request.git_argv[0], identity="a" * 64, role=role),
    )
    operation.prepared.command = SimpleNamespace(
        argv=request.git_argv, executable_identity="a" * 64
    )


def _role_operation(role="wrapper", code=128, object_type="blob"):
    from tests.product_config import git_minimum_commit_probe as probe_module

    probe = probe_module.Probe("A", "stderr-event-v1")
    from harnessix.delivery.git_material_worker import fixed_git_argv

    argv = fixed_git_argv("opaque-verified-launcher", "opaque-common", "opaque-hooks", object_type)
    request = SimpleNamespace(
        trace2_mode="stderr-event-v1",
        trace2_profile_sha256=TRACE2_PROFILE_SHA256,
        git_argv=argv,
    )
    operation = probe_module.Operation(
        probe,
        prepared=SimpleNamespace(write=SimpleNamespace(request=request)),
        finished=True,
        data={"kind": "write"},
    )
    probe.operations.append(operation)
    _bind_operation_role_for_test(operation, role)
    events = _events(code)
    events[1]["argv"] = list(("git.exe", *argv[1:]) if role == "wrapper" else argv)
    return operation, events


@pytest.mark.parametrize("role", ["wrapper", "core"])
@pytest.mark.parametrize("code", [0, 128])
@pytest.mark.parametrize("object_type", ["blob", "tree", "commit"])
def test_verified_role_reaches_operation_projection_without_changing_request(
    role,
    code,
    object_type,
):
    operation, events = _role_operation(role, code, object_type)
    request = operation.prepared.write.request
    original_argv = request.git_argv
    projection.project_operation_trace2(operation, _wire(*events), code)
    result = operation.data["post_git_trace2"]
    assert result["completeness"] == "KNOWN"
    assert "ENTRY_START_MATCHED" in result["stage_witnesses"]
    assert request.git_argv is original_argv
    assert set(result) == OUTPUT_FIELDS
    assert "opaque-verified-launcher" not in operation.probe.render()


@pytest.mark.parametrize("role", ["wrapper", "core"])
async def test_role_wiring_crosses_original_failure_raw_guard_once(monkeypatch, role):
    from tests.governance.test_git_minimum_commit_probe import _signal_post_operation

    bound, events = _role_operation(role)
    probe, operation, calls = _signal_post_operation(monkeypatch, "none", _wire(*events))
    probe.trace2_mode = "stderr-event-v1"
    probe.trace2_roles = bound.probe.trace2_roles
    operation.prepared.write = bound.prepared.write
    operation.prepared.command = bound.prepared.command
    await probe.post_settlement()
    await probe.post_settlement()
    assert operation.data["post_git_trace2"]["completeness"] == "KNOWN"
    assert calls[:6] == ["receipt", "stdout", "stderr", "raw", "raw", "protection"]
    assert calls.count("receipt") == 1 and calls.count("stderr") == 1
    assert operation.data["post_proof"]["status"] == "absent"
    assert operation.data["original_operation_returned"] is False


@pytest.mark.parametrize("role", ["wrapper", "core"])
def test_role_wiring_crosses_original_completion_without_extra_reads(role):
    from tests.product_config import git_minimum_commit_probe as probe_module

    operation, events = _role_operation(role, 0)
    lease = SimpleNamespace(state="exited", stop_reason="exited", returncode=0, pid=33, sequence=3)
    completion = GitProcessCompletion(
        lease=lease,
        receipt=SimpleNamespace(),
        stdout=b"",
        stderr=_wire(*events),
        input_proof=SimpleNamespace(git_returncode=0),
    )
    probe_module._success(operation, "complete", (), completion)
    assert operation.data["post_git_trace2"]["completeness"] == "KNOWN"
    assert operation.data["original_completion_authenticated"] is True
    assert not operation.post_attempted


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "wrong-type",
        "role",
        "path",
        "identity",
        "request-identity",
        "command-identity",
        "command-argv",
        "duplicate",
    ],
)
def test_role_source_must_match_exact_request_and_command(mutation):
    operation, events = _role_operation()
    binding = operation.probe.trace2_roles[0]
    if mutation == "missing":
        operation.probe.trace2_roles = ()
    elif mutation == "wrong-type":
        operation.probe.trace2_roles = (
            SimpleNamespace(
                executable=binding.executable,
                identity=binding.identity,
                role=binding.role,
            ),
        )
    elif mutation == "duplicate":
        operation.probe.trace2_roles *= 2
    elif mutation in {"role", "path", "identity"}:
        factory = type(binding)
        values = dict(executable=binding.executable, identity=binding.identity, role=binding.role)
        values[{"role": "role", "path": "executable", "identity": "identity"}[mutation]] = "wrong"
        operation.probe.trace2_roles = (factory(**values),)
    elif mutation == "request-identity":
        operation.prepared.write.request.git_executable_identity = "b" * 64
    elif mutation == "command-identity":
        operation.prepared.command.executable_identity = "b" * 64
    else:
        operation.prepared.command.argv = ("wrong", *operation.prepared.command.argv[1:])
    projection.project_operation_trace2(operation, _wire(*events), 128)
    result = operation.data["post_git_trace2"]
    assert result["completeness"] == "UNKNOWN"
    assert "ENTRY_START_MATCHED" not in result["stage_witnesses"]


@pytest.mark.parametrize("mutation", ["argv0", "tail", "sid", "duplicate", "profile", "extra"])
@pytest.mark.parametrize("role", ["wrapper", "core"])
def test_role_binding_never_relaxes_original_stream_predicate(role, mutation):
    operation, events = _role_operation(role)
    if mutation == "argv0":
        events[1]["argv"][0] = "GIT.EXE"
    elif mutation == "tail":
        events[1]["argv"][-1] = "changed"
    elif mutation == "sid":
        events[1]["sid"] = "different"
    elif mutation == "duplicate":
        events.insert(2, dict(events[1]))
    elif mutation == "profile":
        events[0]["evt"] = "5"
    else:
        events[1]["unapproved"] = "opaque"
    projection.project_operation_trace2(operation, _wire(*events), 128)
    assert operation.data["post_git_trace2"]["completeness"] == "UNKNOWN"
