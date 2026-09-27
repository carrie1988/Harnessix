"""Event Seal核心认证、原保护与跨实例验证；未连接生产写读路径，不作历史授权验收。"""

from __future__ import annotations

import asyncio
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import AgentEvent, Budget, ThreadCreated, TurnStarted
from harnessix.agent.publication import protect_jsonl
from harnessix.product_config.contracts import SecretReference
from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.session.publication_seal import EventPublicationAuthority

KEY = bytes(range(32))
MATERIAL = "sealed-event-fixture-material/+9"


@contextmanager
def scope(value=MATERIAL, version="9"):
    provider = EnvironmentSecretProvider(
        (EnvironmentSecretSource("model", version, "FIXTURE_KEY"),),
        environment={"FIXTURE_KEY": value},
    )
    with SecretPublicationScope((SecretReference(name="model", version=version),), provider) as s:
        yield s


def event(workspace="safe-workspace"):
    return AgentEvent(
        thread_id=uuid4(),
        sequence=1,
        payload=ThreadCreated(
            workspace=(Path(tempfile.gettempdir()).resolve() / workspace).as_posix()
        ),
    )


def verify(authority, seal, body, item, store, **overrides):
    params = dict(store_id=store, thread_id=item.thread_id, event_id=item.event_id, sequence=1)
    params.update(overrides)
    authority.verify_event(seal.model_dump_json().encode(), body, **params)


async def test_real_scope_signed_event_survives_new_authority_and_new_scope():
    identity, store, item = uuid4(), uuid4(), event()
    with scope() as old:
        a = EventPublicationAuthority(identity, KEY, old)
        body, seal = await a.issue_new_event(store, item, CancelToken())
        assert body == item.model_dump_json().encode()
        assert MATERIAL not in seal.model_dump_json()
        a.close()
    with scope("different-current-material", "10") as current:
        b = EventPublicationAuthority(identity, KEY, current)
        verify(b, seal, body, item, store)
        assert seal.scope_sha256 != b._context_sha256
        b.close()


@pytest.mark.parametrize("field", ["store_id", "thread_id", "event_id", "sequence"])
async def test_authenticated_event_rejects_cross_owner_and_sequence_binding(field):
    with scope() as s:
        a = EventPublicationAuthority(uuid4(), KEY, s)
        store, item = uuid4(), event()
        body, seal = await a.issue_new_event(store, item, CancelToken())
        with pytest.raises(KernelError) as caught:
            verify(a, seal, body, item, store, **{field: 2 if field == "sequence" else uuid4()})
        assert caught.value.code == "publication_history_unproven"
        a.close()


@pytest.mark.parametrize("field", ["scope_sha256", "body_sha256", "tag"])
async def test_authenticated_claims_cannot_be_replaced_with_unkeyed_hashes(field):
    with scope() as s:
        a = EventPublicationAuthority(uuid4(), KEY, s)
        store, item = uuid4(), event()
        body, seal = await a.issue_new_event(store, item, CancelToken())
        with pytest.raises(KernelError) as caught:
            verify(a, seal.model_copy(update={field: "f" * 64}), body, item, store)
        assert caught.value.code == "publication_history_unproven"
        a.close()


@pytest.mark.parametrize("wrong", ["key", "key_id"])
async def test_wrong_persistent_key_or_key_identity_cannot_authenticate(wrong):
    with scope() as s:
        identity, store, item = uuid4(), uuid4(), event()
        a = EventPublicationAuthority(identity, KEY, s)
        body, seal = await a.issue_new_event(store, item, CancelToken())
        b = EventPublicationAuthority(
            uuid4() if wrong == "key_id" else identity,
            bytes(reversed(KEY)) if wrong == "key" else KEY,
            s,
        )
        with pytest.raises(KernelError) as caught:
            verify(b, seal, body, item, store)
        assert caught.value.code == "publication_history_unproven"
        a.close()
        b.close()


async def test_changed_original_bytes_fail_even_with_same_semantic_json():
    with scope() as s:
        a = EventPublicationAuthority(uuid4(), KEY, s)
        store, item = uuid4(), event()
        body, seal = await a.issue_new_event(store, item, CancelToken())
        with pytest.raises(KernelError) as caught:
            verify(a, seal, body + b" ", item, store)
        assert caught.value.code == "publication_history_unproven"
        a.close()


async def test_protection_is_required_before_any_new_seal_can_return():
    with scope() as s:
        a = EventPublicationAuthority(uuid4(), KEY, s)
        with pytest.raises(KernelError) as caught:
            await a.issue_new_event(uuid4(), event(MATERIAL), CancelToken())
        assert caught.value.code == "public_output_secret_leak"
        assert MATERIAL not in str(caught.value)
        a.close()


