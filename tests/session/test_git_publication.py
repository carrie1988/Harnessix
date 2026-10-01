"""真实原Scope与Binding的有限Git记录认证；不认证业务历史、Git执行或公开许可。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.product_config.state_backup_validation import _VerificationOnlyScope
from harnessix.session import store_publication as codec
from harnessix.session.git_publication_contracts import GitDeliveryRecordClaims
from harnessix.session.store_publication import (
    EMPTY_PREFIX,
    MAX_HISTORY_BYTES,
    GitDeliveryPublicationSeal,
    SessionPublicationBinding,
)
from tests.session.test_publication_seal import KEY, MATERIAL, scope

KINDS = ("object_inventory", "product_link", "worktree_event", "checkpoint", "commit_event")
UUID_FIELDS = tuple(
    "delivery_id thread_id turn_id call_id route_id record_id publication_epoch".split()
)
CLAIM_FIELDS = (*UUID_FIELDS, "record_kind", "sequence", "previous_sha256")
BODY = b'{"record":"fixture-private-body","items":[1,2]}\n'
GIT_DOMAIN = b"harnessix.git-delivery-publication/v1\x00"


@pytest.mark.parametrize("close_at", ["scope", "checkpoint"])
async def test_final_issue_checkpoint_cannot_return_seal_after_binding_close(
    binding, monkeypatch, close_at
):
    """原Scope复核后的最后取消回调关闭Key，不能返回此前已签发的候选。"""
    bound, guard = binding
    token = CancelToken()
    original_scope = guard.publication_context
    original_checkpoint = token.checkpoint
    observations = {"scope_calls": 0, "armed": False}

    def observed_scope():
        result = original_scope()
        observations["scope_calls"] += 1
        if observations["scope_calls"] == 2:
            observations["armed"] = True
            if close_at == "scope":
                bound.close()
        return result

    def closing_checkpoint():
        if observations["armed"] and close_at == "checkpoint":
            bound.close()
        return original_checkpoint()

    monkeypatch.setattr(guard, "publication_context", observed_scope)
    monkeypatch.setattr(token, "checkpoint", closing_checkpoint)
    with pytest.raises(KernelError) as error:
        await bound.git.issue(_claims(), BODY, guard, cancel=token)
    assert observations == {"scope_calls": 2, "armed": True}
    assert error.value.code == "publication_key_unavailable"


class IntSubclass(int):
    pass


class BytesSubclass(bytes):
    pass


class UuidSubclass(UUID):
    pass


class StrSubclass(str):
    pass


@contextmanager
def _bound(guard, store=None, identity=None, key=KEY):
    bound = SessionPublicationBinding(store or uuid4(), identity or uuid4(), key, guard)
    try:
        yield bound
    finally:
        bound.close()


@pytest.fixture
def binding():
    with scope() as guard, _bound(guard) as bound:
        yield bound, guard


def _claims(kind="object_inventory", **updates):
    values = {name: uuid4() for name in UUID_FIELDS}
    values.update(record_kind=kind, sequence=1, previous_sha256=EMPTY_PREFIX)
    return GitDeliveryRecordClaims(**(values | updates))


def _replace(claims, **updates):
    return GitDeliveryRecordClaims(**(claims.model_dump() | updates))


def _verify(bound, seal, claims, body=BODY, checkpoint=lambda: None):
    return bound.git_verifier.verify(seal, claims, body, checkpoint=checkpoint)


async def _issue(binding, claims, body=BODY, cancel=None):
    bound, guard = binding
    return await bound.git.issue(
        claims, body, guard, cancel=CancelToken() if cancel is None else cancel
    )


@contextmanager
def _unproven():
    with pytest.raises(KernelError) as error:
        yield error
    assert error.value.code == "publication_history_unproven"
    assert error.value.__cause__ is None
    assert MATERIAL not in str(error.value) and "fixture-private-body" not in str(error.value)


def _changed(claims, field):
    if field in UUID_FIELDS:
        return _replace(claims, **{field: uuid4()})
    replacement = {"record_kind": "commit_event", "sequence": 3, "previous_sha256": "b" * 64}[field]
    return _replace(claims, **{field: replacement})


def _bad_body(attack):
    if attack == "oversized":
        return b"x" * (MAX_HISTORY_BYTES + 1)
    return {
        "bytearray": bytearray(BODY),
        "memoryview": memoryview(BODY),
        "str": BODY.decode(),
        "empty": b"",
        "subclass": BytesSubclass(BODY),
        "none": None,
    }[attack]


@pytest.mark.parametrize("kind", KINDS)
async def test_each_record_kind_authenticates_two_step_prefix_and_exact_identities(binding, kind):
    bound, _ = binding
    first = _claims(kind)
    first_seal = await _issue(binding, first)
    second = _replace(
        first, record_id=uuid4(), sequence=2, previous_sha256=hashlib.sha256(first_seal).hexdigest()
    )
    second_seal = await _issue(binding, second)
    assert _verify(bound, first_seal, first) is None
    assert _verify(bound, second_seal, second) is None
    for proof, claims in ((first_seal, second), (second_seal, first)):
        with _unproven():
            _verify(bound, proof, claims)
    with _unproven():
        _verify(bound, second_seal, _replace(second, previous_sha256="f" * 64))


async def test_seal_is_low_sensitive_original_domain_proof_not_body_or_business_permission(binding):
    claims = _claims()
    seal = await _issue(binding, claims)
    parsed = GitDeliveryPublicationSeal.model_validate_json(seal)
    assert parsed.purpose == "git_delivery_record" and parsed.version == 1
    assert parsed.claims == claims and parsed.body_bytes == len(BODY)
    assert all(type(getattr(parsed.claims, name)) is UUID for name in UUID_FIELDS)
    assert parsed.body_sha256 == hashlib.sha256(BODY).hexdigest()
    assert set(json.loads(seal)) == set(
        "version purpose store_id key_id tag claims scope_sha256 body_sha256 body_bytes".split()
    )
    assert b"fixture-private-body" not in seal and MATERIAL.encode() not in seal
    assert len(seal) <= 4096 and type(seal) is bytes
    assert codec._signed(parsed, bytearray(KEY), GIT_DOMAIN) == seal


async def test_verifier_never_issues_or_reads_current_scope(binding, monkeypatch):
    bound, guard = binding
    claims = _claims()
    seal = await _issue(binding, claims)
    assert bound.git_verifier.identity() == (bound._store_id, bound._key_id)
    assert not hasattr(bound.git_verifier, "issue")

    def forbidden(*args, **kwargs):
        pytest.fail("历史Verifier不能签发或读取当前公开许可")

    monkeypatch.setattr(type(bound.git), "issue", forbidden)
    for name in ("publication_context", "assert_public_json", "assert_public_jsonl"):
        monkeypatch.setattr(guard, name, forbidden)
    assert _verify(bound, seal, claims) is None


@pytest.mark.parametrize("field", CLAIM_FIELDS)
@pytest.mark.parametrize("change_seal", [False, True], ids=["expected-claims", "valid-MAC-seal"])
async def test_every_claim_field_is_bound_even_when_changed_seal_has_valid_mac(
    binding, field, change_seal
):
    bound, _ = binding
    claims = _claims(sequence=2, previous_sha256="a" * 64)
    seal = await _issue(binding, claims)
    changed = _changed(claims, field)
    if change_seal:
        parsed = GitDeliveryPublicationSeal.model_validate_json(seal)
        seal = codec._signed(
            parsed.model_copy(update={"claims": changed}), bytearray(KEY), GIT_DOMAIN
        )
    with _unproven():
        _verify(bound, seal, claims if change_seal else changed)


@pytest.mark.parametrize("wrong", ["store", "key-id", "key"])
async def test_wrong_binding_store_key_identity_or_key_cannot_verify(binding, wrong):
    bound, guard = binding
    claims = _claims()
    seal = await _issue(binding, claims)
    with _bound(
        guard,
        uuid4() if wrong == "store" else bound._store_id,
        uuid4() if wrong == "key-id" else bound._key_id,
        bytes(reversed(KEY)) if wrong == "key" else KEY,
    ) as other:
        with _unproven():
            _verify(other, seal, claims)


@pytest.mark.parametrize(
    "domain",
    [codec._DOMAIN, codec._ARTIFACT_DOMAIN, b"harnessix.session-event-seal/v1\x00"],
    ids=["store-header", "artifact", "event"],
)
async def test_git_shaped_proof_resigned_in_old_domains_is_not_git_proof(binding, domain):
    bound, _ = binding
    claims = _claims()
    parsed = GitDeliveryPublicationSeal.model_validate_json(await _issue(binding, claims))
    forged = codec._signed(parsed, bytearray(KEY), domain)
    with _unproven():
        _verify(bound, forged, claims)


@pytest.mark.parametrize(
    "attack",
    (
        "tag version purpose scope body-sha bytes extra claim-extra bool-bytes bool-sequence "
        "signed-sha signed-bytes"
    ).split(),
)
async def test_mac_or_json_tampering_and_extra_fields_are_fixed_unproven(binding, attack):
    bound, _ = binding
    claims = _claims()
    value = json.loads(await _issue(binding, claims))
    if attack == "claim-extra":
        value["claims"]["unexpected"] = MATERIAL
    elif attack == "bool-sequence":
        value["claims"]["sequence"] = True
    else:
        field, replacement = {
            "tag": ("tag", "f" * 64),
            "version": ("version", 2),
            "purpose": ("purpose", "store_identity"),
            "scope": ("scope_sha256", "f" * 64),
            "body-sha": ("body_sha256", "f" * 64),
            "bytes": ("body_bytes", len(BODY) + 1),
            "extra": ("unexpected", MATERIAL),
            "bool-bytes": ("body_bytes", True),
            "signed-sha": ("body_sha256", "f" * 64),
            "signed-bytes": ("body_bytes", len(BODY) + 1),
        }[attack]
        value[field] = replacement
    encoded = json.dumps(value).encode()
    if attack.startswith("signed-"):
        parsed = GitDeliveryPublicationSeal.model_validate_json(encoded)
        encoded = codec._signed(parsed, bytearray(KEY), GIT_DOMAIN)
    with _unproven():
        _verify(bound, encoded, claims)


@pytest.mark.parametrize(
    "attack",
    "empty 4097 json utf8 truncated bytearray memoryview str model subclass".split(),
)
async def test_seal_requires_actual_bounded_original_bytes(binding, attack):
    bound, _ = binding
    claims = _claims()
    valid = await _issue(binding, claims)
    bad = {
        "empty": b"",
        "4097": b" " * 4097,
        "json": b"{}",
        "utf8": b"\xff",
        "truncated": valid[:-1],
        "bytearray": bytearray(valid),
        "memoryview": memoryview(valid),
        "str": valid.decode(),
        "model": GitDeliveryPublicationSeal.model_validate_json(valid),
        "subclass": BytesSubclass(valid),
    }[attack]
    with _unproven():
        _verify(bound, bad, claims)


@pytest.mark.parametrize(
    "attack", ["bytearray", "memoryview", "str", "empty", "oversized", "subclass", "none"]
)
@pytest.mark.parametrize("operation", ["issue", "verify"])
async def test_body_exact_bytes_and_size_bounds(binding, attack, operation):
    bound, guard = binding
    claims = _claims()
    valid = await _issue(binding, claims)
    bad = _bad_body(attack)
    with _unproven():
        if operation == "issue":
            await bound.git.issue(claims, bad, guard, cancel=CancelToken())
        else:
            _verify(bound, valid, claims, bad)


async def test_same_json_meaning_different_original_bytes_is_not_same_record(binding):
    bound, _ = binding
    claims = _claims()
    seal = await _issue(binding, claims)
    for altered in (b'{ "items": [1, 2], "record": "fixture-private-body" }', BODY + b" "):
        assert json.loads(altered) == json.loads(BODY)
        with _unproven():
            _verify(bound, seal, claims, altered)


@pytest.mark.parametrize(
    "sequence,previous", [(1, EMPTY_PREFIX), (2, "a" * 64), (2**63 - 1, "b" * 64)]
)
async def test_sequence_prefix_positive_and_exact_int64_boundary(binding, sequence, previous):
    bound, _ = binding
    claims = _claims(sequence=sequence, previous_sha256=previous)
    assert _verify(bound, await _issue(binding, claims), claims) is None
    with pytest.raises(ValidationError):
        claims.sequence = 2


@pytest.mark.parametrize(
    "field,value",
    [("sequence", v) for v in (0, -1, 2**63, True, 2.0, "2", IntSubclass(2))]
    + [("previous_sha256", v) for v in ("a" * 63, "a" * 65, "A" * 64, "a" * 64 + "\n", False)]
    + [("record_kind", "tag")],
)
def test_claim_constructor_strict_types_and_canonical_digest(field, value):
    with pytest.raises(ValidationError):
        _claims(**({"sequence": 2, "previous_sha256": "a" * 64} | {field: value}))


@pytest.mark.parametrize("sequence,previous", [(1, "a" * 64), (2, EMPTY_PREFIX)])
def test_first_sequence_iff_zero_prefix_is_not_an_optional_convention(sequence, previous):
    with pytest.raises(ValidationError):
        _claims(sequence=sequence, previous_sha256=previous)


@pytest.mark.parametrize("field", UUID_FIELDS)
def test_claim_uuid_strings_are_not_python_uuid_instances(field):
    with pytest.raises(ValidationError):
        _claims(**{field: str(uuid4())})


@pytest.mark.parametrize(
    "attack",
    "copy-bool construct-bool copy-extra missing object-setattr dict fake none".split(),
)
@pytest.mark.parametrize("operation", ["issue", "verify"])
async def test_bypassed_or_forged_claim_models_are_revalidated_at_each_boundary(
    binding, attack, operation
):
    bound, guard = binding
    claims = _claims()
    seal = await _issue(binding, claims)
    if attack == "copy-bool":
        bad = claims.model_copy(update={"sequence": True})
    elif attack == "construct-bool":
        bad = GitDeliveryRecordClaims.model_construct(**(claims.model_dump() | {"sequence": True}))
    elif attack == "copy-extra":
        bad = claims.model_copy(update={"unexpected": MATERIAL})
    elif attack == "missing":
        bad = GitDeliveryRecordClaims.model_construct(**claims.model_dump(exclude={"call_id"}))
    elif attack == "object-setattr":
        bad = _replace(claims)
        object.__setattr__(bad, "previous_sha256", "a" * 64)
    else:
        bad = {
            "dict": claims.model_dump(),
            "fake": SimpleNamespace(**claims.model_dump()),
            "none": None,
        }[attack]
    with _unproven():
        if operation == "issue":
            await bound.git.issue(bad, BODY, guard, cancel=CancelToken())
        else:
            _verify(bound, seal, bad)


async def test_extra_claim_fields_and_frozen_seal_cannot_change_contract(binding):
    with pytest.raises(ValidationError):
        GitDeliveryRecordClaims(**(_claims().model_dump() | {"unexpected": MATERIAL}))
    sealed = GitDeliveryPublicationSeal.model_validate_json(await _issue(binding, _claims()))
    with pytest.raises(ValidationError):
        sealed.body_bytes = 1


@pytest.mark.parametrize("field", CLAIM_FIELDS)
@pytest.mark.parametrize(
    "method", "constructor issue-copy verify-copy issue-construct verify-construct".split()
)
async def test_scalar_subclasses_rejected_at_all_boundaries(binding, field, method):
    bound, guard = binding
    claims = _claims()
    original = getattr(claims, field)
    if field in UUID_FIELDS:
        subclass = UuidSubclass(str(original))
    else:
        subclass = {int: IntSubclass, str: StrSubclass}[type(original)](original)
    values = claims.model_dump() | {field: subclass}
    if method == "constructor":
        with pytest.raises(ValidationError):
            GitDeliveryRecordClaims(**values)
        return
    if method.endswith("copy"):
        bad = claims.model_copy(update={field: subclass})
    else:
        bad = GitDeliveryRecordClaims.model_construct(**values)
    sealed = await _issue(binding, claims)
    with _unproven():
        if method.startswith("issue"):
            await bound.git.issue(bad, BODY, guard, cancel=CancelToken())
        else:
            _verify(bound, sealed, bad)


async def test_only_low_sensitive_seal_uses_real_protect_json_not_private_body(
    binding, monkeypatch
):
    bound, guard = binding
    original = guard.assert_public_json
    seen = []

    def observing(value, *, checkpoint):
        seen.append(value)
        original(value, checkpoint=checkpoint)

    def forbidden(*args, **kwargs):
        pytest.fail("Git正文不是公开JSONL；必须只保护低敏封印")

    monkeypatch.setattr(guard, "assert_public_json", observing)
    monkeypatch.setattr(guard, "assert_public_jsonl", forbidden)
    body = MATERIAL.encode() + b"\0\xff"
    claims = _claims()
    sealed = await _issue(binding, claims, body)
    assert seen and all(v["purpose"] == "git_delivery_record" for v in seen)
    assert all(MATERIAL not in json.dumps(v) for v in seen)
    assert _verify(bound, sealed, claims, body) is None


async def test_public_seal_secret_hit_uses_original_protection_error(binding):
    bound, _ = binding
    claims = _claims()
    store, identity = bound.git_verifier.identity()
    with scope(str(claims.delivery_id)) as guard, _bound(guard, store, identity) as other:
        with pytest.raises(KernelError) as error:
            await other.git.issue(claims, BODY, guard, cancel=CancelToken())
        assert error.value.code == "public_output_secret_leak"
        assert str(claims.delivery_id) not in str(error.value)


@pytest.mark.parametrize("change", ["other-scope", "context", "closed", "close-during-protect"])
async def test_issue_requires_original_scope_through_protection(binding, change, monkeypatch):
    bound, guard = binding
    claims = _claims()
    if change == "other-scope":
        with scope("other-fixture-scope", "10") as other:
            with pytest.raises(KernelError) as error:
                await bound.git.issue(claims, BODY, other, cancel=CancelToken())
        assert error.value.code == "publication_scope_changed"
        return
    if change == "context":
        value = guard.publication_context() | {"scope_id": str(uuid4())}
        monkeypatch.setattr(guard, "publication_context", lambda: value)
    elif change == "closed":
        guard.close()
    else:
        original = guard.assert_public_json

        def closing(value, *, checkpoint):
            original(value, checkpoint=checkpoint)
            guard.close()

        monkeypatch.setattr(guard, "assert_public_json", closing)
    with pytest.raises(KernelError) as error:
        await _issue(binding, claims)
    assert error.value.code == (
        "publication_scope_changed" if change == "context" else "publication_scope_unavailable"
    )


async def test_historical_same_key_cross_scope_verifies_but_does_not_issue_under_old_scope():
    store, identity, claims = uuid4(), uuid4(), _claims()
    with scope() as old, _bound(old, store, identity) as first:
        sealed = await first.git.issue(claims, BODY, old, cancel=CancelToken())
    with scope("new-fixture-scope", "10") as current, _bound(current, store, identity) as second:
        assert _verify(second, sealed, claims) is None
        with pytest.raises(KernelError):
            await second.git.issue(claims, BODY, old, cancel=CancelToken())


async def test_real_backup_verification_only_scope_verifies_but_refuses_new_issue(binding):
    bound, _ = binding
    claims = _claims()
    sealed = await _issue(binding, claims)
    guard = _VerificationOnlyScope()
    with _bound(guard, bound._store_id, bound._key_id) as reader:
        assert _verify(reader, sealed, claims) is None
        with pytest.raises(KernelError) as error:
            await reader.git.issue(claims, BODY, guard, cancel=CancelToken())
        assert error.value.code == "public_output_protection_failed"


async def test_binding_close_revokes_both_ports_and_clears_shared_key(binding):
    bound, _ = binding
    claims = _claims()
    sealed = await _issue(binding, claims)
    bound.close()
    assert bytes(bound._key) == b"\0" * 32 and KEY == bytes(range(32))
    for method in (lambda: _verify(bound, sealed, claims), bound.git_verifier.identity):
        with pytest.raises(KernelError) as error:
            method()
        assert error.value.code == "publication_key_unavailable"
    with pytest.raises(KernelError) as error:
        await _issue(binding, claims)
    assert error.value.code == "publication_key_unavailable"


async def test_exact_64_mib_binary_body_sha_and_chunk_checkpoints(binding):
    bound, _ = binding
    body = b"\0\xff" * (MAX_HISTORY_BYTES // 2)
    assert MAX_HISTORY_BYTES == 64 * 1024 * 1024
    claims = _claims()
    token = CancelToken()
    checks = []
    token.checkpoint = lambda: checks.append(None)
    sealed = await _issue(binding, claims, body, token)
    assert len(checks) >= MAX_HISTORY_BYTES // (64 * 1024) + 2
    parsed = GitDeliveryPublicationSeal.model_validate_json(sealed)
    assert parsed.body_bytes == len(body) and parsed.body_sha256 == hashlib.sha256(body).hexdigest()
    checks.clear()
    assert _verify(bound, sealed, claims, body, lambda: checks.append(None)) is None
    assert len(checks) >= MAX_HISTORY_BYTES // (64 * 1024) + 2


@pytest.mark.parametrize(
    "exception", [RuntimeError, TimeoutError, TurnCancelled, asyncio.CancelledError, KernelError]
)
@pytest.mark.parametrize("point", ["entry", "hash", "exit"])
async def test_verifier_propagates_original_callback_exception(binding, exception, point):
    bound, _ = binding
    body = b"z" * (2 * 64 * 1024 + 1)
    claims = _claims()
    sealed = await _issue(binding, claims, body)
    calls = []
    _verify(bound, sealed, claims, body, lambda: calls.append(None))
    assert len(calls) >= 5
    target = {"entry": 1, "hash": 3, "exit": len(calls)}[point]
    failure = KernelError("fixture", "合成中止") if exception is KernelError else exception()
    count = 0

    def checkpoint():
        nonlocal count
        count += 1
        if count == target:
            raise failure

    with pytest.raises(exception) as error:
        _verify(bound, sealed, claims, body, checkpoint)
    assert error.value is failure


@pytest.mark.parametrize("point", ["entry", "hash", "exit"])
async def test_issue_explicit_cancel_checkpoint_preserves_same_turn_cancelled(binding, point):
    body = b"z" * (2 * 64 * 1024 + 1)
    claims = _claims()
    probe, token = CancelToken(), CancelToken()
    calls = []
    probe.checkpoint = lambda: calls.append(None)
    await _issue(binding, claims, body, probe)
    target = {"entry": 1, "hash": 3, "exit": len(calls)}[point]
    failure, count = TurnCancelled(), 0

    def checkpoint():
        nonlocal count
        count += 1
        if count == target:
            raise failure

    token.checkpoint = checkpoint
    with pytest.raises(TurnCancelled) as error:
        await _issue(binding, claims, body, token)
    assert error.value is failure


async def test_precancel_and_real_parent_task_cancel_have_no_proof(binding, monkeypatch):
    from harnessix.agent import publication

    token = CancelToken()
    token.cancel()
    with pytest.raises(TurnCancelled):
        await _issue(binding, _claims(), cancel=token)
    reached = asyncio.Event()

    async def waiting(seconds):
        reached.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(publication.asyncio, "sleep", waiting)
    task = asyncio.create_task(_issue(binding, _claims()))
    try:
        await asyncio.wait_for(reached.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
