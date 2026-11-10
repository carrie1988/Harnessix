"""宿主机模型转发：活跃来访固定在原副本，新来访与离开后再来的来访分给活跃来访最少的副本。"""

from __future__ import annotations

import asyncio
import json
import socketserver
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from integrations.harbor.model_relay import (
    StickyBalancer,
    container_addresses,
    parse,
    running_containers,
    start,
)

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


def test_only_a_container_network_relay_may_listen_on_the_wildcard_address() -> None:
    """断网试次的模型转发边车在自己的容器网络内监听全部地址，不发布宿主机端口。"""
    assert parse("0.0.0.0:18100=host.docker.internal:18100", container=True) == (
        ("0.0.0.0", 18100),
        (("host.docker.internal", 18100),),
    )
    for spec in ("0.0.0.0:18100=host.docker.internal:18100", "[::]:18100=127.0.0.1:18001"):
        with pytest.raises(ValueError):
            parse(spec)


def request(
    balancer: StickyBalancer, client: str, running: frozenset[str] | None = None
) -> tuple[str, int]:
    """一次不保持连接的请求：开始即结束。"""
    target = balancer.acquire(client, running)
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


def test_a_running_container_keeps_its_replica_however_long_it_is_quiet() -> None:
    """按运行中容器识别：长命令期间没有任何连接，容器仍在运行就仍占原副本，不会被挪走。"""
    clock = Clock()
    balancer = StickyBalancer((A, B), idle_seconds=120, clock=clock)
    running = frozenset({"c1", "c2"})
    assert [request(balancer, c, running) for c in ("c1", "c2")] == [A, B]
    clock.now = 1000
    running = frozenset({"c1", "c2", "c3"})
    assert request(balancer, "c3", running) == A
    assert request(balancer, "c1", running) == A
    assert request(balancer, "c2", running) == B


def test_a_removed_container_frees_its_replica_at_once() -> None:
    """试次结束、容器删除后立即不计负载：几秒后启动的新试次分到空出的副本，而不是并列取靠前者。"""
    clock = Clock()
    balancer = StickyBalancer((A, B), idle_seconds=120, clock=clock)
    running = frozenset({"c1", "c2", "c3", "c4"})
    assert [request(balancer, c, running) for c in ("c1", "c2", "c3", "c4")] == [A, B, A, B]
    clock.now = 10
    running = frozenset({"c1", "c3", "c4", "c5"})  # B上的c2刚结束
    assert request(balancer, "c5", running) == B


def test_without_a_container_list_every_client_falls_back_to_connection_activity() -> None:
    """容器列表暂时不可用：已识别的容器也按连接活动判断，结束的试次不会一直占着副本。"""
    clock = Clock()
    balancer = StickyBalancer((A, B), idle_seconds=120, clock=clock)
    running = frozenset({"c1", "c2"})
    assert [request(balancer, c, running) for c in ("c1", "c2")] == [A, B]
    clock.now = 500
    assert request(balancer, "10.0.0.7") == A
    assert request(balancer, "10.0.0.8") == B


def test_container_addresses_map_every_network_address_to_its_container() -> None:
    containers = [
        {"Id": "c1", "NetworkSettings": {"Networks": {"t1": {"IPAddress": "172.20.0.2"}}}},
        {"Id": "c2", "NetworkSettings": {"Networks": {}}},
        {
            "Id": "c3",
            "NetworkSettings": {
                "Networks": {
                    "a": {"IPAddress": "172.21.0.3", "GlobalIPv6Address": ""},
                    "b": {"IPAddress": "172.22.0.3", "GlobalIPv6Address": "fd00::3"},
                }
            },
        },
    ]
    assert container_addresses(containers) == {
        "172.20.0.2": "c1",
        "172.21.0.3": "c3",
        "172.22.0.3": "c3",
        "fd00::3": "c3",
    }
    with pytest.raises(ValueError):
        container_addresses({"message": "not a list"})


@pytest.fixture
def docker_socket() -> Iterator[tuple[Path, list[str], list[tuple[int, object]]]]:
    """假Docker守护进程：在Unix套接字上按队列逐个回答请求，并记录请求行。"""
    requests: list[str] = []
    answers: list[tuple[int, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(f"{self.command} {self.path}")
            status, payload = answers.pop(0)
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def address_string(self) -> str:
            return "docker"

        def log_message(self, format: str, *args: object) -> None:
            pass

    with tempfile.TemporaryDirectory(prefix="relay") as directory:
        path = Path(directory) / "d.sock"
        server = socketserver.UnixStreamServer(str(path), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield path, requests, answers
        finally:
            server.shutdown()
            server.server_close()


def test_running_containers_only_lists_containers_over_the_docker_socket(
    docker_socket: tuple[Path, list[str], list[tuple[int, object]]],
) -> None:
    path, requests, answers = docker_socket
    answers.append(
        (200, [{"Id": "c1", "NetworkSettings": {"Networks": {"t": {"IPAddress": "172.20.0.2"}}}}])
    )
    answers.append((500, {"message": "daemon error"}))
    assert running_containers(str(path)) == {"172.20.0.2": "c1"}
    with pytest.raises(OSError):
        running_containers(str(path))
    assert requests == ["GET /v1.41/containers/json"] * 2


class RecordingBalancer(StickyBalancer):
    def __init__(self, targets: tuple[tuple[str, int], ...]) -> None:
        super().__init__(targets)
        self.clients: list[tuple[str, frozenset[str] | None]] = []

    def acquire(self, client: str, running: frozenset[str] | None = None) -> tuple[str, int]:
        self.clients.append((client, running))
        return super().acquire(client, running)


async def upstream(name: bytes) -> asyncio.Server:
    async def reply(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(name)
        await writer.drain()
        writer.close()

    return await asyncio.start_server(reply, "127.0.0.1", 0)


async def ask(port: int) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"ping\n")
    await writer.drain()
    answer = await reader.read()
    writer.close()
    return answer


async def test_relay_keeps_one_client_on_one_replica() -> None:
    first, second = await upstream(b"a"), await upstream(b"b")
    targets = tuple(("127.0.0.1", s.sockets[0].getsockname()[1]) for s in (first, second))
    relay = await start(("127.0.0.1", 0), targets)
    port = relay.sockets[0].getsockname()[1]
    try:
        assert {await ask(port) for _ in range(4)} == {b"a"}
    finally:
        for server in (relay, first, second):
            server.close()


async def test_relay_identifies_clients_by_container_and_survives_a_failed_list() -> None:
    """来访地址属于运行中容器时以容器ID分配；容器列表查询失败时这条连接按来访地址照常转发。"""
    first, second = await upstream(b"a"), await upstream(b"b")
    targets = tuple(("127.0.0.1", s.sockets[0].getsockname()[1]) for s in (first, second))
    views: list[dict[str, str] | OSError] = [
        {"127.0.0.1": "c1", "172.20.0.9": "c0"},
        OSError("docker unavailable"),
        {"127.0.0.1": "c2"},
    ]

    def containers() -> dict[str, str]:
        view = views.pop(0)
        if isinstance(view, OSError):
            raise view
        return view

    balancer = RecordingBalancer(targets)
    relay = await start(("127.0.0.1", 0), targets, balancer, containers)
    port = relay.sockets[0].getsockname()[1]
    try:
        assert [await ask(port) for _ in range(3)] == [b"a", b"b", b"a"]
    finally:
        for server in (relay, first, second):
            server.close()
    assert balancer.clients == [
        ("c1", frozenset({"c1", "c0"})),
        ("127.0.0.1", None),
        ("c2", frozenset({"c2"})),
    ]
