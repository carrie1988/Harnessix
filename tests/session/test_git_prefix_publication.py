"""独立Git尾锚认证端口；真实Binding/Scope正控，不表示全前缀或执行授权。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.publication import protect_json
from harnessix.product_config.state_backup_validation import _VerificationOnlyScope
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.session import git_prefix_publication as prefix
from harnessix.session import store_publication as codec
from harnessix.session.git_prefix_contracts import (
    GitStorePrefixAnchorClaims,
    snapshot_git_store_prefix_claims,
)
from harnessix.session.publication_seal import PublicationScope
from harnessix.session.store_publication import GitStorePrefixAnchorSeal, SessionPublicationBinding
from tests.session.test_git_publication import _claims as original_git_claims
from tests.session.test_publication_seal import KEY, MATERIAL
from tests.session.test_publication_seal import scope as original_scope

scope = cast(Callable[..., AbstractContextManager[SecretPublicationScope]], original_scope)
Binding = tuple[SessionPublicationBinding, SecretPublicationScope]
BODY = b'{"catalog":"private-prefix-fixture","complete":true}\n'
DOMAIN = b"harnessix.git-store-prefix-anchor/v1\0"


class IntSubclass(int):
    pass


class StrSubclass(str):
    pass


class UuidSubclass(UUID):
    pass


class BytesSubclass(bytes):
    pass


def claims(**updates: object) -> GitStorePrefixAnchorClaims:
    return GitStorePrefixAnchorClaims.model_validate(
        {"schema_version": "2", "store_genesis_epoch": uuid4(), "revision": 0} | updates
    )


@contextmanager
def bound(
    guard: PublicationScope,
    store_id: UUID | None = None,
    key_id: UUID | None = None,
    key: bytes = KEY,
) -> Iterator[SessionPublicationBinding]:
    owner = SessionPublicationBinding(store_id or uuid4(), key_id or uuid4(), key, guard)
    try:
        yield owner
    finally:
        owner.close()


@pytest.fixture
def binding() -> Iterator[Binding]:
    with scope() as guard, bound(guard) as owner:
        yield owner, guard


async def issue(
    binding: Binding, value: object, body: object = BODY, cancel: CancelToken | None = None
) -> bytes:
    owner, guard = binding
    return await owner.git_prefix.issue(
        cast(GitStorePrefixAnchorClaims, value),
        body,
        guard,
        cancel=CancelToken() if cancel is None else cancel,
    )


def verify(
    owner: SessionPublicationBinding,
    seal: object,
    value: object,
    body: object = BODY,
    checkpoint: Callable[[], None] = lambda: None,
) -> None:
    owner.git_prefix_verifier.verify(
        seal, cast(GitStorePrefixAnchorClaims, value), body, checkpoint=checkpoint
    )


@contextmanager
def unproven() -> Iterator[None]:
    with pytest.raises(KernelError) as error:
        yield
    assert error.value.code == "publication_history_unproven"
    assert error.value.__cause__ is None
    assert MATERIAL not in str(error.value) and "private-prefix-fixture" not in str(error.value)


@pytest.mark.parametrize("revision", [0, 1, 2**63 - 1])
async def test_real_binding_round_trip_exact_revision_and_low_sensitive_seal(
    binding: Binding, revision: int
) -> None:
    owner, _ = binding
    value = claims(revision=revision)
    encoded = await issue(binding, value)
    sealed = GitStorePrefixAnchorSeal.model_validate_json(encoded)
    verify(owner, encoded, value)
    assert sealed.version == 1 and sealed.purpose == "git_store_prefix_anchor"
    assert sealed.claims == value and sealed.body_bytes == len(BODY)
    assert sealed.body_sha256 == hashlib.sha256(BODY).hexdigest()
    assert (sealed.store_id, sealed.key_id) == owner.git_prefix_verifier.identity()
    assert owner.git_prefix.identity() == owner.git_verifier.identity()
    assert type(encoded) is bytes and len(encoded) <= 4096
    assert encoded == codec._signed(sealed, owner._key, DOMAIN)
    assert encoded == sealed.model_dump_json().encode()
    assert set(json.loads(encoded)) == set(
        "version purpose store_id key_id tag claims scope_sha256 body_sha256 body_bytes".split()
    )
    assert b"private-prefix-fixture" not in encoded


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("schema_version", "1"),
        ("schema_version", StrSubclass("2")),
        ("store_genesis_epoch", str(uuid4())),
        ("store_genesis_epoch", UuidSubclass(str(uuid4()))),
        ("store_genesis_epoch", None),
        *(("revision", value) for value in [-1, 2**63, True, 1.0, "0", IntSubclass(0), None]),
        ("unexpected", "extra"),
    ],
)
def test_claim_constructor_requires_exact_scalar_types(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        claims(**{field: value})


@pytest.mark.parametrize("operation", ["issue", "verify"])
@pytest.mark.parametrize(
    "attack",
    [
        "construct-valid",
        "copy-valid",
        "deep-copy-valid",
        "copy-update-valid",
        "construct-bool",
        "copy-bool",
        "copy-extra",
        "missing",
        "dict",
        "fake",
        "none",
        "subclass",
        "extra-dict",
        "extra-state",
        "mutated-valid",
        "mutated-bool",
    ],
)
async def test_only_actually_validated_exact_models_cross_port_boundaries(
    binding: Binding, operation: str, attack: str
) -> None:
    owner, _ = binding
    original = claims()
    seal = await issue(binding, original)
    data = original.model_dump()
    bad: object
    if attack.startswith("construct"):
        bad = GitStorePrefixAnchorClaims.model_construct(
            **(data | ({"revision": True} if attack.endswith("bool") else {}))
        )
    elif attack in {
        "copy-valid",
        "deep-copy-valid",
        "copy-update-valid",
        "copy-bool",
        "copy-extra",
    }:
        updates: dict[str, dict[str, Any]] = {
            "copy-update-valid": {"revision": 1},
            "copy-bool": {"revision": True},
            "copy-extra": {"unexpected": "extra"},
        }
        update = updates.get(attack, {})
        bad = original.model_copy(update=update, deep=attack == "deep-copy-valid")
    elif attack == "missing":
        bad = GitStorePrefixAnchorClaims.model_construct(
            **original.model_dump(exclude={"revision"})
        )
    elif attack == "subclass":

        class DerivedClaims(GitStorePrefixAnchorClaims):
            pass

        bad = DerivedClaims.model_validate(data)
    elif attack in {"extra-dict", "extra-state", "mutated-valid", "mutated-bool"}:
        fresh = GitStorePrefixAnchorClaims.model_validate(data)
        if attack == "extra-dict":
            fresh.__dict__["unexpected"] = "extra"
        elif attack == "extra-state":
            object.__setattr__(fresh, "__pydantic_extra__", {"unexpected": "extra"})
        else:
            object.__setattr__(fresh, "revision", attack == "mutated-bool" or 1)
        bad = fresh
    else:
        bad = {"dict": data, "fake": SimpleNamespace(**data), "none": None}[attack]
    with unproven():
        if operation == "issue":
            await issue(binding, bad)
        else:
            verify(owner, seal, bad)


def test_snapshot_is_fresh_validated_frozen_and_has_no_private_serialized_fields() -> None:
    original = claims()
    frozen = snapshot_git_store_prefix_claims(original)
    assert frozen is not original and frozen.model_dump() == original.model_dump()
    assert set(frozen.model_dump()) == {"schema_version", "store_genesis_epoch", "revision"}
    with pytest.raises(ValidationError):
        frozen.revision = 1
    with unproven():
        snapshot_git_store_prefix_claims(frozen.model_copy())


@pytest.mark.parametrize("field", ["store_genesis_epoch", "revision"])
@pytest.mark.parametrize("change_seal", [False, True])
async def test_every_variable_claim_field_is_mac_bound(
    binding: Binding, field: str, change_seal: bool
) -> None:
    owner, _ = binding
    original = claims()
    changed = GitStorePrefixAnchorClaims.model_validate(
        original.model_dump() | {field: uuid4() if field == "store_genesis_epoch" else 1}
    )
    seal = await issue(binding, changed if change_seal else original)
    with unproven():
        verify(owner, seal, original if change_seal else changed)


@pytest.mark.parametrize("wrong", ["store", "key-id", "key"])
async def test_store_and_original_key_identity_and_key_material_are_bound(
    binding: Binding, wrong: str
) -> None:
    owner, guard = binding
    value = claims()
    seal = await issue(binding, value)
    with bound(
        guard,
        uuid4() if wrong == "store" else owner._store_id,
        uuid4() if wrong == "key-id" else owner._key_id,
        bytes(reversed(KEY)) if wrong == "key" else KEY,
    ) as other:
        with unproven():
            verify(other, seal, value)


@pytest.mark.parametrize(
    "domain",
    [
        codec._DOMAIN,
        codec._ARTIFACT_DOMAIN,
        codec._GIT_DOMAIN,
        b"harnessix.session-event-seal/v1\0",
    ],
    ids=["session", "artifact", "original-git", "event"],
)
async def test_real_mac_in_every_old_domain_is_not_a_prefix_anchor(
    binding: Binding, domain: bytes
) -> None:
    owner, _ = binding
    value = claims()
    parsed = GitStorePrefixAnchorSeal.model_validate_json(await issue(binding, value))
    with unproven():
        verify(owner, codec._signed(parsed, owner._key, domain), value)


@pytest.mark.parametrize(
    "field",
    [
        "version",
        "purpose",
        "store_id",
        "key_id",
        "tag",
        "scope_sha256",
        "body_sha256",
        "body_bytes",
        "claims",
    ],
)
async def test_each_seal_field_tamper_rejects_before_body_hash(
    binding: Binding, field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _ = binding
    value = claims()
    parsed = json.loads(await issue(binding, value))
    replacements = {
        "version": 2,
        "purpose": "git_delivery_record",
        "store_id": str(uuid4()),
        "key_id": str(uuid4()),
        "tag": "f" * 64,
        "scope_sha256": "f" * 64,
        "body_sha256": "f" * 64,
        "body_bytes": len(BODY) + 1,
        "claims": value.model_dump(mode="json") | {"schema_version": "1"},
    }
    parsed[field] = replacements[field]
    monkeypatch.setattr(codec, "_git_body_digest", lambda *_: pytest.fail("坏MAC不能观察正文"))
    with unproven():
        verify(owner, json.dumps(parsed, separators=(",", ":")).encode(), value, object())


@pytest.mark.parametrize("field", ["body_sha256", "body_bytes"])
async def test_valid_mac_still_binds_complete_body_sha_and_length(
    binding: Binding, field: str
) -> None:
    owner, _ = binding
    value = claims()
    parsed = GitStorePrefixAnchorSeal.model_validate_json(await issue(binding, value))
    changed = parsed.model_copy(
        update={field: "f" * 64 if field == "body_sha256" else len(BODY) + 1}
    )
    with unproven():
        verify(owner, codec._signed(changed, owner._key, DOMAIN), value)


@pytest.mark.parametrize(
    "encoding",
    [
        "whitespace",
        "reordered",
        "root-duplicate",
        "claim-duplicate",
        "escaped-key",
        "upper-uuid",
        "suffix",
        "missing-version",
        "missing-purpose",
    ],
)
async def test_new_seal_rejects_duplicate_and_noncanonical_encoding(
    binding: Binding, encoding: str
) -> None:
    owner, _ = binding
    value = claims(store_genesis_epoch=UUID("12345678-1234-4abc-8abc-123456789abc"))
    seal = await issue(binding, value)
    parsed = json.loads(seal)
    bad = {
        "whitespace": json.dumps(parsed, indent=2).encode(),
        "reordered": json.dumps(
            dict(reversed(list(parsed.items()))), separators=(",", ":")
        ).encode(),
        "root-duplicate": seal.replace(b'{"version":1,', b'{"version":1,"version":1,', 1),
        "claim-duplicate": seal.replace(
            b'"schema_version":"2",', b'"schema_version":"2","schema_version":"2",', 1
        ),
        "escaped-key": seal.replace(b'"purpose"', b'"\\u0070urpose"', 1),
        "upper-uuid": seal.replace(
            str(value.store_genesis_epoch).encode(), str(value.store_genesis_epoch).upper().encode()
        ),
        "suffix": seal + b"\n",
        "missing-version": json.dumps(
            {key: item for key, item in parsed.items() if key != "version"},
            separators=(",", ":"),
        ).encode(),
        "missing-purpose": json.dumps(
            {key: item for key, item in parsed.items() if key != "purpose"},
            separators=(",", ":"),
        ).encode(),
    }[encoding]
    assert bad != seal
    with unproven():
        verify(owner, bad, value)


@pytest.mark.parametrize(
    "attack",
    ["empty", "oversized", "invalid-json", "invalid-utf8", "bytearray", "subclass", "str", "model"],
)
async def test_seal_requires_original_bounded_bytes(binding: Binding, attack: str) -> None:
    owner, _ = binding
    value = claims()
    seal = await issue(binding, value)
    bad = {
        "empty": b"",
        "oversized": b" " * 4097,
        "invalid-json": b"{}",
        "invalid-utf8": b"\xff",
        "bytearray": bytearray(seal),
        "subclass": BytesSubclass(seal),
        "str": seal.decode(),
        "model": GitStorePrefixAnchorSeal.model_validate_json(seal),
    }[attack]
    with unproven():
        verify(owner, bad, value)


@pytest.mark.parametrize("operation", ["issue", "verify"])
@pytest.mark.parametrize(
    "attack", ["empty", "oversized", "bytearray", "memoryview", "subclass", "str", "none"]
)
async def test_body_exact_type_and_unmodified_original_size_limit(
    binding: Binding, operation: str, attack: str
) -> None:
    owner, _ = binding
    value = claims()
    seal = await issue(binding, value)
    bad = {
        "empty": b"",
        "oversized": b"x" * (codec.MAX_HISTORY_BYTES + 1),
        "bytearray": bytearray(BODY),
        "memoryview": memoryview(BODY),
        "subclass": BytesSubclass(BODY),
        "str": BODY.decode(),
        "none": None,
    }[attack]
    with unproven():
        if operation == "issue":
            await issue(binding, value, bad)
        else:
            verify(owner, seal, value, bad)


async def test_complete_binary_64_mib_and_each_64_kib_checkpoint(
    binding: Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _ = binding
    body = b"\0\xff" * (codec.MAX_HISTORY_BYTES // 2)
    assert codec.MAX_HISTORY_BYTES == 64 * 1024 * 1024
    value, token = claims(), CancelToken()
    checks: list[None] = []
    monkeypatch.setattr(token, "checkpoint", lambda: checks.append(None))
    seal = await issue(binding, value, body, token)
    parsed = GitStorePrefixAnchorSeal.model_validate_json(seal)
    assert parsed.body_sha256 == hashlib.sha256(body).hexdigest() and parsed.body_bytes == len(body)
    assert len(checks) >= len(body) // (64 * 1024) + 2
    checks.clear()
    verify(owner, seal, value, body, lambda: checks.append(None))
    assert len(checks) == len(body) // (64 * 1024) + 2
    changed = body[:-1] + b"x"
    with unproven():
        verify(owner, seal, value, changed)


async def test_private_body_is_only_hashed_not_parsed_or_exported_to_protection(
    binding: Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, guard = binding
    original = guard.assert_public_json
    observed: list[Any] = []

    def observing(value: Any, *, checkpoint: Callable[[], None]) -> None:
        observed.append(value)
        original(value, checkpoint=checkpoint)

    monkeypatch.setattr(guard, "assert_public_json", observing)
    monkeypatch.setattr(
        guard, "assert_public_jsonl", lambda *_args, **_kw: pytest.fail("私有catalog不能出口")
    )
    body = MATERIAL.encode() + b"\0\xffnot-json"
    value = claims()
    seal = await issue(binding, value, body)
    verify(owner, seal, value, body)
    assert observed and all(entry["purpose"] == "git_store_prefix_anchor" for entry in observed)
    assert all(MATERIAL not in json.dumps(entry) for entry in observed)


async def test_verifier_has_no_issue_and_never_observes_current_scope(
    binding: Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, guard = binding
    value = claims()
    seal = await issue(binding, value)
    assert not hasattr(owner.git_prefix_verifier, "issue")
    monkeypatch.setattr(
        type(owner.git_prefix), "issue", lambda *_args, **_kw: pytest.fail("只验真端口不能签发")
    )
    for name in ("publication_context", "assert_public_json", "assert_public_jsonl"):
        monkeypatch.setattr(
            guard, name, lambda *_args, **_kw: pytest.fail("历史验真不能检查当前Scope")
        )
    verify(owner, seal, value)


async def test_historical_proof_verifies_under_new_and_verification_only_scope(
    binding: Binding,
) -> None:
    owner, _ = binding
    value = claims()
    seal = await issue(binding, value)
    with (
        scope("new-current-material", "10") as current,
        bound(current, owner._store_id, owner._key_id) as other,
    ):
        current.close()
        verify(other, seal, value)
    readonly_scope = _VerificationOnlyScope()
    with bound(readonly_scope, owner._store_id, owner._key_id) as reader:
        verify(reader, seal, value)
        with pytest.raises(KernelError) as error:
            await reader.git_prefix.issue(value, BODY, readonly_scope, cancel=CancelToken())
        assert error.value.code == "public_output_protection_failed"


@pytest.mark.parametrize(
    "change",
    ["same-context-other-object", "context", "closed", "close-during-protect", "replace-original"],
)
async def test_issue_retains_original_scope_identity_and_context_through_protection(
    binding: Binding, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, guard = binding
    if change in {"same-context-other-object", "replace-original"}:
        with scope() as other:
            original_context = guard.publication_context()
            monkeypatch.setattr(other, "publication_context", lambda: original_context)
            if change == "replace-original":
                monkeypatch.setattr(owner._events, "_protection", other)
            with pytest.raises(KernelError) as error:
                await owner.git_prefix.issue(
                    claims(),
                    BODY,
                    other if change == "same-context-other-object" else guard,
                    cancel=CancelToken(),
                )
        assert error.value.code == "publication_scope_changed"
        return
    if change == "context":
        context = guard.publication_context() | {"scope_id": str(uuid4())}
        monkeypatch.setattr(guard, "publication_context", lambda: context)
    elif change == "closed":
        guard.close()
    else:
        original = guard.assert_public_json

        def closing(value: Any, *, checkpoint: Callable[[], None]) -> None:
            original(value, checkpoint=checkpoint)
            guard.close()

        monkeypatch.setattr(guard, "assert_public_json", closing)
    with pytest.raises(KernelError) as error:
        await issue(binding, claims())
    assert error.value.code == (
        "publication_scope_changed" if change == "context" else "publication_scope_unavailable"
    )


@pytest.mark.parametrize("close_at", ["scope", "checkpoint"])
async def test_final_scope_or_cancel_callback_cannot_publish_after_key_close(
    binding: Binding, close_at: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, guard = binding
    token = CancelToken()
    original_scope, original_check = guard.publication_context, token.checkpoint
    calls, armed = 0, False

    def observed_scope() -> dict[str, object]:
        nonlocal calls, armed
        result = original_scope()
        calls += 1
        if calls == 2:
            armed = True
            if close_at == "scope":
                owner.close()
        return result

    def closing_checkpoint() -> None:
        if armed and close_at == "checkpoint":
            owner.close()
        original_check()

    monkeypatch.setattr(guard, "publication_context", observed_scope)
    monkeypatch.setattr(token, "checkpoint", closing_checkpoint)
    with pytest.raises(KernelError) as error:
        await issue(binding, claims(), cancel=token)
    assert error.value.code == "publication_key_unavailable" and calls == 2


async def test_final_cancel_preserves_original_turn_cancelled_identity(
    binding: Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, guard = binding
    token, failure = CancelToken(), TurnCancelled()
    original_scope = guard.publication_context
    calls = 0

    def observed_scope() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return original_scope()

    def checkpoint() -> None:
        if calls == 2:
            raise failure

    monkeypatch.setattr(guard, "publication_context", observed_scope)
    monkeypatch.setattr(token, "checkpoint", checkpoint)
    with pytest.raises(TurnCancelled) as error:
        await issue(binding, claims(), cancel=token)
    assert error.value is failure


async def test_final_checkpoint_closing_real_scope_must_not_return_seal(
    binding: Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """最终检查点关闭真实Scope且Binding仍开放时，不得交付经保护的候选。"""
    owner, guard = binding
    original = guard.publication_context
    reads = 0

    def observing_context() -> dict[str, object]:
        nonlocal reads
        reads += 1
        return original()

    class ClosingToken(CancelToken):
        def checkpoint(self) -> None:
            super().checkpoint()
            if reads >= 2:
                guard.close()

    monkeypatch.setattr(guard, "publication_context", observing_context)
    with pytest.raises(KernelError) as error:
        await issue(binding, claims(), cancel=ClosingToken())
    assert error.value.code == "publication_scope_unavailable"
    assert error.value.__cause__ is None
    assert not owner._closed


@pytest.mark.parametrize("exception", [TurnCancelled, TimeoutError, asyncio.CancelledError])
async def test_final_checkpoint_scope_close_does_not_mask_original_exception(
    binding: Binding, monkeypatch: pytest.MonkeyPatch, exception: type[BaseException]
) -> None:
    """最终回调已抛取消/期限异常时原对象优先传播，不改写为Scope关闭错误。"""
    _, guard = binding
    original, failure = guard.publication_context, exception()
    reads = 0

    def observing_context() -> dict[str, object]:
        nonlocal reads
        reads += 1
        return original()

    class RejectingToken(CancelToken):
        def checkpoint(self) -> None:
            super().checkpoint()
            if reads >= 2:
                guard.close()
                raise failure

    monkeypatch.setattr(guard, "publication_context", observing_context)
    with pytest.raises(exception) as error:
        await issue(binding, claims(), cancel=RejectingToken())
    assert error.value is failure
    assert reads == 2


async def test_final_scope_recheck_keeps_last_binding_open_check(
    binding: Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """最终实际Scope读取回调关闭Key后，仍由末尾Binding检查阻止交付。"""
    owner, guard = binding
    original = guard.publication_context
    reads = 0

    def closing_context() -> dict[str, object]:
        nonlocal reads
        reads += 1
        result = original()
        if reads == 3:
            owner.close()
        return result

    monkeypatch.setattr(guard, "publication_context", closing_context)
    with pytest.raises(KernelError) as error:
        await issue(binding, claims())
    assert error.value.code == "publication_key_unavailable"
    assert reads == 3 and owner._closed


async def test_final_checkpoint_is_followed_by_actual_scope_recheck(
    binding: Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功路径从真实Scope读取三次；最后取消检查之后仍有实际读取。"""
    owner, guard = binding
    original = guard.publication_context
    observations: list[str] = []

    def observing_context() -> dict[str, object]:
        result = original()
        observations.append("scope")
        return result

    class ObservingToken(CancelToken):
        def checkpoint(self) -> None:
            super().checkpoint()
            if observations.count("scope") == 2:
                observations.append("final-checkpoint")

    monkeypatch.setattr(guard, "publication_context", observing_context)
    value = claims()
    seal = await issue(binding, value, cancel=ObservingToken())
    assert observations == ["scope", "scope", "final-checkpoint", "scope"]
    assert not owner._closed
    verify(owner, seal, value)


