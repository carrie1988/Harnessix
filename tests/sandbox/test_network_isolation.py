from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from harnessix.agent.errors import KernelError
from harnessix.sandbox.contracts import ManagedEgressBinding, NetworkDestination, NetworkPolicy
from harnessix.sandbox.network import resolve_network_policy
from harnessix.sandbox.network_isolation import (
    GATEWAY_LABEL,
    POLICY_LABEL,
    attest_internal_network,
)


def _snapshot():
    policy = NetworkPolicy(
        mode="limited",
        destinations=(
            NetworkDestination(
                kind="domain",
                value="packages.example.com",
                protocol="https",
                ports=(443,),
            ),
        ),
    )
    return resolve_network_policy(
        policy,
        resolver=lambda _: ("93.184.216.34",),
        now=datetime(2026, 9, 8, tzinfo=UTC),
    )


def test_internal_network_attestation_binds_labels_driver_and_only_gateway() -> None:
    snapshot = _snapshot()
    gateway = "a" * 64
    document = [
        {
            "Name": "harnessix-internal-123456789abc",
            "Internal": True,
            "Driver": "bridge",
            "Labels": {POLICY_LABEL: snapshot.digest, GATEWAY_LABEL: gateway},
            "Containers": {"id": {"Name": "harnessix-egress"}},
        }
    ]
    binding = attest_internal_network(
        json.dumps(document).encode(),
        network_name="harnessix-internal-123456789abc",
        proxy_port=8080,
        policy=snapshot,
        gateway_digest=gateway,
    )
    assert binding.policy_digest == snapshot.digest
    assert binding.gateway_digest == gateway


def test_managed_egress_binding_rejects_invalid_proxy_port() -> None:
    with pytest.raises(ValueError):
        ManagedEgressBinding(
            network_name="harnessix-internal-123456789abc",
            proxy_url="http://harnessix-egress:65536",
            policy_digest="a" * 64,
            gateway_digest="b" * 64,
            internal_network_attestation="c" * 64,
        )


@pytest.mark.parametrize("change", ["external", "label", "extra", "duplicate"])
def test_internal_network_attestation_fails_closed(change: str) -> None:
    snapshot = _snapshot()
    gateway = "a" * 64
    document = {
        "Name": "harnessix-internal-123456789abc",
        "Internal": change != "external",
        "Driver": "bridge",
        "Labels": {
            POLICY_LABEL: "b" * 64 if change == "label" else snapshot.digest,
            GATEWAY_LABEL: gateway,
        },
        "Containers": {
            "id": {"Name": "harnessix-egress"},
            **({"other": {"Name": "untrusted"}} if change == "extra" else {}),
        },
    }
    if change == "duplicate":
        payload = b'[{"Name":"one","Name":"two"}]'
    else:
        payload = json.dumps([document]).encode()
    with pytest.raises(KernelError) as error:
        attest_internal_network(
            payload,
            network_name="harnessix-internal-123456789abc",
            proxy_port=8080,
            policy=snapshot,
            gateway_digest=gateway,
        )
    assert error.value.code == "network_policy_unenforceable"
