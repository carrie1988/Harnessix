from __future__ import annotations

import asyncio
import ipaddress
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import canonical_digest
from harnessix.sandbox.contracts import (
    DestinationProtocol,
    NetworkPolicySnapshot,
    canonical_domain,
)
from harnessix.sandbox.network import authorized_addresses

MAX_PROXY_HEADER_BYTES = 16 * 1024
MAX_TLS_CLIENT_HELLO_BYTES = 64 * 1024

Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


@dataclass(frozen=True, slots=True)
class EgressGatewayLimits:
    header_timeout_seconds: float = 10.0
    connect_timeout_seconds: float = 10.0
    idle_timeout_seconds: float = 300.0
    max_bytes_each_direction: int = 1024 * 1024 * 1024


async def _connect(address: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(address, port)


def egress_gateway_digest(snapshot: NetworkPolicySnapshot, limits: EgressGatewayLimits) -> str:
    return canonical_digest(
        {
            "implementation": "harnessix-managed-egress/v1",
            "policy_digest": snapshot.digest,
            "limits": {
                "header_timeout_seconds": limits.header_timeout_seconds,
                "connect_timeout_seconds": limits.connect_timeout_seconds,
                "idle_timeout_seconds": limits.idle_timeout_seconds,
                "max_bytes_each_direction": limits.max_bytes_each_direction,
            },
        }
    )


def _authority(value: str) -> tuple[str, int]:
    try:
        parsed = urlsplit("//" + value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise KernelError("network_destination_denied", "CONNECT目标格式无效") from None
    if (
        host is None
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise KernelError("network_destination_denied", "CONNECT目标格式无效")
    return host, port


def parse_tls_server_name(handshake: bytes) -> str:
    """解析完整TLS ClientHello握手正文；不接受ECH或无SNI连接。"""

    try:
        if len(handshake) < 4 or handshake[0] != 1:
            raise ValueError
        body_length = int.from_bytes(handshake[1:4], "big")
        if body_length != len(handshake) - 4 or body_length > MAX_TLS_CLIENT_HELLO_BYTES:
            raise ValueError
        body = memoryview(handshake)[4:]
        cursor = 34  # legacy_version(2) + random(32)
        session_length = body[cursor]
        cursor += 1 + session_length
        cipher_length = int.from_bytes(body[cursor : cursor + 2], "big")
        cursor += 2 + cipher_length
        compression_length = body[cursor]
        cursor += 1 + compression_length
        extension_length = int.from_bytes(body[cursor : cursor + 2], "big")
        cursor += 2
        end = cursor + extension_length
        if end != len(body):
            raise ValueError
        while cursor < end:
            extension_type = int.from_bytes(body[cursor : cursor + 2], "big")
            length = int.from_bytes(body[cursor + 2 : cursor + 4], "big")
            cursor += 4
            value = body[cursor : cursor + length]
            cursor += length
            if extension_type != 0:
                continue
            names_length = int.from_bytes(value[:2], "big")
            if names_length != len(value) - 2:
                raise ValueError
            names = value[2:]
            name_type = names[0]
            name_length = int.from_bytes(names[1:3], "big")
            name = bytes(names[3 : 3 + name_length])
            if name_type != 0 or len(name) != name_length or 3 + name_length != len(names):
                raise ValueError
            decoded = name.decode("ascii")
            if decoded != canonical_domain(decoded):
                raise ValueError
            return decoded
    except (IndexError, UnicodeError, ValueError):
        raise KernelError("network_tls_identity_denied", "TLS ClientHello SNI无效") from None
    raise KernelError("network_tls_identity_denied", "TLS ClientHello缺少SNI")


async def read_tls_client_hello(reader: asyncio.StreamReader) -> tuple[bytes, str]:
    records = bytearray()
    handshake = bytearray()
    expected: int | None = None
    while expected is None or len(handshake) < expected:
        header = await reader.readexactly(5)
        if header[0] != 22:
            raise KernelError("network_tls_identity_denied", "CONNECT流不是TLS握手")
        length = int.from_bytes(header[3:5], "big")
        if length <= 0 or len(records) + 5 + length > MAX_TLS_CLIENT_HELLO_BYTES:
            raise KernelError("network_tls_identity_denied", "TLS ClientHello超过上限")
        body = await reader.readexactly(length)
        records.extend(header)
        records.extend(body)
        handshake.extend(body)
        if expected is None and len(handshake) >= 4:
            expected = 4 + int.from_bytes(handshake[1:4], "big")
            if expected > MAX_TLS_CLIENT_HELLO_BYTES:
                raise KernelError("network_tls_identity_denied", "TLS ClientHello超过上限")
    if expected is None or len(handshake) != expected:
        raise KernelError("network_tls_identity_denied", "TLS ClientHello边界无效")
    value = bytes(records)
    return value, parse_tls_server_name(bytes(handshake))


class ManagedEgressGateway:
    """只允许CONNECT；域名规则同时校验批准DNS快照和TLS SNI。"""

    def __init__(
        self,
        snapshot: NetworkPolicySnapshot,
        *,
        connector: Connector = _connect,
        limits: EgressGatewayLimits | None = None,
    ) -> None:
        selected_limits = limits or EgressGatewayLimits()
        if snapshot.policy.mode not in {"limited", "restricted"}:
            raise KernelError("network_policy_unenforceable", "出口网关只接受选择性网络策略")
        if (
            not math.isfinite(selected_limits.header_timeout_seconds)
            or not math.isfinite(selected_limits.connect_timeout_seconds)
            or not math.isfinite(selected_limits.idle_timeout_seconds)
            or selected_limits.header_timeout_seconds <= 0
            or selected_limits.connect_timeout_seconds <= 0
            or selected_limits.idle_timeout_seconds <= 0
            or type(selected_limits.max_bytes_each_direction) is not int
            or not 1 <= selected_limits.max_bytes_each_direction <= 16 * 1024**3
        ):
            raise KernelError("network_policy_invalid", "出口网关时限无效")
        self.snapshot = snapshot
        self.limits = selected_limits
        self.digest = egress_gateway_digest(snapshot, selected_limits)
        self._connector = connector

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            header = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), self.limits.header_timeout_seconds
            )
            if len(header) > MAX_PROXY_HEADER_BYTES:
                raise KernelError("network_proxy_request_denied", "代理请求头超过上限")
            line, *_ = header.split(b"\r\n")
            try:
                method, target, version = line.decode("ascii").split(" ")
            except (UnicodeError, ValueError):
                raise KernelError("network_proxy_request_denied", "代理请求行无效") from None
            if method != "CONNECT" or version != "HTTP/1.1":
                await self._response(writer, 405, "CONNECT Required")
                return
            host, port = _authority(target)
            try:
                ipaddress.ip_address(host)
            except ValueError:
                protocol: DestinationProtocol = "https"
            else:
                protocol = self._ip_protocol(host, port)
            addresses = authorized_addresses(self.snapshot, host, port, protocol)
            upstream_reader, upstream_writer = await self._open(addresses, port)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            prefix = b""
            if protocol == "https" and not _is_ip(host):
                prefix, server_name = await asyncio.wait_for(
                    read_tls_client_hello(reader), self.limits.header_timeout_seconds
                )
                if server_name != canonical_domain(host):
                    raise KernelError("network_tls_identity_denied", "TLS SNI与批准目标不一致")
                upstream_writer.write(prefix)
                await upstream_writer.drain()
            await self._relay(reader, writer, upstream_reader, upstream_writer)
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
            KernelError,
            OSError,
        ):
            if not writer.is_closing():
                await self._response(writer, 403, "Egress Denied")
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            writer.close()
            await writer.wait_closed()

    def _ip_protocol(self, host: str, port: int) -> DestinationProtocol:
        for protocol in ("https", "tcp"):
            try:
                authorized_addresses(self.snapshot, host, port, protocol)
            except KernelError:
                continue
            return protocol
        raise KernelError("network_destination_denied", "IP目标协议未授权")

    async def _open(
        self, addresses: tuple[str, ...], port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        for address in addresses:
            try:
                return await asyncio.wait_for(
                    self._connector(address, port), self.limits.connect_timeout_seconds
                )
            except (OSError, TimeoutError):
                continue
        raise KernelError("network_connect_failed", "批准目标连接失败")

    async def _relay(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        async def copy(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
            observed = 0
            while data := await asyncio.wait_for(
                source.read(65536), self.limits.idle_timeout_seconds
            ):
                observed += len(data)
                if observed > self.limits.max_bytes_each_direction:
                    raise KernelError("network_transfer_limit", "出口连接超过字节上限")
                target.write(data)
                await target.drain()

        tasks = {
            asyncio.create_task(copy(client_reader, upstream_writer)),
            asyncio.create_task(copy(upstream_reader, client_writer)),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)

    @staticmethod
    async def _response(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
        if writer.is_closing():
            return
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode(
                "ascii"
            )
        )
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True
