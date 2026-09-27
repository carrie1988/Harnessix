"""同一版本快照、独立副本、清零和有界原生扫描的完整合同测试。"""

from __future__ import annotations

import base64
from urllib.parse import quote

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.secrets import publication
from harnessix.secrets.provider import SecretMaterial
from harnessix.secrets.publication import SecretPublicationScope
from tests.trusted_actions.test_secret_publication import BINDING, CANARY, source


@pytest.mark.parametrize(
    "value",
    [
        CANARY,
        base64.b64encode(CANARY.encode()).decode(),
        quote(CANARY, safe=""),
        CANARY.encode().hex(),
        CANARY.encode().hex().upper(),
        {CANARY: "not public"},
        {"nested": [{"value": CANARY}]},
    ],
)
def test_native_keys_values_and_finite_encodings_are_rejected(value):
    with SecretPublicationScope((BINDING,), source()) as scope:
        with pytest.raises(KernelError, match="Action输出包含受保护Secret") as caught:
            scope.assert_safe(value, (BINDING,), checkpoint=lambda: None)
        assert caught.value.code == "trusted_action_secret_leak"
        assert CANARY not in str(caught.value)


def test_one_snapshot_ignores_later_environment_rotation_and_material_changes():
    environment = {"VALUE": CANARY}
    from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource

    provider = EnvironmentSecretProvider(
        (EnvironmentSecretSource("registry", "7", "VALUE"),), environment=environment
    )
    with SecretPublicationScope((BINDING, BINDING), provider) as scope:
        environment["VALUE"] = "rotated-into-different-same-version-value"
        first = scope.resolve("registry")
        assert bytes(first.value) == CANARY.encode()
        first.clear()
        second = scope.resolve("registry")
        assert bytes(second.value) == CANARY.encode()
        second.clear()
        safe = {"nested": [None, True, 7, {"value": "completed"}]}
        scope.assert_safe(safe, (BINDING,), checkpoint=lambda: None)
        assert safe == {"nested": [None, True, 7, {"value": "completed"}]}


@pytest.mark.parametrize("version", ["8", "missing"])
def test_missing_or_changed_original_version_is_not_authorized(version):
    with SecretPublicationScope((BINDING,), source()) as scope:
        binding = BINDING.model_copy(update={"version": version})
        with pytest.raises(KernelError) as caught:
            scope.assert_safe({"summary": "safe"}, (binding,), checkpoint=lambda: None)
        assert caught.value.code == "trusted_action_secret_unavailable"


def test_capture_wrong_material_and_callback_error_never_expose_original_text():
    class BadProvider:
        def resolve(self, name):
            raise RuntimeError(CANARY)

    with pytest.raises(KernelError) as caught:
        SecretPublicationScope((BINDING,), BadProvider())
    assert CANARY not in str(caught.value) and caught.value.__cause__ is None
    material = SecretMaterial("registry", "8", bytearray(CANARY.encode()))

    class WrongProvider:
        def resolve(self, name):
            return material

    with pytest.raises(KernelError):
        SecretPublicationScope((BINDING,), WrongProvider())
    assert material.value == bytearray(len(CANARY))


@pytest.mark.parametrize("value", [b"tiny"[:3], b"", b"bad\0value", b"x" * 65537])
def test_capture_unsafe_material_fails_closed_and_clears_original(value):
    raw = SecretMaterial("registry", "7", bytearray(value))

    class Provider:
        def resolve(self, _):
            return raw

    with pytest.raises(KernelError):
        SecretPublicationScope((BINDING,), Provider())
    assert (not any(raw.value)) if len(value) <= 65536 else bytes(raw.value) == value


def test_close_is_idempotent_clears_internal_material_and_blocks_every_entry():
    scope = SecretPublicationScope((BINDING,), source())
    retained = scope._materials["registry"].value
    assert CANARY not in repr(scope)
    scope.close()
    scope.close()
    assert not any(retained) and not scope._patterns
    for operation in [
        lambda: scope.resolve("registry"),
        lambda: scope.require(()),
        lambda: scope.assert_safe({}, (), checkpoint=lambda: None),
    ]:
        with pytest.raises(KernelError) as caught:
            operation()
        assert caught.value.code == "trusted_action_secret_unavailable"


@pytest.mark.parametrize("kind", ["cycle", "nodes", "bytes", "work", "hook"])
def test_native_structure_and_shared_scan_budget_are_finite(tmp_path, monkeypatch, kind):
    value = {"value": "completed"}
    if kind == "cycle":
        value["value"] = value
    elif kind == "nodes":
        monkeypatch.setattr(publication, "MAX_SCAN_NODES", 2)
    elif kind == "bytes":
        monkeypatch.setattr(publication, "MAX_SCAN_BYTES", 2)
    elif kind == "work":
        monkeypatch.setattr(publication, "MAX_SCAN_WORK", 2)
    else:

        class Hook(dict):
            def items(self):
                raise AssertionError("不能执行扩展钩子")

        value = Hook()
    with SecretPublicationScope((BINDING,), source()) as scope:
        with pytest.raises(KernelError):
            scope.assert_safe(value, (BINDING,), checkpoint=lambda: None)


@pytest.mark.parametrize("error", [TurnCancelled, TimeoutError])
def test_synchronous_checkpoint_preserves_cancellation_and_timeout(error):
    with SecretPublicationScope((BINDING,), source()) as scope:

        def checkpoint():
            raise error()

        with pytest.raises(error):
            scope.assert_safe({"summary": "completed"}, (BINDING,), checkpoint=checkpoint)


def test_capture_limits_conflicting_versions_and_original_clear(monkeypatch):
    material = SecretMaterial("registry", "7", bytearray(CANARY.encode()))

    class Provider:
        def resolve(self, _):
            return material

    with SecretPublicationScope((BINDING,), Provider()) as scope:
        assert not any(material.value)
        assert bytes(scope.resolve("registry").value) == CANARY.encode()
    with pytest.raises(KernelError):
        SecretPublicationScope((BINDING, BINDING.model_copy(update={"version": "8"})), source())
    monkeypatch.setattr(publication, "MAX_PATTERN_BYTES", 2)
    with pytest.raises(KernelError):
        SecretPublicationScope((BINDING,), source())


@pytest.mark.parametrize(
    "text,value",
    [("1234", 1234), ("true", True), ("null", None), ("false", False), ("12.34", 12.34)],
)
def test_native_scalar_serialization_cannot_bypass_registered_secret(text, value):
    material = SecretMaterial("registry", "7", bytearray(text.encode()))

    class Provider:
        def resolve(self, _):
            return material

    with SecretPublicationScope((BINDING,), Provider()) as scope:
        with pytest.raises(KernelError) as caught:
            scope.assert_safe({"value": value}, (BINDING,), checkpoint=lambda: None)
        assert caught.value.code == "trusted_action_secret_leak"


def test_complete_native_json_credential_cannot_bypass_string_only_checks():
    material = SecretMaterial("registry", "7", bytearray(b'{"token":"structured-original-value"}'))

    class Provider:
        def resolve(self, _):
            return material

    with SecretPublicationScope((BINDING,), Provider()) as scope:
        with pytest.raises(KernelError) as caught:
            scope.assert_safe(
                {"credential": {"token": "structured-original-value"}},
                (BINDING,),
                checkpoint=lambda: None,
            )
        assert caught.value.code == "trusted_action_secret_leak"