@pytest.mark.parametrize("bad", [b"", b"{}", b"not-json", b" " * 4097])
async def test_missing_invalid_or_oversized_seal_is_not_legacy_reauthorization(bad):
    with scope() as s:
        a = EventPublicationAuthority(uuid4(), KEY, s)
        store, item = uuid4(), event()
        body, _ = await a.issue_new_event(store, item, CancelToken())
        with pytest.raises(KernelError) as caught:
            a.verify_event(
                bad,
                body,
                store_id=store,
                thread_id=item.thread_id,
                event_id=item.event_id,
                sequence=1,
            )
        assert caught.value.code == "publication_history_unproven"
        a.close()


@pytest.mark.parametrize("change", ["version", "extra", "bool_sequence", "bad_tag", "old_event"])
async def test_strict_receipt_codec_rejects_unsupported_or_constructed_claims(change):
    with scope() as s:
        a = EventPublicationAuthority(uuid4(), KEY, s)
        store, item = uuid4(), event()
        body, seal = await a.issue_new_event(store, item, CancelToken())
        value = seal.model_dump(mode="json")
        key, replacement = {
            "version": ("seal_version", 2),
            "extra": ("unexpected", MATERIAL),
            "bool_sequence": ("sequence", True),
            "bad_tag": ("tag", MATERIAL),
            "old_event": ("event_schema_version", 19),
        }[change]
        value[key] = replacement
        with pytest.raises(KernelError) as caught:
            a.verify_event(
                json.dumps(value).encode(),
                body,
                store_id=store,
                thread_id=item.thread_id,
                event_id=item.event_id,
                sequence=1,
            )
        assert caught.value.code == "publication_history_unproven"
        assert MATERIAL not in str(caught.value)
        a.close()


@pytest.mark.parametrize("point", ["before_issue", "during_check"])
async def test_scope_close_prevents_issue_before_or_after_original_body_check(point, monkeypatch):
    with scope() as s:
        a = EventPublicationAuthority(uuid4(), KEY, s)
        if point == "before_issue":
            s.close()
        else:
            original = s.assert_public_jsonl

            def closing(body, *, checkpoint):
                original(body, checkpoint=checkpoint)
                s.close()

            monkeypatch.setattr(s, "assert_public_jsonl", closing)
        with pytest.raises(KernelError):
            await a.issue_new_event(uuid4(), event(), CancelToken())
        a.close()


async def test_proven_original_event_is_not_permission_to_export_new_registered_material():
    item, store, identity = event("future-current-credential"), uuid4(), uuid4()
    with scope() as old:
        a = EventPublicationAuthority(identity, KEY, old)
        body, seal = await a.issue_new_event(store, item, CancelToken())
        a.close()
    with scope("future-current-credential", "10") as current:
        b = EventPublicationAuthority(identity, KEY, current)
        verify(b, seal, body, item, store)
        with pytest.raises(KernelError) as caught:
            await protect_jsonl(current, body, CancelToken())
        assert caught.value.code == "public_output_secret_leak"
        b.close()


async def test_scope_identity_has_no_material_and_is_distinct_for_equal_name_versions():
    with scope() as first, scope() as second:
        before = first.publication_context()
        assert MATERIAL not in json.dumps(before)
        assert before == first.publication_context()
        assert before["scope_id"] != second.publication_context()["scope_id"]
        before["bindings"].clear()
        assert first.publication_context()["bindings"]
        first.close()
        with pytest.raises(KernelError):
            first.publication_context()


async def test_explicit_cancel_prevents_issue():
    with scope() as s:
        a = EventPublicationAuthority(uuid4(), KEY, s)
        token = CancelToken()
        token.cancel()
        with pytest.raises(TurnCancelled):
            await a.issue_new_event(uuid4(), event(), token)
        a.close()


