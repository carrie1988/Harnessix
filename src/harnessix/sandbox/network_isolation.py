"""Sandbox与网络隔离：探测并构造平台网络隔离命令。"""

from __future__ import annotations

import json
from typing import Any

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import canonical_digest
from harnessix.sandbox.contracts import ManagedEgressBinding, NetworkPolicySnapshot

MAX_NETWORK_INSPECT_BYTES = 64 * 1024
POLICY_LABEL = "com.harnessix.network-policy"
GATEWAY_LABEL = "com.harnessix.egress-gateway"


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def attest_internal_network(
    inspect_output: bytes,
    *,
    network_name: str,
    proxy_port: int,
    policy: NetworkPolicySnapshot,
    gateway_digest: str,
) -> ManagedEgressBinding:
    if (
        type(inspect_output) is not bytes
        or not inspect_output
        or len(inspect_output) > MAX_NETWORK_INSPECT_BYTES
        or type(proxy_port) is not int
        or not 1 <= proxy_port <= 65535
    ):
        raise KernelError("network_attestation_invalid", "Docker网络证明输入无效")
    try:
        decoded = inspect_output.decode("utf-8")
        payload = json.loads(decoded, object_pairs_hook=_object, parse_constant=lambda _: _fail())
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise ValueError
        network = payload[0]
        labels = network.get("Labels")
        containers = network.get("Containers")
        if (
            network.get("Name") != network_name
            or network.get("Internal") is not True
            or network.get("Driver") != "bridge"
            or not isinstance(labels, dict)
            or labels.get(POLICY_LABEL) != policy.digest
            or labels.get(GATEWAY_LABEL) != gateway_digest
            or not isinstance(containers, dict)
        ):
            raise ValueError
        names = []
        for container in containers.values():
            if not isinstance(container, dict) or not isinstance(container.get("Name"), str):
                raise ValueError
            names.append(container["Name"])
        if names != ["harnessix-egress"]:
            raise ValueError
        attestation = canonical_digest(network)
        return ManagedEgressBinding(
            network_name=network_name,
            proxy_url=f"http://harnessix-egress:{proxy_port}",
            policy_digest=policy.digest,
            gateway_digest=gateway_digest,
            internal_network_attestation=attestation,
        )
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise KernelError(
            "network_policy_unenforceable", "Docker内部网络不能通过完整性证明"
        ) from None


def _fail() -> None:
    raise ValueError("non-finite JSON")
