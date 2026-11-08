"""宿主机模型转发：多个副本之间按来访地址固定分配，新来访分给近期活跃最少的副本。"""

from __future__ import annotations

import asyncio

import pytest

from integrations.harbor.model_relay import StickyBalancer, parse, start

A, B = ("127.0.0.1", 18001), ("127.0.0.1", 18002)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_parse_accepts_several_targets_and_rejects_duplicates() -> None:
    assert parse("172.17.0.1:18100=127.0.0.1:18001|127.0.0.1:18002") == (
        ("172.17.0.1", 18100),
        (A, B),
    )
    assert parse("127.0.0.1:18001=172.17.0.1:18101") == (
        ("127.0.0.1", 18001),
        (("172.17.0.1", 18101),),
    )
    with pytest.raises(ValueError):
        parse("172.17.0.1:18100=127.0.0.1:18001|127.0.0.1:18001")
    with pytest.raises(ValueError):
        parse("0.0.0.0:18100=127.0.0.1:18001|127.0.0.1:18002")


def test_concurrent_trials_split_evenly_and_stay_on_their_replica() -> None:
    """四个试次几乎同时开始：两两分到两个副本；之后每个试次的请求都回到同一副本。"""
    balancer = StickyBalancer((A, B), clock=Clock())
    first = [balancer.target_for(f"172.20.0.{n}") for n in range(2, 6)]
    assert first == [A, B, A, B]
    assert [balancer.target_for(f"172.20.0.{n}") for n in range(2, 6)] == first


def test_finished_trials_stop_counting_after_the_idle_window() -> None:
    clock = Clock()
    balancer = StickyBalancer((A, B), idle_seconds=600, clock=clock)
    for client in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
        balancer.target_for(client)  # A, B, A
    clock.now = 500
    balancer.touch("10.0.0.2")  # B上的试次仍在运行（长命令刚结束）
    clock.now = 1000  # 两个A上的试次已超过空闲窗口，只剩B上的一个计入负载
    assert balancer.target_for("10.0.0.4") == A
    assert balancer.target_for("10.0.0.5") == A  # A、B各有一个活跃来访，并列取靠前者
    assert balancer.target_for("10.0.0.6") == B


async def test_relay_keeps_one_client_on_one_replica() -> None:
    async def upstream(name: bytes) -> asyncio.Server:
        async def reply(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readline()
            writer.write(name)
            await writer.drain()
            writer.close()

        return await asyncio.start_server(reply, "127.0.0.1", 0)

    first, second = await upstream(b"a"), await upstream(b"b")
    targets = tuple(("127.0.0.1", s.sockets[0].getsockname()[1]) for s in (first, second))
    relay = await start(("127.0.0.1", 0), targets)
    port = relay.sockets[0].getsockname()[1]

    async def ask() -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"ping\n")
        await writer.drain()
        answer = await reader.read()
        writer.close()
        return answer

    try:
        assert {await ask() for _ in range(4)} == {b"a"}
    finally:
        for server in (relay, first, second):
            server.close()