@pytest.mark.parametrize("close", [False, True])
async def test_async_protection_freezes_claims_but_cannot_return_after_binding_close(
    binding: Binding, close: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _ = binding
    original = protect_json
    reached, release = asyncio.Event(), asyncio.Event()

    async def paused(protection: Any, value: Any, cancel: CancelToken) -> None:
        reached.set()
        await release.wait()
        await original(protection, value, cancel)

    monkeypatch.setattr(prefix, "protect_json", paused)
    value = claims()
    before = value.model_dump()
    task = asyncio.create_task(issue(binding, value))
    await asyncio.wait_for(reached.wait(), 2)
    object.__setattr__(value, "revision", 1)
    if close:
        owner.close()
    release.set()
    if close:
        with pytest.raises(KernelError) as error:
            await task
        assert error.value.code == "publication_key_unavailable"
    else:
        seal = await task
        assert GitStorePrefixAnchorSeal.model_validate_json(seal).claims.model_dump() == before


@pytest.mark.parametrize("point", [1, 3, 5])
@pytest.mark.parametrize(
    "exception", [TurnCancelled, TimeoutError, asyncio.CancelledError, KernelError]
)
@pytest.mark.parametrize("operation", ["issue", "verify"])
async def test_hash_checkpoint_preserves_cancellation_deadline_and_exception_identity(
    binding: Binding,
    point: int,
    exception: type[BaseException],
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, _ = binding
    body = b"x" * (2 * 64 * 1024 + 1)
    value = claims()
    seal = await issue(binding, value, body)
    failure = (
        KernelError("deadline-fixture", "检查点期限") if exception is KernelError else exception()
    )
    count = 0

    def checkpoint() -> None:
        nonlocal count
        count += 1
        if count == point:
            raise failure

    with pytest.raises(exception) as error:
        if operation == "verify":
            verify(owner, seal, value, body, checkpoint)
        else:
            token = CancelToken()
            monkeypatch.setattr(token, "checkpoint", checkpoint)
            await issue(binding, value, body, token)
    assert error.value is failure


async def test_close_revokes_both_ports_and_zeroes_only_original_binding_key(
    binding: Binding,
) -> None:
    owner, _ = binding
    value = claims()
    seal = await issue(binding, value)
    owner.close()
    assert bytes(owner._key) == b"\0" * 32 and KEY == bytes(range(32))
    for method in (owner.git_prefix_verifier.identity, lambda: verify(owner, seal, value)):
        with pytest.raises(KernelError) as error:
            method()
        assert error.value.code == "publication_key_unavailable"
    with pytest.raises(KernelError) as error:
        await issue(binding, value)
    assert error.value.code == "publication_key_unavailable"


async def test_original_five_kinds_and_historical_seal_encoding_are_unchanged(
    binding: Binding,
) -> None:
    owner, guard = binding
    original = cast(Callable[..., Any], original_git_claims)()
    seal = await owner.git.issue(original, BODY, guard, cancel=CancelToken())
    old_encoding = json.dumps(json.loads(seal), indent=2).encode()
    owner.git_verifier.verify(old_encoding, original, BODY, checkpoint=lambda: None)
    with unproven():
        verify(owner, seal, claims())


async def test_only_low_sensitive_seal_secret_hit_uses_original_protection_error(
    binding: Binding,
) -> None:
    owner, _ = binding
    value = claims()
    with (
        scope(str(value.store_genesis_epoch)) as guard,
        bound(guard, owner._store_id, owner._key_id) as other,
    ):
        with pytest.raises(KernelError) as error:
            await other.git_prefix.issue(value, BODY, guard, cancel=CancelToken())
        assert error.value.code == "public_output_secret_leak"
        assert str(value.store_genesis_epoch) not in str(error.value)


async def test_verifier_checkpoint_closing_binding_cannot_publish_success(binding: Binding) -> None:
    owner, _ = binding
    value = claims()
    seal = await issue(binding, value)
    with pytest.raises(KernelError) as error:
        verify(owner, seal, value, checkpoint=owner.close)
    assert error.value.code == "publication_key_unavailable"


async def test_precancel_and_real_async_parent_task_cancel_return_no_candidate(
    binding: Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = CancelToken()
    token.cancel()
    with pytest.raises(TurnCancelled):
        await issue(binding, claims(), cancel=token)
    reached = asyncio.Event()

    async def waiting(_seconds: float) -> None:
        reached.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "sleep", waiting)
    task = asyncio.create_task(issue(binding, claims()))
    try:
        await asyncio.wait_for(reached.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
