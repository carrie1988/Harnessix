"""完整 Git 输入的真实 Owner/Git 验证；不冒充默认产品交付或商用验收。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git import GitDeliveryRuntime
from harnessix.delivery.git_material_input_contracts import (
    MAX_MANIFEST_BYTES,
    GitMaterialInputError,
    decode_manifest,
    encode_manifest,
)
from harnessix.delivery.git_object_material import GitObjectMaterial, GitObjectRead
from harnessix.delivery.git_store import SQLiteGitDeliveryStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import PolicyDecisionKind
from harnessix.processes.supervision_contracts import MAX_PROCESS_INPUT_BYTES
from harnessix.product_config import git_material_process as material_process
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.workspace.leases import WorkspaceLeaseStore
from tests.product_config import test_git_delivery_process as process_tests

make_process = process_tests.make_process

_LIMIT = 8 * 1024 * 1024
_CANARY = b"git-material-input-secret-canary"


class _Protection:
    def __init__(self, values=()) -> None:
        self.values = values
        self.calls = 0

    def output_redaction_values(self) -> tuple[bytes, ...]:
        self.calls += 1
        return self.values if self.calls == 1 else (b"rotated-protection-value",)


def _repository(case, tmp_path: Path, object_format: str):
    # 同步领域只用于造测试仓库/原绑定，待验证写入及回读均走原异步受控端口。
    case.runner.run(case.workspace, ("init", "-q", f"--object-format={object_format}"))
    for name, value in (("user.name", "Harnessix Test"), ("user.email", "test@harnessix.invalid")):
        case.runner.run(case.workspace, ("config", name, value))
    (case.workspace / "file.txt").write_bytes(b"baseline\n")
    case.runner.run(case.workspace, ("add", "--", "file.txt"))
    case.runner.run(case.workspace, ("commit", "-qm", "baseline"))
    return _repository_binding_existing(case, tmp_path)


def _repository_binding_existing(case, tmp_path: Path):
    # 正式领域在现有仓库重取原完整绑定，避免以手工digest代替配置核验。
    with SQLiteWorkspaceTransactionStore(tmp_path / "binding-workspace") as workspace:
        with SQLiteGitDeliveryStore(tmp_path / "binding-git") as git:
            with WorkspaceLeaseStore(tmp_path / "binding-leases.db") as leases:
                runtime = GitDeliveryRuntime(workspace, git, leases, case.runner.path)
                return runtime.bind_repository(case.workspace, "d" * 64)


def _material(body: bytes, object_format: str, kind: str) -> GitObjectMaterial:
    oid = hashlib.new(object_format, f"{kind} {len(body)}\0".encode("ascii") + body).hexdigest()
    return GitObjectMaterial(kind, oid, object_format, body)


def _body(binding, kind: str, size: int) -> bytes:
    if kind == "blob":
        return (bytes(range(256)) * ((size + 255) // 256))[:size]
    if kind == "commit":
        header = (
            f"tree {binding.head_tree_oid}\n"
            "author Harnessix Test <test@harnessix.invalid> 0 +0000\n"
            "committer Harnessix Test <test@harnessix.invalid> 0 +0000\n\n"
        ).encode("ascii")
        return header + b"x" * (max(size, len(header)) - len(header))
    if size == 0:
        return b""
    # 有序完整 tree 记录；原生解析还须通过，不采用 --literally 绕过语义校验。
    oid = bytes.fromhex(binding.head_tree_oid)
    entry_size = len(b"40000 f00000000\0") + len(oid)
    count = max(0, size // entry_size - 1)
    body = b"".join(b"40000 f%08d\0" % index + oid for index in range(count))
    remaining = size - len(body)
    return body + b"40000 " + b"z" * (remaining - 7 - len(oid)) + b"\0" + oid


def _prepare(case, binding, material, budget=None):
    return case.port.prepare_object_write(
        case.workspace,
        binding,
        case.workspace / ".git",
        material,
        source_digest="a" * 64,
        budget=budget or GitOperationBudget(45),
    )


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("kind", ["blob", "tree", "commit"])
@pytest.mark.parametrize("size", [0, _LIMIT], ids=["empty-or-valid-minimum", "8MiB"])
async def test_real_complete_input_and_independently_approved_readback(
    make_process,
    tmp_path,
    object_format,
    kind,
    size,
) -> None:
    protection = _Protection()
    case = make_process(output_redaction=protection)
    binding = _repository(case, tmp_path, object_format)
    body = _body(binding, kind, size)
    material = _material(body, object_format, kind)
    prepared = _prepare(case, binding, material)
    request = prepared.write.request
    assert prepared.spec.input_bytes <= MAX_MANIFEST_BYTES < MAX_PROCESS_INPUT_BYTES
    assert prepared.command.input_data == body
    assert not await asyncio.to_thread(Path(request.body_path).exists)
    process_tests._assert_not_started(case)
    completion = await process_tests._run(case, prepared)
    assert completion.input_proof is not None and completion.material is None
    assert completion.input_proof.producer_pid == completion.lease.pid
    assert completion.input_proof.body_bytes == len(body)
    assert completion.input_proof.snapshot_sha256 == hashlib.sha256(body).hexdigest()
    process_tests._assert_completion(
        case, prepared, completion, completion.stdout, completion.stderr
    )
    assert protection.calls == 1
    assert not await asyncio.to_thread(Path(request.body_path).exists)
    assert not await asyncio.to_thread(
        lambda: tuple(Path(request.stage_root).glob("snapshot-*.bin"))
    )
    read = case.port.prepare_object_read(
        case.workspace,
        GitObjectRead(kind, material.object_id, object_format),
        budget=prepared.budget,
    )
    assert read.spec.process_id != prepared.spec.process_id
    returned = await process_tests._run(case, read)
    assert returned.material == material
    assert protection.calls == 2


@pytest.mark.parametrize("decision", [PolicyDecisionKind.REQUIRE_APPROVAL, PolicyDecisionKind.DENY])
async def test_original_approval_refuses_before_stage_or_owner(make_process, tmp_path, decision):
    case = make_process(output_redaction=_Protection())
    binding = _repository(case, tmp_path, "sha1")
    prepared = _prepare(case, binding, _material(b"approved-body", "sha1", "blob"))
    plan = process_tests._plan(case, prepared, decision=decision)
    with pytest.raises(KernelError):
        await case.port.run(prepared, plan, CancelToken(), budget=prepared.budget)
    process_tests._assert_not_started(case, Path(prepared.write.request.body_path))


@pytest.mark.parametrize("variant", ["raw", "base64", "hex", "tail-8MiB", "marker"])
async def test_same_owner_protection_prevents_any_stage_or_git_object(
    make_process,
    tmp_path,
    variant,
):
    secret = b"[REDACTED]" if variant == "marker" else _CANARY
    values = {"base64": base64.b64encode(secret), "hex": secret.hex().encode("ascii")}
    body = values.get(variant, secret)
    if variant == "tail-8MiB":
        body = b"x" * (_LIMIT - len(secret)) + secret
    protection = _Protection((secret,))
    case = make_process(output_redaction=protection)
    binding = _repository(case, tmp_path, "sha1")
    prepared = _prepare(case, binding, _material(body, "sha1", "blob"))
    before = tuple(
        sorted(
            p.relative_to(case.workspace / ".git")
            for p in (case.workspace / ".git/objects").rglob("*")
        )
    )
    with pytest.raises(KernelError) as error:
        await process_tests._run(case, prepared)
    assert error.value.code == "git_material_input_protected"
    assert _CANARY.decode() not in error.value.to_failure().model_dump_json()
    assert protection.calls == 1
    assert not await asyncio.to_thread(Path(prepared.write.request.body_path).exists)
    after = tuple(
        sorted(
            p.relative_to(case.workspace / ".git")
            for p in (case.workspace / ".git/objects").rglob("*")
        )
    )
    assert before == after
    lease = process_tests._lease(case, prepared)
    assert lease.state == "exited" and lease.stop_reason == "cancelled"


async def test_missing_protection_refuses_before_owner(make_process, tmp_path):
    case = make_process()
    binding = _repository(case, tmp_path, "sha1")
    prepared = _prepare(case, binding, _material(b"body", "sha1", "blob"))
    with pytest.raises(KernelError) as error:
        await process_tests._run(case, prepared)
    assert error.value.code == "process_output_protection_unavailable"
    process_tests._assert_not_started(case, Path(prepared.write.request.body_path))


@pytest.mark.parametrize(
    "field",
    [
        "source_digest",
        "nonce",
        "body_sha256",
        "expected_oid",
        "purpose_digest",
        "expiry_monotonic_ns",
    ],
)
async def test_original_plan_rejects_changed_input_before_stage(make_process, tmp_path, field):
    case = make_process(output_redaction=_Protection())
    binding = _repository(case, tmp_path, "sha1")
    prepared = _prepare(case, binding, _material(b"original-body", "sha1", "blob"))
    plan = process_tests._plan(case, prepared)
    request = prepared.write.request
    old = getattr(request, field)
    changed = old + 1 if type(old) is int else ("b" if old[0] != "b" else "c") + old[1:]
    object.__setattr__(request, field, changed)
    with pytest.raises((KernelError, GitMaterialInputError)):
        await case.port.run(
            prepared,
            plan,
            CancelToken(),
            budget=prepared.budget,
            checkpoint=process_tests._checkpoint(plan),
        )
    process_tests._assert_not_started(case)


@pytest.mark.parametrize(
    "attack", ["extra", "duplicate", "newline", "array", "oversized", "bool-size", "wrong-purpose"]
)
def test_manifest_requires_exact_canonical_single_use_fields(make_process, tmp_path, attack):
    case = make_process(output_redaction=_Protection())
    binding = _repository(case, tmp_path, "sha1")
    prepared = _prepare(case, binding, _material(b"body", "sha1", "blob"))
    payload = prepared.write.control_input
    value = json.loads(payload)
    variants = {
        "duplicate": payload[:-1] + b',"nonce":"' + value["nonce"].encode() + b'"}',
        "newline": payload + b"\n",
        "array": b"[]",
        "oversized": b"x" * (MAX_MANIFEST_BYTES + 1),
    }
    if attack == "extra":
        value["extra"] = True
    elif attack == "bool-size":
        value["body_bytes"] = True
    elif attack == "wrong-purpose":
        value["purpose"] = "commit"
    altered = variants.get(
        attack, json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    )
    with pytest.raises(GitMaterialInputError):
        decode_manifest(altered)
    assert encode_manifest(prepared.write.request) == payload
    process_tests._assert_not_started(case)


@pytest.mark.parametrize(
    "location", ["config", "objects", "stage-root", "inner-argv", "runtime-argv"]
)
async def test_target_or_executable_drift_refuses_before_owner(
    make_process,
    tmp_path,
    location,
):
    case = make_process(output_redaction=_Protection())
    binding = _repository(case, tmp_path, "sha1")
    prepared = _prepare(case, binding, _material(b"body", "sha1", "blob"))
    plan = process_tests._plan(case, prepared)
    if location == "config":
        await asyncio.to_thread(lambda: (case.workspace / ".git/config").write_bytes(b"[unsafe]\n"))
    elif location in {"objects", "stage-root"}:
        path = Path(
            prepared.write.request.objects_path
            if location == "objects"
            else prepared.write.request.stage_root
        )
        await asyncio.to_thread(path.rename, path.with_name(path.name + "-old"))
        await asyncio.to_thread(path.mkdir)
    elif location == "inner-argv":
        object.__setattr__(prepared.command, "argv", (*prepared.command.argv, "--literally"))
    else:
        object.__setattr__(prepared.write, "argv", (*prepared.write.argv, "unexpected"))
    with pytest.raises((KernelError, GitMaterialInputError)):
        await case.port.run(
            prepared,
            plan,
            CancelToken(),
            budget=prepared.budget,
            checkpoint=process_tests._checkpoint(plan),
        )
    process_tests._assert_not_started(case)


@pytest.mark.parametrize("mode", ["caller-cancel", "direct-task-cancel", "deadline"])
async def test_staging_cancel_or_deadline_drains_owned_thread_before_return(
    make_process,
    tmp_path,
    monkeypatch,
    mode,
):
    case = make_process(output_redaction=_Protection())
    binding = _repository(case, tmp_path, "sha1")
    budget = GitOperationBudget(2 if mode == "deadline" else 45)
    prepared = _prepare(case, binding, _material(b"x" * _LIMIT, "sha1", "blob"), budget)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = material_process.stage_material

    def held(write, cancel, current_budget):
        entered.set()
        assert release.wait(10), "测试未释放同步 staging 屏障"
        try:
            return original(write, cancel, current_budget)
        finally:
            finished.set()

    monkeypatch.setattr(material_process, "stage_material", held)
    cancel = CancelToken()
    plan = process_tests._plan(case, prepared)
    task = asyncio.create_task(
        case.port.run(
            prepared,
            plan,
            cancel,
            budget=budget,
            checkpoint=process_tests._checkpoint(plan),
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        if mode == "caller-cancel":
            cancel.cancel()
        elif mode == "direct-task-cancel":
            case.port._active[1].cancel()
        else:
            await asyncio.sleep(budget.remaining() + 0.1)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
    expected = KernelError if mode == "deadline" else (TurnCancelled, asyncio.CancelledError)
    with pytest.raises(expected):
        await task
    assert finished.is_set() and case.port._active is None
    assert not await asyncio.to_thread(Path(prepared.write.request.body_path).exists)
    assert process_tests._lease(case, prepared).state != "running"


@pytest.mark.parametrize("fault", ["proof-invalid", "wrong-pid"])
async def test_uncertain_completion_after_control_delivery_is_not_replayed(
    make_process,
    tmp_path,
    monkeypatch,
    fault,
):
    case = make_process(output_redaction=_Protection())
    binding = _repository(case, tmp_path, "sha1")
    prepared = _prepare(case, binding, _material(b"material-object", "sha1", "blob"))
    from harnessix.product_config import git_delivery_process as port_module

    original = port_module.decode_proof
    calls = []

    def corrupt(body, request):
        calls.append(body)
        proof = original(body, request)
        if fault == "wrong-pid":
            return replace(proof, producer_pid=proof.producer_pid + 1)
        raise GitMaterialInputError("git_material_proof_invalid")

    monkeypatch.setattr(port_module, "decode_proof", corrupt)
    with pytest.raises(KernelError) as error:
        await process_tests._run(case, prepared)
    assert error.value.code == "git_material_effect_unknown"
    assert len(calls) == 1
    assert process_tests._lease(case, prepared).state == "exited"
    assert not await asyncio.to_thread(Path(prepared.write.request.body_path).exists)
    # 对象可能已存在，不能自动删除或凭原过程证明认定业务提交成功。
    read = case.port.prepare_object_read(
        case.workspace,
        GitObjectRead("blob", prepared.write.material.object_id, "sha1"),
        budget=prepared.budget,
    )
    returned = await process_tests._run(case, read)
    assert returned.material == prepared.write.material


@pytest.mark.parametrize("uncertain", [False, True])
async def test_settlement_control_loss_preserves_possible_material_effect(uncertain):
    class BrokenControl:
        calls = 0

        async def stop(self, reason):
            self.calls += 1
            raise KernelError("process_control_lost", "合成控制失联")

        async def wait(self):
            raise AssertionError("stop抛错后不应伪造wait成功")

    handle = BrokenControl()
    with pytest.raises(KernelError) as failure:
        await material_process.settle_input_failure(handle, uncertain=uncertain)
    assert failure.value.code == (
        "git_material_effect_unknown" if uncertain else "process_control_lost"
    )
    assert handle.calls == 1


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_material_capacity_plus_one_refuses_before_owner(make_process, tmp_path, object_format):
    case = make_process(output_redaction=_Protection())
    binding = _repository(case, tmp_path, object_format)
    material = _material(b"x" * _LIMIT, object_format, "blob")
    object.__setattr__(material, "body", material.body + b"x")
    with pytest.raises(KernelError):
        _prepare(case, binding, material)
    process_tests._assert_not_started(case)
