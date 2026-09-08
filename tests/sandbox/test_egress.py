from __future__ import annotations

import asyncio
import ssl
from datetime import UTC, datetime

import pytest

from harnessix.agent.errors import KernelError
from harnessix.sandbox.contracts import NetworkDestination, NetworkPolicy
from harnessix.sandbox.egress import ManagedEgressGateway, parse_tls_server_name
from harnessix.sandbox.network import resolve_network_policy


def _client_hello(hostname: str) -> bytes:
    context = ssl.create_default_context()
    incoming = ssl.MemoryBIO()
    outgoing = ssl.MemoryBIO()
    connection = context.wrap_bio(incoming, outgoing, server_hostname=hostname)
    with pytest.raises(ssl.SSLWantReadError):
        connection.do_handshake()
    return outgoing.read()


def _handshake(records: bytes) -> bytes:
    result = bytearray()
    cursor = 0
    while cursor < len(records):
        assert records[cursor] == 22
        length = int.from_bytes(records[cursor + 3 : cursor + 5], "big")
        result.extend(records[cursor + 5 : cursor + 5 + length])
        cursor += 5 + length
    return bytes(result)


def _policy(now: datetime):
    policy = NetworkPolicy(
        mode="restricted",
        destinations=(
            NetworkDestination(
                kind="domain",
                value="packages.example.com",
                protocol="https",
                ports=(443,),
            ),
        ),
        allow_private_addresses=True,
    )
    return resolve_network_policy(policy, resolver=lambda _: ("127.0.0.1",), now=now)


def test_tls_client_hello_requires_exact_canonical_sni() -> None:
    assert parse_tls_server_name(_handshake(_client_hello("packages.example.com"))) == (
        "packages.example.com"
    )
    with pytest.raises(KernelError) as absent:
        parse_tls_server_name(b"\x01\x00\x00\x00")
    assert absent.value.code == "network_tls_identity_denied"


async def test_gateway_relays_only_after_connect_target_and_tls_sni_match() -> None:
    now = datetime.now(UTC)
    upstream_received = asyncio.Event()
    observed = bytearray()

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        observed.extend(await reader.read(65536))
        upstream_received.set()
        writer.write(b"upstream-response")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    upstream_port = upstream_server.sockets[0].getsockname()[1]

    async def connector(address: str, port: int):
        assert (address, port) == ("127.0.0.1", 443)
        return await asyncio.open_connection("127.0.0.1", upstream_port)

    gateway = ManagedEgressGateway(_policy(now), connector=connector)
    gateway_server = await asyncio.start_server(gateway.handle, "127.0.0.1", 0)
    gateway_port = gateway_server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", gateway_port)
        writer.write(b"CONNECT packages.example.com:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert await reader.readuntil(b"\r\n\r\n") == (
            b"HTTP/1.1 200 Connection Established\r\n\r\n"
        )
        hello = _client_hello("packages.example.com")
        writer.write(hello)
        await writer.drain()
        assert await reader.readexactly(len(b"upstream-response")) == b"upstream-response"
        await asyncio.wait_for(upstream_received.wait(), 1)
        assert observed == hello
        writer.close()
        await writer.wait_closed()
    finally:
        gateway_server.close()
        upstream_server.close()
        await gateway_server.wait_closed()
        await upstream_server.wait_closed()


async def test_gateway_does_not_forward_mismatched_sni() -> None:
    now = datetime.now(UTC)
    observed = bytearray()

    async def connector(address: str, port: int):
        reader = asyncio.StreamReader()

        class Writer:
            def write(self, data):
                observed.extend(data)

            async def drain(self):
                return None

            def close(self):
                return None

            async def wait_closed(self):
                return None

        return reader, Writer()  # type: ignore[return-value]

    gateway = ManagedEgressGateway(_policy(now), connector=connector)
    server = await asyncio.start_server(gateway.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT packages.example.com:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert b"200 Connection Established" in await reader.readuntil(b"\r\n\r\n")
        writer.write(_client_hello("evil.example.com"))
        await writer.drain()
        await reader.read()
        assert observed == b""
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
