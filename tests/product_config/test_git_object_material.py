"""Git 对象材料的独立验证；进程合同不替代交付或备份业务验收。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
from enum import StrEnum

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import MAX_TRANSACTION_FILE_BYTES
from harnessix.delivery.git_object_material import (
    GitObjectMaterial,
    GitObjectRead,
    decode_git_object_batch,
)
from harnessix.domain.models import ApprovalOutcome, PolicyDecisionKind
from harnessix.processes.owner_receipt import verify_owner_receipt
from harnessix.processes.supervision_contracts import MAX_PROCESS_INPUT_BYTES
from harnessix.product_config.git_delivery_process import GitDeliveryProcess, GitOperationBudget
from tests.product_config import test_git_delivery_process as process_tests

# 显式复用原异步工厂，避免直接导入与测试参数同名的 fixture 造成 F811。
make_process = process_tests.make_process
_plan = process_tests._plan
_checkpoint = process_tests._checkpoint
_run = process_tests._run
_assert_not_started = process_tests._assert_not_started
_assert_completion = process_tests._assert_completion
_changed_spec = process_tests._changed_spec
_lease = process_tests._lease
_receipt = process_tests._receipt

_MIB = 1024 * 1024
_BODY_LIMIT = 8 * _MIB
_STDOUT_LIMIT = _BODY_LIMIT + 128 + 1
_CANARY = b"git-object-material-public-error-canary"


def _body(size: int) -> bytes:
    return (bytes(range(256)) * ((size + 255) // 256))[:size]


def _oid(kind: str, body: bytes, object_format: str) -> str:
    return hashlib.new(object_format, f"{kind} {len(body)}\0".encode("ascii") + body).hexdigest()


def _request(body: bytes, object_format: str = "sha1", kind: str = "blob") -> GitObjectRead:
    return GitObjectRead(
        object_type=kind, object_id=_oid(kind, body, object_format), object_format=object_format
    )


def _frame(request: GitObjectRead, body: bytes) -> bytes:
    return f"{request.object_id} {request.object_type} {len(body)}\n".encode("ascii") + body + b"\n"


def _assert_material(material, request: GitObjectRead, body: bytes) -> None:
    assert type(material) is GitObjectMaterial
    assert material.object_type == request.object_type
    assert material.object_id == request.object_id
    assert material.object_format == request.object_format
    assert type(material.body) is bytes and material.body == body
    assert material.body_bytes == len(body)
    assert material.body_sha256 == hashlib.sha256(body).hexdigest()
    assert material.object_id == _oid(material.object_type, material.body, material.object_format)
    assert "body=" not in repr(material)


def _assert_public_failure(error: KernelError) -> None:
    assert error.code.startswith("git_")
    assert not error.retryable
    public = error.to_failure().model_dump_json()
    assert _CANARY.decode("ascii") not in public
    assert _CANARY.decode("ascii") not in str(error)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("kind", ["blob", "tree", "commit"])
@pytest.mark.parametrize("size", [0, 1, _BODY_LIMIT], ids=["empty", "one", "8MiB"])
def test_decode_exact_binary_body_and_git_oid(object_format, kind, size) -> None:
    body = _body(size)
    request = _request(body, object_format, kind)
    material = decode_git_object_batch(request, _frame(request, body))
    _assert_material(material, request, body)


def test_request_and_material_are_frozen_and_body_is_not_represented() -> None:
    body = _CANARY + b"\0\xff\n"
    request = _request(body)
    material = decode_git_object_batch(request, _frame(request, body))
    with pytest.raises(FrozenInstanceError):
        request.object_type = "tree"
    with pytest.raises(FrozenInstanceError):
        material.body = b"changed"
    assert _CANARY.decode("ascii") not in repr(material)
    assert "body=" not in repr(material)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_material_limits_are_fixed_to_transaction_limit(object_format) -> None:
    request = _request(b"", object_format)
    assert MAX_TRANSACTION_FILE_BYTES == _BODY_LIMIT
    assert request.max_body_bytes == MAX_TRANSACTION_FILE_BYTES
    assert request.stdout_limit == _STDOUT_LIMIT


def _malformed_frame(request: GitObjectRead, body: bytes, attack: str) -> bytes:
    oid = request.object_id.encode("ascii")
    size = str(len(body)).encode("ascii")
    valid = _frame(request, body)
    headers = {
        "invalid-type": oid + b" tag " + size,
        "wrong-type": oid + b" tree " + size,
        "uppercase-type": oid + b" BLOB " + size,
        "wrong-oid": b"0" * len(oid) + b" blob " + size,
        "uppercase-oid": oid.upper() + b" blob " + size,
        "short-oid": oid[:-1] + b" blob " + size,
        "long-oid": oid + b"0 blob " + size,
        "nonhex-oid": b"g" * len(oid) + b" blob " + size,
        "other-format-oid": b"a" * (64 if len(oid) == 40 else 40) + b" blob " + size,
        "body-only-hash": hashlib.new(request.object_format, body).hexdigest().encode()
        + b" blob "
        + size,
        "leading-zero": oid + b" blob 0" + size,
        "signed-plus": oid + b" blob +" + size,
        "negative-size": oid + b" blob -1",
        "decimal-point": oid + b" blob 1.0",
        "exponent-size": oid + b" blob 1e2",
        "unicode-digit": oid + " blob １２".encode(),
        "missing-size": oid + b" blob",
        "missing-type": oid + b" " + size,
        "extra-field": oid + b" blob " + size + b" extra",
        "leading-space": b" " + oid + b" blob " + size,
        "double-space": oid + b"  blob " + size,
        "tab-separator": oid + b"\tblob\t" + size,
        "crlf-header": oid + b" blob " + size + b"\r",
        "nul-header": oid + b" blob " + size + b"\0",
        "too-large-declared-size": oid + b" blob " + str(_BODY_LIMIT + 1).encode(),
        "long-header": oid + b" blob " + b"9" * 256,
        "short-body-declaration": oid + b" blob " + str(len(body) - 1).encode(),
        "long-body-declaration": oid + b" blob " + str(len(body) + 1).encode(),
    }
    if attack in headers:
        return headers[attack] + b"\n" + body + b"\n"
    return {
        "empty-frame": b"",
        "missing-object": oid + b" missing\n",
        "ambiguous-object": oid + b" ambiguous\n",
        "no-header-lf": oid + b" blob " + size,
        "long-no-header-lf": oid + b" blob " + b"9" * 4096,
        "truncated-body": valid[: -len(body) // 2],
        "no-body-lf": valid[:-1],
        "crlf-body-terminator": valid[:-1] + b"\r\n",
        "wrong-body-terminator": valid[:-1] + b"x",
        "trailing-lf": valid + b"\n",
        "trailing-nul": valid + b"\0",
        "trailing-data": valid + _CANARY,
        "multiple-objects": valid + valid,
        "body-hash-mismatch": valid[:-2] + bytes((body[-1] ^ 1,)) + b"\n",
    }[attack]


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize(
    "attack",
    [
        "invalid-type",
        "wrong-type",
        "uppercase-type",
        "wrong-oid",
        "uppercase-oid",
        "short-oid",
        "long-oid",
        "nonhex-oid",
        "other-format-oid",
        "body-only-hash",
        "leading-zero",
        "signed-plus",
        "negative-size",
        "decimal-point",
        "exponent-size",
        "unicode-digit",
        "missing-size",
        "missing-type",
        "extra-field",
        "leading-space",
        "double-space",
        "tab-separator",
        "crlf-header",
        "nul-header",
        "too-large-declared-size",
        "long-header",
        "short-body-declaration",
        "long-body-declaration",
        "empty-frame",
        "missing-object",
        "ambiguous-object",
        "no-header-lf",
        "long-no-header-lf",
        "truncated-body",
        "no-body-lf",
        "crlf-body-terminator",
        "wrong-body-terminator",
        "trailing-lf",
        "trailing-nul",
        "trailing-data",
        "multiple-objects",
        "body-hash-mismatch",
    ],
)
def test_decode_rejects_malicious_or_incomplete_frame_with_public_error(
    object_format, attack
) -> None:
    body = _CANARY + b"\0\xff\n"
    request = _request(body, object_format)
    with pytest.raises(KernelError) as failure:
        decode_git_object_batch(request, _malformed_frame(request, body, attack))
    _assert_public_failure(failure.value)
    expected_code = {
        "missing-object": "git_material_missing",
        "too-large-declared-size": "git_material_limit",
        "body-hash-mismatch": "git_material_oid_mismatch",
    }.get(attack, "git_material_invalid")
    assert failure.value.code == expected_code
    assert failure.value.message == "Git完整对象材料不符合固定读取契约"
    assert request.object_id not in failure.value.to_failure().model_dump_json()


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_decode_rejects_one_byte_over_body_limit_even_with_correct_oid(object_format) -> None:
    body = _body(_BODY_LIMIT + 1)
    request = _request(body, object_format)
    with pytest.raises(KernelError) as failure:
        decode_git_object_batch(request, _frame(request, body))
    _assert_public_failure(failure.value)


@pytest.mark.parametrize("convert", [bytearray, memoryview, lambda value: value.decode("latin1")])
def test_decode_requires_actual_bytes_not_mutable_or_text_buffer(convert) -> None:
    body = _CANARY
    request = _request(body)
    with pytest.raises(KernelError) as failure:
        decode_git_object_batch(request, convert(_frame(request, body)))
    _assert_public_failure(failure.value)


class _Text(str):
    pass


class _ObjectKind(StrEnum):
    BLOB = "blob"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("object_type", "tag"),
        ("object_type", "BLOB"),
        ("object_type", b"blob"),
        ("object_type", True),
        ("object_type", _Text("blob")),
        ("object_type", _ObjectKind.BLOB),
        ("object_id", b"a" * 40),
        ("object_id", True),
        ("object_id", _Text("a" * 40)),
        ("object_id", "A" * 40),
        ("object_id", "g" * 40),
        ("object_id", "a" * 39),
        ("object_id", "a" * 41),
        ("object_id", "a" * 64),
        ("object_id", "a" * 40 + "\n"),
        ("object_format", "sha256"),
        ("object_format", "SHA1"),
        ("object_format", "md5"),
        ("object_format", b"sha1"),
        ("object_format", True),
        ("object_format", _Text("sha1")),
    ],
)
def test_prepare_revalidates_actual_field_types_and_format_length(
    make_process, field, value
) -> None:
    case = make_process()
    request = _request(_CANARY)
    # 绕过构造器生成畸形材料，独立证明 prepare 在执行边界再次校验。
    object.__setattr__(request, field, value)
    with pytest.raises(KernelError) as failure:
        case.port.prepare_object_read(case.workspace, request, budget=GitOperationBudget(45))
    _assert_public_failure(failure.value)
    _assert_not_started(case)


@pytest.mark.parametrize("subclass", [False, True])
def test_prepare_rejects_duck_typed_or_subclass_request(make_process, subclass) -> None:
    class Derived(GitObjectRead):
        pass

    case = make_process()
    request = _request(b"")
    candidate = (
        Derived(request.object_type, request.object_id, request.object_format)
        if subclass
        else {"object_type": "blob", "object_id": request.object_id, "object_format": "sha1"}
    )
    with pytest.raises(KernelError) as failure:
        case.port.prepare_object_read(case.workspace, candidate, budget=GitOperationBudget(45))
    _assert_public_failure(failure.value)
    _assert_not_started(case)


@pytest.mark.parametrize("length", [40, 63, 65])
def test_prepare_revalidates_sha256_oid_length(make_process, length) -> None:
    case = make_process()
    request = _request(b"", "sha256")
    object.__setattr__(request, "object_id", "a" * length)
    with pytest.raises(KernelError) as failure:
        case.port.prepare_object_read(case.workspace, request, budget=GitOperationBudget(45))
    assert failure.value.code == "git_material_request_invalid"
    _assert_not_started(case)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("invalid", ["noncanonical-zero", "missing-empty-body-lf"])
def test_decode_empty_object_requires_canonical_zero_and_body_lf(object_format, invalid) -> None:
    request = _request(b"", object_format)
    framed = _frame(request, b"")
    framed = (
        framed.replace(b" blob 0\n", b" blob 00\n")
        if invalid == "noncanonical-zero"
        else framed[:-1]
    )
    with pytest.raises(KernelError) as failure:
        decode_git_object_batch(request, framed)
    assert failure.value.code == "git_material_invalid"
    _assert_public_failure(failure.value)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_prepare_binds_only_full_oid_batch_stdin_and_fixed_limits(
    make_process, object_format
) -> None:
    case = make_process()
    request = _request(b"\0binary\xff", object_format)
    budget = GitOperationBudget(45)
    prepared = case.port.prepare_object_read(case.workspace, request, budget=budget)
    assert prepared.material is request and prepared.budget is budget
    assert prepared.command.arguments == ("cat-file", "--batch")
    assert prepared.command.input_data == request.object_id.encode("ascii") + b"\n"
    assert prepared.command.accepted == (0,) and prepared.command.index_file is None
    assert prepared.command.timeout_seconds == 20.0
    assert prepared.spec.argv == prepared.command.argv
    assert prepared.spec.stdin == "pipe"
    assert prepared.spec.input_bytes == len(request.object_id) + 1
    assert prepared.spec.output_bytes == _STDOUT_LIMIT + _MIB
    assert dict(prepared.command.environment)["GIT_NO_LAZY_FETCH"] == "1"
    arguments = prepared.approval_arguments()
    assert (
        arguments["material"]
        == request.binding()
        == {
            "version": "git-object-read/v1",
            "object_type": "blob",
            "object_id": request.object_id,
            "object_format": object_format,
            "max_body_bytes": _BODY_LIMIT,
            "stdout_limit": _STDOUT_LIMIT,
        }
    )
    assert arguments == _plan(case, prepared).intent.arguments
    _assert_not_started(case)


def test_prepare_interfaces_have_no_caller_selected_stream_limits() -> None:
    material = inspect.signature(GitDeliveryProcess.prepare_object_read)
    assert tuple(material.parameters) == ("self", "cwd", "request", "budget", "timeout")
    assert material.parameters["timeout"].default == 20
    standard = inspect.signature(GitDeliveryProcess.prepare)
    assert not any("limit" in name or "max_body" in name for name in standard.parameters)


def _init_repository(case, object_format: str) -> None:
    result = case.runner.run(case.workspace, ("init", f"--object-format={object_format}"))
    assert result.returncode == 0
    actual = case.runner.run(case.workspace, ("rev-parse", "--show-object-format"))
    assert actual.stdout == object_format.encode("ascii") + b"\n"


def _store_object(case, body: bytes, object_format: str, kind: str = "blob") -> GitObjectRead:
    # hash-object 的写入仅建立真实仓库 fixture，不经过受控读取端口或扩大输入合同。
    stored = case.runner.run(
        case.workspace, ("hash-object", "-w", "-t", kind, "--stdin"), input_data=body
    )
    request = _request(body, object_format, kind)
    assert stored.stdout == request.object_id.encode("ascii") + b"\n"
    return request


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("size", [0, 1, 65537, _BODY_LIMIT], ids=["empty", "nul", "binary", "8MiB"])
async def test_real_git_blob_material_has_authenticated_full_raw_receipt(
    make_process, object_format, size
) -> None:
    case = make_process()
    _init_repository(case, object_format)
    body = _body(size)
    request = _store_object(case, body, object_format)
    prepared = case.port.prepare_object_read(case.workspace, request, budget=GitOperationBudget(45))
    completion = await _run(case, prepared)
    _assert_completion(case, prepared, completion, _frame(request, body), b"")
    _assert_material(completion.material, request, body)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("kind", ["tree", "commit"])
async def test_real_git_tree_and_commit_are_verified_by_exact_object_oid(
    make_process, object_format, kind
) -> None:
    case = make_process()
    _init_repository(case, object_format)
    blob = _store_object(case, b"tree fixture\0\xff", object_format)
    tree_body = b"100644 fixture.bin\0" + bytes.fromhex(blob.object_id)
    tree = _store_object(case, tree_body, object_format, "tree")
    if kind == "tree":
        body, request = tree_body, tree
    else:
        body = (
            f"tree {tree.object_id}\n"
            "author Fixture <fixture@example.invalid> 1700000000 +0000\n"
            "committer Fixture <fixture@example.invalid> 1700000000 +0000\n"
            "\nobject-material fixture\n"
        ).encode("ascii")
        request = _store_object(case, body, object_format, "commit")
    prepared = case.port.prepare_object_read(case.workspace, request, budget=GitOperationBudget(45))
    completion = await _run(case, prepared)
    _assert_completion(case, prepared, completion, _frame(request, body), b"")
    _assert_material(completion.material, request, body)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("invalid", ["wrong-type", "missing", "over-limit"])
async def test_real_git_invalid_object_never_returns_prefix_as_material(
    make_process, object_format, invalid
) -> None:
    case = make_process()
    _init_repository(case, object_format)
    if invalid == "missing":
        request = GitObjectRead(
            "blob", "0" * (40 if object_format == "sha1" else 64), object_format
        )
    else:
        body = _CANARY if invalid == "wrong-type" else _body(_BODY_LIMIT + 1)
        request = _store_object(case, body, object_format)
        if invalid == "wrong-type":
            request = replace(request, object_type="tree")
    prepared = case.port.prepare_object_read(case.workspace, request, budget=GitOperationBudget(45))
    with pytest.raises(KernelError) as failure:
        await _run(case, prepared)
    _assert_public_failure(failure.value)
    lease = _lease(case, prepared)
    assert lease.state == "exited"
    assert lease.stop_reason in {"exited", "output_limit"}


@pytest.mark.parametrize(
    "attack",
    ["multiple-objects", "trailing-data", "no-body-lf", "body-hash-mismatch", "missing-object"],
)
async def test_authenticated_raw_frame_still_requires_single_valid_git_object(
    make_process, attack
) -> None:
    # 固定测试程序仅注入协议异常；真实 Owner 回执不能替代对象内容与 framing 验证。
    body = _CANARY + b"\0\xff\n"
    request = _request(body)
    framed = _malformed_frame(request, body, attack)
    case = make_process(python_code=f"import sys; sys.stdout.buffer.write({framed!r})")
    prepared = case.port.prepare_object_read(case.workspace, request, budget=GitOperationBudget(45))
    with pytest.raises(KernelError) as failure:
        await _run(case, prepared)
    _assert_public_failure(failure.value)
    lease, receipt = _lease(case, prepared), _receipt(case, prepared)
    assert lease.state == "exited" and lease.returncode == 0
    assert receipt.raw_stdout.eof
    assert receipt.raw_stdout.observed_bytes == len(framed)
    assert receipt.raw_stdout.sha256 == hashlib.sha256(framed).hexdigest()
    assert (
        verify_owner_receipt(
            receipt,
            owner_token=lease.owner_token,
            process_id=lease.process_id,
            owner_identity=lease.owner_identity,
        )
        == receipt
    )


@pytest.mark.parametrize("field", ["mac", "sha256", "eof", "observed_bytes"])
async def test_real_object_receipt_authentication_rejects_modified_raw_facts(
    make_process, field
) -> None:
    case = make_process()
    _init_repository(case, "sha1")
    body = b"authenticated material\0\xff"
    request = _store_object(case, body, "sha1")
    prepared = case.port.prepare_object_read(case.workspace, request, budget=GitOperationBudget(45))
    completion = await _run(case, prepared)
    _assert_completion(case, prepared, completion, _frame(request, body), b"")
    receipt, lease = completion.receipt, completion.lease
    if field == "mac":
        modified = receipt.model_copy(update={"mac": "0" * 64})
    else:
        changes = {"sha256": "0" * 64, "eof": False, "observed_bytes": len(completion.stdout) + 1}
        raw = receipt.raw_stdout.model_copy(update={field: changes[field]})
        modified = receipt.model_copy(update={"raw_stdout": raw})
    with pytest.raises(KernelError) as failure:
        verify_owner_receipt(
            modified,
            owner_token=lease.owner_token,
            process_id=lease.process_id,
            owner_identity=lease.owner_identity,
        )
    assert failure.value.code == "process_owner_receipt_invalid"


@pytest.mark.parametrize("approval", ["missing", "rejected", "deny", "another-plan"])
async def test_material_read_without_original_approval_never_starts(make_process, approval) -> None:
    case = make_process()
    prepared = case.port.prepare_object_read(
        case.workspace, _request(b""), budget=GitOperationBudget(45)
    )
    decision = (
        PolicyDecisionKind.DENY if approval == "deny" else PolicyDecisionKind.REQUIRE_APPROVAL
    )
    plan = _plan(case, prepared, decision=decision)
    checkpoint = None
    if approval == "rejected":
        checkpoint = _checkpoint(plan, ApprovalOutcome.REJECTED)
    elif approval == "deny":
        checkpoint = _checkpoint(plan)
    elif approval == "another-plan":
        checkpoint = _checkpoint(_plan(case, prepared))
    with pytest.raises(KernelError) as failure:
        await case.port.run(
            prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=checkpoint
        )
    assert failure.value.code == "approval_required"
    _assert_not_started(case)


async def test_same_oid_different_type_changes_material_approval_not_command(make_process) -> None:
    case = make_process()
    request = _request(b"same oid")
    first = case.port.prepare_object_read(case.workspace, request, budget=GitOperationBudget(45))
    second = replace(first, material=replace(request, object_type="tree"))
    assert first.command == second.command and first.spec == second.spec
    assert first.command.digest == second.command.digest
    assert first.approval_arguments() != second.approval_arguments()
    plan = _plan(case, first)
    with pytest.raises(KernelError) as failure:
        await case.port.run(
            second, plan, CancelToken(), budget=first.budget, checkpoint=_checkpoint(plan)
        )
    assert failure.value.code == "git_process_plan_mismatch"
    _assert_not_started(case)


async def test_same_oid_with_modified_format_is_rejected_before_start(make_process) -> None:
    case = make_process()
    first = case.port.prepare_object_read(
        case.workspace, _request(b""), budget=GitOperationBudget(45)
    )
    plan = _plan(case, first)
    changed_request = _request(b"")
    object.__setattr__(changed_request, "object_format", "sha256")
    tampered = replace(first, material=changed_request)
    with pytest.raises(KernelError) as failure:
        await case.port.run(
            tampered, plan, CancelToken(), budget=first.budget, checkpoint=_checkpoint(plan)
        )
    _assert_public_failure(failure.value)
    _assert_not_started(case)


@pytest.mark.parametrize(
    "change", ["no-lf", "uppercase", "second-oid", "empty", "closed", "revision"]
)
async def test_even_reapproved_wrong_material_stdin_is_rejected(make_process, change) -> None:
    case = make_process()
    request = _request(b"material")
    prepared = case.port.prepare_object_read(case.workspace, request, budget=GitOperationBudget(45))
    stdin = {
        "no-lf": request.object_id.encode(),
        "uppercase": request.object_id.upper().encode() + b"\n",
        "second-oid": prepared.command.input_data * 2,
        "empty": b"",
        "closed": None,
        "revision": b"HEAD\n",
    }[change]
    command = case.runner.prepare_command(case.workspace, ("cat-file", "--batch"), input_data=stdin)
    spec = _changed_spec(
        prepared.spec,
        stdin="closed" if stdin is None else "pipe",
        input_bytes=0 if stdin is None else max(1, len(stdin)),
    )
    tampered = replace(prepared, command=command, spec=spec)
    plan = _plan(case, tampered)
    with pytest.raises(KernelError) as failure:
        await case.port.run(
            tampered, plan, CancelToken(), budget=prepared.budget, checkpoint=_checkpoint(plan)
        )
    _assert_public_failure(failure.value)
    _assert_not_started(case)


@pytest.mark.parametrize(
    "change", ["stdout-limit", "excess-output", "input-size", "argv", "no-material"]
)
async def test_even_reapproved_changed_material_spec_cannot_start(make_process, change) -> None:
    case = make_process()
    prepared = case.port.prepare_object_read(
        case.workspace, _request(b""), budget=GitOperationBudget(45)
    )
    changes = {
        "stdout-limit": {"output_bytes": 2 * _MIB},
        "excess-output": {"output_bytes": prepared.spec.output_bytes + 1},
        "input-size": {"input_bytes": prepared.spec.input_bytes + 1},
        "argv": {"argv": (*prepared.spec.argv[:-1], "--batch-check")},
        "no-material": {},
    }[change]
    tampered = (
        replace(prepared, material=None)
        if change == "no-material"
        else replace(prepared, spec=_changed_spec(prepared.spec, **changes))
    )
    plan = _plan(case, tampered)
    with pytest.raises(KernelError) as failure:
        await case.port.run(
            tampered, plan, CancelToken(), budget=prepared.budget, checkpoint=_checkpoint(plan)
        )
    assert failure.value.code == "git_process_request_invalid"
    _assert_not_started(case)


@pytest.mark.parametrize("change", ["batch-check", "extra-argument"])
async def test_material_path_cannot_authorize_other_git_commands(make_process, change) -> None:
    case = make_process()
    prepared = case.port.prepare_object_read(
        case.workspace, _request(b""), budget=GitOperationBudget(45)
    )
    arguments = (
        ("cat-file", "--batch-check")
        if change == "batch-check"
        else ("cat-file", "--batch", "HEAD")
    )
    command = case.runner.prepare_command(
        case.workspace, arguments, input_data=prepared.command.input_data
    )
    tampered = replace(
        prepared, command=command, spec=_changed_spec(prepared.spec, argv=command.argv)
    )
    plan = _plan(case, tampered)
    with pytest.raises(KernelError) as failure:
        await case.port.run(
            tampered, plan, CancelToken(), budget=prepared.budget, checkpoint=_checkpoint(plan)
        )
    _assert_public_failure(failure.value)
    _assert_not_started(case)


@pytest.mark.parametrize("change", ["plan-cwd", "physical-cwd"])
async def test_material_read_rejects_wrong_or_replaced_workspace_cwd(make_process, change) -> None:
    case = make_process()
    (case.workspace / "nested").mkdir()
    prepared = case.port.prepare_object_read(
        case.workspace, _request(b""), budget=GitOperationBudget(45)
    )
    plan = _plan(case, prepared, cwd="nested" if change == "plan-cwd" else ".")
    if change == "physical-cwd":
        case.workspace.rename(case.workspace.with_name("original-workspace"))
        case.workspace.mkdir()
    with pytest.raises(KernelError) as failure:
        await case.port.run(
            prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=_checkpoint(plan)
        )
    assert failure.value.code == (
        "git_process_plan_mismatch" if change == "plan-cwd" else "git_command_binding_changed"
    )
    _assert_not_started(case)


@pytest.mark.parametrize("location", ["body", "oid"])
async def test_real_material_matched_redaction_rejects_instead_of_exposing_body(
    make_process, location
) -> None:
    body = _CANARY + b"\0\xff\n"
    expected_request = _request(body)
    protected = _CANARY if location == "body" else expected_request.object_id.encode()
    case = make_process(output_redaction=process_tests._Redaction(protected))
    _init_repository(case, "sha1")
    request = _store_object(case, body, "sha1")
    prepared = case.port.prepare_object_read(case.workspace, request, budget=GitOperationBudget(45))
    with pytest.raises(KernelError) as failure:
        await _run(case, prepared)
    assert failure.value.code == "git_process_output_changed"
    lease, receipt = _lease(case, prepared), _receipt(case, prepared)
    raw = _frame(request, body)
    assert receipt.raw_stdout.eof and receipt.raw_stdout.observed_bytes == len(raw)
    assert receipt.raw_stdout.sha256 == hashlib.sha256(raw).hexdigest()
    persisted = (
        case.state / "process-owner/runs" / str(lease.process_id) / "stdout.bin"
    ).read_bytes()
    assert protected not in persisted
    assert persisted == raw.replace(protected, b"[REDACTED]")
    assert persisted != raw


@pytest.mark.parametrize("location", ["body", "stderr"])
async def test_real_owner_byte_identical_redaction_match_rejects_material(
    make_process, location
) -> None:
    protected = b"[REDACTED]"
    source = process_tests._Redaction(protected)
    if location == "body":
        body, stderr = _CANARY + b"\0" + protected + b"\0\xff\n", b""
        case = make_process(output_redaction=source)
        _init_repository(case, "sha1")
        request = _store_object(case, body, "sha1")
    else:
        # 固定测试程序仅注入 stderr；执行、脱敏和回执均由原真实 Owner 完成。
        body, stderr = b"ordinary-object\0\xff", _CANARY + b"\0" + protected + b"\0\xff\n"
        request = _request(body)
        code = f"import sys; sys.stdout.buffer.write({_frame(request, body)!r})"
        code += f"; sys.stderr.buffer.write({stderr!r})"
        case = make_process(python_code=code, output_redaction=source)
    prepared = case.port.prepare_object_read(case.workspace, request, budget=GitOperationBudget(45))
    completion, rejection = None, None
    try:
        completion = await _run(case, prepared)
    except KernelError as error:
        rejection = error

    # 先校验真实退出与认证材料，确保 RED 只源于缺少拒绝，而不是伪造或不完整回执。
    lease, receipt = _lease(case, prepared), _receipt(case, prepared)
    assert lease.state == "exited" and lease.stop_reason == "exited" and lease.returncode == 0
    assert (
        verify_owner_receipt(
            receipt,
            owner_token=lease.owner_token,
            process_id=lease.process_id,
            owner_identity=lease.owner_identity,
        )
        == receipt
    )
    stdout = _frame(request, body)
    matched_stream = stdout if location == "body" else stderr
    assert matched_stream.count(protected) == 1
    for stream, expected in (("stdout", stdout), ("stderr", stderr)):
        raw, observed = getattr(receipt, f"raw_{stream}"), getattr(lease, stream)
        digest = hashlib.sha256(expected).hexdigest()
        assert raw.eof and raw.observed_bytes == len(expected) and raw.sha256 == digest
        assert observed.eof and not observed.truncated
        assert observed.persisted_bytes == observed.observed_bytes == len(expected)
        assert observed.persisted_sha256 == observed.sha256 == digest
        persisted = (
            case.state / "process-owner/runs" / str(lease.process_id) / f"{stream}.bin"
        ).read_bytes()
        assert persisted == expected.replace(protected, b"[REDACTED]") == expected
    receipt_json = receipt.model_dump_json(warnings="error")
    assert _CANARY.decode("ascii") not in receipt_json
    assert protected.decode("ascii") not in receipt_json
    if completion is not None:
        _assert_completion(case, prepared, completion, stdout, stderr)
        _assert_material(completion.material, request, body)
    assert rejection is not None, "保护值命中即使替换字节相同，也必须拒绝返回 Git 材料"
    assert completion is None
    assert rejection.code == "git_process_output_changed"
    _assert_public_failure(rejection)


class _RotatingRedaction:
    def __init__(self) -> None:
        self.calls = 0
        self.value = b"absent-rotating-protection-canary"

    def output_redaction_values(self) -> tuple[bytes, ...]:
        self.calls += 1
        snapshot = (self.value,)
        self.value = (
            b"absent-rotating-protection-canary" if self.value == b"[REDACTED]" else b"[REDACTED]"
        )
        return snapshot


async def test_real_owner_freezes_protection_once_per_execution_despite_source_rotation(
    make_process,
) -> None:
    source = _RotatingRedaction()
    case = make_process(output_redaction=source)
    _init_repository(case, "sha256")
    body = b"object with frozen protection\0[REDACTED]\0\xff"
    request = _store_object(case, body, "sha256")
    first = case.port.prepare_object_read(case.workspace, request, budget=GitOperationBudget(45))
    # 首次读取后 Source 已旋转为命中值，但本次 Owner 与返回检查仍须使用未命中的快照。
    completion = await _run(case, first)
    assert source.calls == 1 and source.value == b"[REDACTED]"
    _assert_completion(case, first, completion, _frame(request, body), b"")
    _assert_material(completion.material, request, body)

    second = case.port.prepare_object_read(case.workspace, request, budget=GitOperationBudget(45))
    # 下一次执行重新冻结命中值；其后 Source 旋转为未命中值不能撤销此次保护。
    with pytest.raises(KernelError) as failure:
        await _run(case, second)
    assert failure.value.code == "git_process_output_changed"
    assert source.calls == 2 and source.value == b"absent-rotating-protection-canary"
    _assert_public_failure(failure.value)
    receipt = _receipt(case, second)
    expected = _frame(request, body)
    assert receipt.raw_stdout.eof and receipt.raw_stdout.observed_bytes == len(expected)
    assert receipt.raw_stdout.sha256 == hashlib.sha256(expected).hexdigest()


async def test_real_owner_empty_protection_tuple_is_frozen_once_and_preserves_material(
    make_process,
) -> None:
    class EmptyRedaction:
        calls = 0

        def output_redaction_values(self) -> tuple[bytes, ...]:
            self.calls += 1
            return ()

    source = EmptyRedaction()
    case = make_process(output_redaction=source)
    _init_repository(case, "sha1")
    body = b"no configured protection\0[REDACTED]\0\xff"
    request = _store_object(case, body, "sha1")
    prepared = case.port.prepare_object_read(case.workspace, request, budget=GitOperationBudget(45))
    completion = await _run(case, prepared)
    assert source.calls == 1
    _assert_completion(case, prepared, completion, _frame(request, body), b"")
    _assert_material(completion.material, request, body)


async def test_real_material_unmatched_redaction_preserves_full_binary_body(make_process) -> None:
    case = make_process(output_redaction=process_tests._Redaction(b"absent-material-canary"))
    _init_repository(case, "sha256")
    body = bytes(range(256)) * 257 + b"\0\xff\n"
    request = _store_object(case, body, "sha256")
    prepared = case.port.prepare_object_read(case.workspace, request, budget=GitOperationBudget(45))
    completion = await _run(case, prepared)
    _assert_completion(case, prepared, completion, _frame(request, body), b"")
    _assert_material(completion.material, request, body)


async def test_material_stderr_matched_redaction_also_rejects_valid_stdout(make_process) -> None:
    body = b"valid material\0"
    request = _request(body)
    framed = _frame(request, body)
    case = make_process(
        python_code=(
            f"import sys; sys.stdout.buffer.write({framed!r}); sys.stderr.buffer.write({_CANARY!r})"
        ),
        output_redaction=process_tests._Redaction(_CANARY),
    )
    prepared = case.port.prepare_object_read(case.workspace, request, budget=GitOperationBudget(45))
    with pytest.raises(KernelError) as failure:
        await _run(case, prepared)
    assert failure.value.code == "git_process_output_changed"
    receipt = _receipt(case, prepared)
    assert receipt.raw_stdout.sha256 == hashlib.sha256(framed).hexdigest()
    assert receipt.raw_stderr.eof
    assert receipt.raw_stderr.sha256 == hashlib.sha256(_CANARY).hexdigest()


@pytest.mark.parametrize("size", [_MIB, _MIB + 1])
async def test_material_read_keeps_original_stderr_limit(make_process, size) -> None:
    body = b"small material\0"
    request = _request(body)
    frame = _frame(request, body)
    code = f"import sys; sys.stdout.buffer.write({frame!r})"
    code += f"; sys.stderr.buffer.write(b'e'*{size})"
    case = make_process(python_code=code)
    prepared = case.port.prepare_object_read(case.workspace, request, budget=GitOperationBudget(45))
    if size == _MIB:
        completion = await _run(case, prepared)
        _assert_completion(case, prepared, completion, frame, b"e" * size)
        _assert_material(completion.material, request, body)
    else:
        with pytest.raises(KernelError) as failure:
            await _run(case, prepared)
        assert failure.value.code == "git_command_failed"
        assert _receipt(case, prepared).raw_stderr.observed_bytes == size


def test_standard_prepare_still_rejects_eight_mib_stdin_before_start(make_process) -> None:
    case = make_process()
    assert MAX_PROCESS_INPUT_BYTES == _MIB
    with pytest.raises(KernelError) as failure:
        case.port.prepare(
            case.workspace,
            ("hash-object", "--stdin"),
            budget=GitOperationBudget(45),
            input_data=_body(_BODY_LIMIT),
        )
    assert failure.value.code == "git_process_input_limit"
    _assert_not_started(case)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
async def test_standard_cat_file_still_rejects_eight_mib_output(
    make_process, object_format
) -> None:
    case = make_process()
    _init_repository(case, object_format)
    request = _store_object(case, _body(_BODY_LIMIT), object_format)
    prepared = case.port.prepare(
        case.workspace,
        ("cat-file", "--batch"),
        budget=GitOperationBudget(45),
        input_data=request.object_id.encode() + b"\n",
    )
    assert prepared.material is None and prepared.spec.output_bytes == 2 * _MIB
    with pytest.raises(KernelError) as failure:
        await _run(case, prepared)
    assert failure.value.code == "git_command_failed"
    lease = _lease(case, prepared)
    assert lease.state == "exited" and lease.stop_reason == "output_limit"
    assert lease.stdout.truncated


async def test_standard_git_completion_has_no_object_material(make_process) -> None:
    case = make_process()
    baseline = case.runner.run(case.workspace, ("--version",))
    prepared = case.port.prepare(case.workspace, ("--version",), budget=GitOperationBudget(45))
    assert prepared.material is None
    assert dict(prepared.command.environment)["GIT_NO_LAZY_FETCH"] == "1"
    completion = await _run(case, prepared)
    assert completion.material is None
    _assert_completion(case, prepared, completion, baseline.stdout, baseline.stderr)


async def test_precancelled_material_read_creates_no_execution_state(make_process) -> None:
    case = make_process()
    prepared = case.port.prepare_object_read(
        case.workspace, _request(b""), budget=GitOperationBudget(45)
    )
    plan = _plan(case, prepared)
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(TurnCancelled):
        await case.port.run(
            prepared, plan, cancel, budget=prepared.budget, checkpoint=_checkpoint(plan)
        )
    _assert_not_started(case)


@asynccontextmanager
async def _live_material_tree(make_process, *, command_timeout=20.0):
    # 固定测试程序只验证材料通道仍复用真实 Owner 和进程树结算，不宣称 Git 业务读取。
    case = make_process(python_code=process_tests._tree_program())
    prepared = case.port.prepare_object_read(
        case.workspace, _request(b""), budget=GitOperationBudget(45), timeout=command_timeout
    )
    plan, cancel = _plan(case, prepared), CancelToken()
    task = asyncio.create_task(
        case.port.run(prepared, plan, cancel, budget=prepared.budget, checkpoint=_checkpoint(plan))
    )
    try:
        for _ in range(500):
            pids = await asyncio.to_thread(process_tests._marked_pids, case.workspace)
            if len(pids) == len(process_tests._PID_FILES):
                assert all(process_tests._process_running(pid) for pid in pids)
                break
            if task.done():
                await task
                pytest.fail("材料通道进程树未就绪即退出")
            await asyncio.sleep(0.01)
        else:
            pytest.fail("材料通道真实进程树未报告就绪")
        yield case, prepared, task, cancel, pids
    finally:
        cancel.cancel()
        await case.port.aclose()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for pid in reversed(process_tests._marked_pids(case.workspace)):
            await asyncio.to_thread(process_tests._emergency_stop, pid)


@pytest.mark.parametrize("cancellation", ["token", "task"])
async def test_material_channel_cancellation_reclaims_real_tree(make_process, cancellation) -> None:
    async with _live_material_tree(make_process) as (case, prepared, task, cancel, pids):
        if cancellation == "token":
            cancel.cancel()
            expected_error = TurnCancelled
        else:
            task.cancel()
            expected_error = asyncio.CancelledError
        with pytest.raises(expected_error):
            await asyncio.wait_for(asyncio.shield(task), 10)
        await process_tests._wait_stopped(pids)
        lease = _lease(case, prepared)
        assert lease.state == "exited" and lease.stop_reason == "cancelled"
        assert lease.pid == pids[0]
        assert process_tests._owner_receipt(case, prepared).stop_reason == "cancelled"


async def test_material_channel_timeout_reclaims_real_tree(make_process) -> None:
    async with _live_material_tree(make_process, command_timeout=3.0) as (
        case,
        prepared,
        task,
        _,
        pids,
    ):
        with pytest.raises(KernelError) as failure:
            await asyncio.wait_for(asyncio.shield(task), 10)
        assert failure.value.code == "git_process_timeout"
        await process_tests._wait_stopped(pids)
        lease = _lease(case, prepared)
        assert lease.state == "exited" and lease.stop_reason == "timeout"
        assert process_tests._owner_receipt(case, prepared).stop_reason == "timeout"


pytestmark = pytest.mark.skipif(os.name not in {"posix", "nt"}, reason="需要原平台 Process Owner")