async def test_parent_cancel_during_real_guard_has_no_receipt(monkeypatch):
    from harnessix.agent import publication

    with scope() as s:
        a = EventPublicationAuthority(uuid4(), KEY, s)
        reached = asyncio.Event()

        async def waiting(seconds):
            reached.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(publication.asyncio, "sleep", waiting)
        task = asyncio.create_task(a.issue_new_event(uuid4(), event(), CancelToken()))
        await asyncio.wait_for(reached.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        a.close()


async def test_owned_mutable_key_is_cleared_without_mutating_caller_copy():
    caller = bytearray(KEY)
    with scope() as s:
        a = EventPublicationAuthority(uuid4(), caller, s)
        held = a._key
        caller[:] = b"\1" * len(caller)
        assert a._key == bytearray(KEY)
        a.close()
        a.close()
        assert not any(held) and any(caller)
        with pytest.raises(KernelError) as caught:
            await a.issue_new_event(uuid4(), event(), CancelToken())
        assert caught.value.code == "publication_key_unavailable"


@pytest.mark.parametrize("fault", ["limit", "timeout", "extension"])
async def test_real_original_guard_fault_does_not_produce_seal(fault, monkeypatch):
    from harnessix.agent import publication
    from harnessix.secrets import publication as secret_publication

    with scope() as s:
        a = EventPublicationAuthority(uuid4(), KEY, s)
        if fault == "limit":
            monkeypatch.setattr(secret_publication, "MAX_SCAN_NODES", 0)
        elif fault == "timeout":
            monkeypatch.setattr(publication, "PUBLIC_PROTECTION_TIMEOUT", 0)
        else:

            def broken(value, *, checkpoint):
                raise ValueError(MATERIAL)

            monkeypatch.setattr(s, "assert_public_json", broken)
        with pytest.raises(KernelError) as caught:
            await a.issue_new_event(uuid4(), event(), CancelToken())
        assert (
            caught.value.code
            == {
                "limit": "public_output_limit",
                "timeout": "public_output_timeout",
                "extension": "public_output_protection_failed",
            }[fault]
        )
        assert MATERIAL not in str(caught.value)
        a.close()


async def test_replaced_scope_identity_cannot_sign_with_original_issuer(monkeypatch):
    with scope() as s:
        a = EventPublicationAuthority(uuid4(), KEY, s)
        original = s.publication_context()
        original["scope_id"] = str(uuid4())
        monkeypatch.setattr(s, "publication_context", lambda: original)
        with pytest.raises(KernelError) as caught:
            await a.issue_new_event(uuid4(), event(), CancelToken())
        assert caught.value.code == "publication_scope_changed"
        a.close()


@pytest.mark.parametrize("size", [0, 16, 31, 33, 4096])
async def test_non_256_bit_key_is_rejected_without_key_diagnostic(size):
    with scope() as s:
        with pytest.raises(KernelError) as caught:
            EventPublicationAuthority(uuid4(), b"x" * size, s)
        assert caught.value.code == "publication_key_unavailable"


@pytest.mark.parametrize("bad", [True, -1, "1"])
async def test_expected_row_sequence_is_not_coerced(bad):
    with scope() as s:
        a = EventPublicationAuthority(uuid4(), KEY, s)
        store, item = uuid4(), event()
        body, seal = await a.issue_new_event(store, item, CancelToken())
        with pytest.raises(KernelError) as caught:
            verify(a, seal, body, item, store, sequence=bad)
        assert caught.value.code == "publication_event_invalid"
        a.close()


async def test_closed_authority_cannot_verify_even_with_authentic_receipt():
    with scope() as s:
        a = EventPublicationAuthority(uuid4(), KEY, s)
        store, item = uuid4(), event()
        body, seal = await a.issue_new_event(store, item, CancelToken())
        a.close()
        with pytest.raises(KernelError) as caught:
            verify(a, seal, body, item, store)
        assert caught.value.code == "publication_key_unavailable"


@pytest.mark.parametrize("seconds", [0.00001, 0.0000001, 86400.0])
async def test_candidate_preserves_existing_pydantic_event_encoding(seconds):
    item = AgentEvent(
        thread_id=uuid4(),
        turn_id=uuid4(),
        sequence=2,
        payload=TurnStarted(
            request_id="codec-check",
            request_fingerprint="a" * 64,
            budget=Budget(timeout_seconds=seconds),
        ),
    )
    with scope() as s:
        a = EventPublicationAuthority(uuid4(), KEY, s)
        body, _ = await a.issue_new_event(uuid4(), item, CancelToken())
        assert body == item.model_dump_json().encode()
        a.close()


async def test_native_guard_cannot_rewrite_the_frozen_candidate(monkeypatch):
    item, store = event(), uuid4()
    original_bytes = item.model_dump_json().encode()
    with scope() as s:
        a = EventPublicationAuthority(uuid4(), KEY, s)
        original = s.assert_public_json

        def mutating(value, *, checkpoint):
            original(value, checkpoint=checkpoint)
            if "payload" in value:
                value["payload"]["workspace"] = event("rewritten").payload.workspace

        monkeypatch.setattr(s, "assert_public_json", mutating)
        body, seal = await a.issue_new_event(store, item, CancelToken())
        assert body == original_bytes
        verify(a, seal, body, item, store)
        a.close()


async def test_caller_mutation_during_guard_does_not_change_original_identity(monkeypatch):
    item, store = event(), uuid4()
    original_bytes = item.model_dump_json().encode()
    original_id = item.event_id
    with scope() as s:
        a = EventPublicationAuthority(uuid4(), KEY, s)
        original = s.assert_public_json

        def mutating(value, *, checkpoint):
            original(value, checkpoint=checkpoint)
            object.__setattr__(item, "event_id", uuid4())
            object.__setattr__(item.payload, "workspace", event("changed-caller").payload.workspace)

        monkeypatch.setattr(s, "assert_public_json", mutating)
        body, seal = await a.issue_new_event(store, item, CancelToken())
        assert body == original_bytes and seal.event_id == original_id != item.event_id
        a.close()
