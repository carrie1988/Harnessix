from __future__ import annotations

import base64
import json
from urllib.parse import quote_from_bytes

import pytest

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import SecretVersionBinding
from harnessix.secrets.guard import SecretLeakGuard
from harnessix.secrets.provider import (
    EnvironmentSecretProvider,
    EnvironmentSecretSource,
    resolve_secret_environment,
)
from harnessix.secrets.redaction import REDACTION_MARKER, StreamingSecretRedactor, redact_bytes


def test_versioned_secret_scope_injects_only_declared_target_and_closes() -> None:
    provider = EnvironmentSecretProvider(
        (EnvironmentSecretSource("registry", "7", "HOST_REGISTRY_TOKEN"),),
        environment={"HOST_REGISTRY_TOKEN": "s3cret-value"},
    )
    binding = SecretVersionBinding(name="registry", version="7", target="TOKEN")
    resolved = resolve_secret_environment((binding,), provider, platform="posix")
    assert resolved.as_text() == {"TOKEN": "s3cret-value"}
    assert resolved.bindings() == (("TOKEN", "registry", "7"),)
    assert "s3cret-value" not in repr(resolved)
    resolved.close()
    with pytest.raises(KernelError) as closed:
        resolved.as_text()
    assert closed.value.code == "secret_scope_closed"


def test_secret_version_drift_and_windows_target_collision_fail_closed() -> None:
    provider = EnvironmentSecretProvider(
        (
            EnvironmentSecretSource("first", "2", "FIRST"),
            EnvironmentSecretSource("second", "1", "SECOND"),
        ),
        environment={"FIRST": "first-value", "SECOND": "second-value"},
    )
    with pytest.raises(KernelError) as version:
        resolve_secret_environment(
            (SecretVersionBinding(name="first", version="1", target="TOKEN"),),
            provider,
            platform="posix",
        )
    assert version.value.code == "secret_version_changed"

    with pytest.raises(KernelError) as collision:
        resolve_secret_environment(
            (
                SecretVersionBinding(name="first", version="2", target="Token"),
                SecretVersionBinding(name="second", version="1", target="TOKEN"),
            ),
            provider,
            platform="windows",
        )
    assert collision.value.code == "secret_target_conflict"


def test_streaming_redactor_covers_every_chunk_boundary_and_common_encodings() -> None:
    secret = b"s3cr et/+value"
    encoded = base64.b64encode(secret)
    quoted = quote_from_bytes(secret, safe="").encode("ascii")
    json_encoded = json.dumps(secret.decode("utf-8"))[1:-1].encode("utf-8")
    body = (
        b"raw="
        + secret
        + b" base64="
        + encoded
        + b" url="
        + quoted
        + b" json="
        + json_encoded
        + b" end"
    )
    for boundary in range(len(body) + 1):
        redactor = StreamingSecretRedactor((secret,))
        result = redactor.feed(body[:boundary]) + redactor.feed(body[boundary:]) + redactor.finish()
        assert secret not in result
        assert encoded not in result
        assert quoted not in result
        assert json_encoded not in result
        assert result.count(REDACTION_MARKER) == 4


def test_redactor_fails_closed_for_too_short_secret_and_after_finish() -> None:
    with pytest.raises(KernelError) as short:
        redact_bytes(b"abc", (b"abc",))
    assert short.value.code == "secret_redaction_unsafe"

    redactor = StreamingSecretRedactor((b"long-secret",))
    redactor.finish()
    with pytest.raises(KernelError) as closed:
        redactor.feed(b"output")
    assert closed.value.code == "secret_redaction_closed"


def test_secret_leak_guard_redacts_values_and_rejects_keys_or_unserializable_data() -> None:
    guard = SecretLeakGuard((b"secret-canary",))
    redacted = guard.redact_json(
        {
            "log": "prefix secret-canary suffix",
            "nested": ["safe", "secret-canary"],
        }
    )
    assert redacted == {
        "log": "prefix [REDACTED] suffix",
        "nested": ["safe", "[REDACTED]"],
    }
    guard.assert_safe(redacted)
    with pytest.raises(KernelError) as key:
        guard.redact_json({"secret-canary": "value"})
    assert key.value.code == "secret_redaction_failed"
    with pytest.raises(KernelError) as unsupported:
        guard.assert_safe(object())
    assert unsupported.value.code == "secret_redaction_failed"
