from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harnessix.agent.errors import KernelError
from harnessix.sandbox.contracts import NetworkDestination, NetworkPolicy
from harnessix.sandbox.network import authorized_addresses, resolve_network_policy

NOW = datetime(2026, 9, 8, tzinfo=UTC)


def _domain_policy(*, allow_private: bool = False) -> NetworkPolicy:
    return NetworkPolicy(
        mode="restricted" if allow_private else "limited",
        destinations=(
            NetworkDestination(
                kind="domain",
                value="packages.example.com",
                protocol="https",
                ports=(443,),
            ),
        ),
        allow_private_addresses=allow_private,
        resolution_ttl_seconds=60,
    )


@pytest.mark.parametrize(
    "value",
    ["EXAMPLE.com", "example.com.", "localhost", "127.0.0.1", "a_b.example.com", "*.example.com"],
)
def test_domain_rules_require_canonical_exact_hostname(value: str) -> None:
    with pytest.raises(ValueError):
        NetworkDestination(kind="domain", value=value, protocol="https", ports=(443,))


def test_resolution_is_pinned_sorted_and_expires() -> None:
    snapshot = resolve_network_policy(
        _domain_policy(),
        resolver=lambda _: ("2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"),
        now=NOW,
    )
    addresses = authorized_addresses(snapshot, "packages.example.com", 443, "https", now=NOW)
    assert addresses == ("2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34")
    with pytest.raises(KernelError) as port:
        authorized_addresses(snapshot, "packages.example.com", 80, "https", now=NOW)
    assert port.value.code == "network_destination_denied"
    with pytest.raises(KernelError) as expired:
        authorized_addresses(
            snapshot,
            "packages.example.com",
            443,
            "https",
            now=NOW + timedelta(seconds=60),
        )
    assert expired.value.code == "network_destination_denied"


def test_private_dns_answer_requires_explicit_restricted_policy() -> None:
    with pytest.raises(KernelError) as denied:
        resolve_network_policy(_domain_policy(), resolver=lambda _: ("127.0.0.1",), now=NOW)
    assert denied.value.code == "network_destination_denied"

    snapshot = resolve_network_policy(
        _domain_policy(allow_private=True), resolver=lambda _: ("127.0.0.1",), now=NOW
    )
    assert authorized_addresses(snapshot, "packages.example.com", 443, "https", now=NOW) == (
        "127.0.0.1",
    )


def test_cidr_authorizes_only_matching_ip_protocol_and_port() -> None:
    policy = NetworkPolicy(
        mode="restricted",
        destinations=(
            NetworkDestination(kind="cidr", value="8.8.8.0/24", protocol="tcp", ports=(22,)),
        ),
    )
    snapshot = resolve_network_policy(policy, now=NOW)
    assert authorized_addresses(snapshot, "8.8.8.8", 22, "tcp", now=NOW) == ("8.8.8.8",)
    with pytest.raises(KernelError):
        authorized_addresses(snapshot, "8.8.4.4", 22, "tcp", now=NOW)


def test_none_policy_denies_gateway_authorization() -> None:
    snapshot = resolve_network_policy(NetworkPolicy(mode="none"), now=NOW)
    with pytest.raises(KernelError) as denied:
        authorized_addresses(snapshot, "8.8.8.8", 443, "https", now=NOW)
    assert denied.value.code == "network_destination_denied"
