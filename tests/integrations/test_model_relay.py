"""宿主机模型转发：活跃来访固定在原副本，新来访与离开后再来的来访分给活跃来访最少的副本。"""

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


def request(balancer: StickyBalancer, client: str) -> tuple[str, int]:
    """一次不保持连接的请求：开始即结束。"""
    target = balancer.acquire(client)
    balancer.release(client)
    return target


def test_concurrent_trials_split_evenly_and_stay_on_their_replica() -> None:
    """四个试次几乎同时开始：两两分到两个副本；之后每个试次的请求都回到同一副本。"""
    balancer = StickyBalancer((A, B), clock=Clock())
    first = [request(balancer, f"172.20.0.{n}") for n in range(2, 6)]
    assert first == [A, B, A, B]
    assert [request(balancer, f"172.20.0.{n}") for n in range(2, 6)] == first


def test_open_connections_keep_a_trial_on_its_replica_past_the_idle_window() -> None:
    """保持长连接的试次在长命令期间不发请求，仍计入原副本负载，新试次分到另一副本。"""
    clock = Clock()
    balancer = StickyBalancer((A, B), idle_seconds=120, clock=clock)
    assert balancer.acquire("10.0.0.1") == A  # 连接保持打开
    clock.now = 900
    assert request(balancer, "10.0.0.2") == B
    assert request(balancer, "10.0.0.1") == A


def test_finished_trials_stop_counting_and_a_reused_address_is_rebalanced() -> None:
    """试次结束（连接全部关闭且超过空闲窗口）后不再计入负载；复用其地址的新试次按当前负载分配。"""
    clock = Clock()
    balancer = StickyBalancer((A, B), idle_seconds=120, clock=clock)
    assert [request(balancer, c) for c in ("10.0.0.1", "10.0.0.2", "10.0.0.3")] == [A, B, A]
    clock.now = 100
    balancer.acquire("10.0.0.3")  # A上仍在运行的试次
    clock.now = 300  # 10.0.0.1与10.0.0.2已离开，A上只剩10.0.0.3
    assert request(balancer, "10.0.0.1") == B  # 地址被新试次复用：不再沿用旧分配A
    assert request(balancer, "10.0.0.4") == A  # A、B各一个活跃来访，并列取靠前者


def test_a_returning_trial_prefers_its_previous_replica_when_loads_tie() -> None:
    """不保持连接的试次在长命令后再来，负载相同时回到原副本，保留前缀缓存。"""
    clock = Clock()
    balancer = StickyBalancer((A, B), idle_seconds=120, clock=clock)
    assert [request(balancer, c) for c in ("10.0.0.1", "10.0.0.2", "10.0.0.3")] == [A, B, A]
    clock.now = 500  # 三个来访都已离开，负载0:0
    assert request(balancer, "10.0.0.2") == B


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
