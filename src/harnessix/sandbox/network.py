"""Sandbox与网络隔离：规范化网络目的地并执行策略匹配。"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.sandbox.contracts import (
    DestinationProtocol,
    NetworkPolicy,
    NetworkPolicySnapshot,
    NetworkResolution,
    canonical_domain,
    network_policy_snapshot_digest,
)

AddressResolver = Callable[[str], Sequence[str]]


def system_resolver(hostname: str) -> tuple[str, ...]:
    try:
        addresses: set[str] = set()
        for _, _, _, _, sockaddr in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM):
            value = sockaddr[0]
            if isinstance(value, str):
                addresses.add(value)
        return tuple(sorted(addresses))
    except OSError:
        raise KernelError("network_resolution_failed", "网络目标解析失败") from None


def _allowed_address(value: str, *, allow_private: bool) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise KernelError("network_resolution_failed", "网络目标返回非法地址") from None
    if not allow_private and not address.is_global:
        raise KernelError("network_destination_denied", "网络目标解析到非公网地址")
    return address.compressed


def resolve_network_policy(
    policy: NetworkPolicy,
    *,
    resolver: AddressResolver = system_resolver,
    now: datetime | None = None,
) -> NetworkPolicySnapshot:
    checked_now = now or datetime.now(UTC)
    if checked_now.tzinfo is None:
        raise KernelError("network_policy_invalid", "网络策略时间必须包含时区")
    resolutions: list[NetworkResolution] = []
    try:
        for destination in policy.destinations:
            if destination.kind != "domain":
                continue
            values = tuple(
                sorted(
                    {
                        _allowed_address(value, allow_private=policy.allow_private_addresses)
                        for value in resolver(destination.value)
                    }
                )
            )
            if not values or len(values) > 16:
                raise KernelError("network_resolution_failed", "网络目标解析结果数量无效")
            resolutions.append(
                NetworkResolution(
                    hostname=destination.value,
                    addresses=values,
                    resolved_at=checked_now,
                    expires_at=checked_now + timedelta(seconds=policy.resolution_ttl_seconds),
                )
            )
        payload = {
            "spec_version": "harnessix.network-policy-snapshot/v1",
            "policy": policy.model_dump(mode="json", warnings="error"),
            "resolutions": [item.model_dump(mode="json") for item in resolutions],
        }
        from harnessix.execution.contracts import canonical_digest

        return NetworkPolicySnapshot(
            policy=policy,
            resolutions=tuple(resolutions),
            digest=canonical_digest(payload),
        )
    except KernelError:
        raise
    except (ValidationError, ValueError, TypeError):
        raise KernelError("network_policy_invalid", "网络策略快照不符合契约") from None


def authorized_addresses(
    snapshot: NetworkPolicySnapshot,
    host: str,
    port: int,
    protocol: DestinationProtocol,
    *,
    now: datetime | None = None,
) -> tuple[str, ...]:
    if snapshot.digest != network_policy_snapshot_digest(snapshot):
        raise KernelError("network_policy_changed", "网络策略快照已经变化")
    if snapshot.policy.mode == "none":
        raise KernelError("network_destination_denied", "当前Sandbox禁止网络")
    if snapshot.policy.mode == "full":
        raise KernelError("network_gateway_not_required", "full模式不由选择性网关授权")
    if type(port) is not int or not 1 <= port <= 65535:
        raise KernelError("network_destination_denied", "网络端口无效")
    checked_now = now or datetime.now(UTC)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            hostname = canonical_domain(host)
        except ValueError:
            raise KernelError("network_destination_denied", "网络目标主机无效") from None
        destination = next(
            (
                item
                for item in snapshot.policy.destinations
                if item.kind == "domain"
                and item.value == hostname
                and item.protocol == protocol
                and port in item.ports
            ),
            None,
        )
        resolution = next(
            (item for item in snapshot.resolutions if item.hostname == hostname), None
        )
        if destination is None or resolution is None or checked_now >= resolution.expires_at:
            raise KernelError("network_destination_denied", "网络目标未授权或解析已过期") from None
        return resolution.addresses
    canonical = address.compressed
    if not snapshot.policy.allow_private_addresses and not address.is_global:
        raise KernelError("network_destination_denied", "网络IP不属于允许的公网范围")
    for destination in snapshot.policy.destinations:
        if (
            destination.kind == "cidr"
            and destination.protocol == protocol
            and port in destination.ports
            and address in ipaddress.ip_network(destination.value)
        ):
            return (canonical,)
    raise KernelError("network_destination_denied", "网络IP或端口未授权")
