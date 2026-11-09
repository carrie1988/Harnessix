"""模型端点TCP转发：只做字节双向转发，不解析、不记录内容；只依赖标准库。

三处使用：
- 任务容器内由协议驱动启动，监听容器本机回环端口（产品只允许回环地址使用明文HTTP）；
- 宿主机上独立运行，监听Docker网桥地址，转发到只绑定宿主机回环的vLLM；
- 断网试次的模型转发边车（compose/network-isolation.yaml）：在试次自己的容器网络内转发到宿主机转发。
监听地址必须显式给出，拒绝0.0.0.0和::等通配地址，避免把模型服务暴露到外部网络；只有声明在独立
容器网络内运行（--container）时才允许通配地址，该容器不发布宿主机端口。

宿主机规则可以列出多个目标（以`|`分隔），用于在多个模型副本间分摊并发试次：活跃来访
（一个运行中的试次容器）始终转发到同一副本，保留该会话在副本上的前缀缓存；新来访分给活跃来访最少
的副本，并列时优先原副本。宿主机给出Docker套接字时按运行中容器识别来访：容器在运行即活跃、删除即
离开，复用其地址的新容器按新来访分配；否则按连接活动判断来访是否仍在。
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import ipaddress
import json
import logging
import socket
import time
from collections import Counter
from collections.abc import Callable, Mapping

Endpoint = tuple[str, int]
TARGET_SEPARATOR = "|"
# 不是运行中容器的来访（或宿主机没有给出Docker套接字）：没有打开的连接且超过该时长没有连接活动
# 即视为离开，不再计入副本负载，再来时重新分配。不保持连接的客户端在长命令期间离开后再来，
# 负载相同时仍回到原副本。
IDLE_SECONDS = 120.0
DOCKER_API_VERSION = "v1.41"
DOCKER_TIMEOUT_SECONDS = 5.0
_LOG = logging.getLogger(__name__)
# 容器列表查询失败（守护进程不可达、非200或返回结构异常）只影响这条连接的识别方式，不中断转发。
_DOCKER_LIST_ERRORS = (
    OSError,
    ValueError,
    LookupError,
    TypeError,
    AttributeError,
    http.client.HTTPException,
)


def _endpoint(value: str) -> Endpoint:
    host, _, port = value.rpartition(":")
    host = host.strip("[]")
    if not host or not port.isdecimal() or not 0 <= int(port) < 65536:
        raise ValueError("转发端点须为主机:端口")
    return host, int(port)


def parse(spec: str, container: bool = False) -> tuple[Endpoint, tuple[Endpoint, ...]]:
    """解析“监听主机:端口=目标主机:端口[|目标主机:端口…]”；容器网络外监听地址不能是通配地址。"""
    listen, separator, targets = spec.partition("=")
    if not separator:
        raise ValueError("转发规则须为 监听=目标")
    listen_endpoint = _endpoint(listen)
    target_endpoints = tuple(_endpoint(target) for target in targets.split(TARGET_SEPARATOR))
    if len(set(target_endpoints)) != len(target_endpoints):
        raise ValueError("转发目标重复")
    try:
        wildcard = ipaddress.ip_address(listen_endpoint[0]).is_unspecified
    except ValueError:
        wildcard = False
    if wildcard and not container:
        raise ValueError("转发不能监听通配地址")
    return listen_endpoint, target_endpoints


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def container_addresses(containers: object) -> dict[str, str]:
    """Docker容器列表中每个网络地址到容器ID；不在任何网络上的容器没有地址。"""
    if not isinstance(containers, list):
        raise ValueError("Docker容器列表须为数组")
    addresses: dict[str, str] = {}
    for container in containers:
        settings = container.get("NetworkSettings") or {}
        for network in (settings.get("Networks") or {}).values():
            for field in ("IPAddress", "GlobalIPv6Address"):
                if address := (network or {}).get(field):
                    addresses[address] = container["Id"]
    return addresses


def running_containers(socket_path: str) -> dict[str, str]:
    """只读查询宿主机Docker：运行中容器的地址到容器ID。只调用GET /containers/json。"""
    connection = _UnixHTTPConnection(socket_path, DOCKER_TIMEOUT_SECONDS)
    try:
        connection.request("GET", f"/{DOCKER_API_VERSION}/containers/json")
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()
    if response.status != 200:
        raise OSError(f"Docker API返回{response.status}")
    return container_addresses(json.loads(body))


class StickyBalancer:
    """来访到目标的分配：活跃来访留在原目标，新来访与离开后再来的来访分给活跃来访最少的目标。

    给出运行中容器（running）时，其中的来访只要容器在运行就活跃，容器删除后即被遗忘；
    其余来访有打开的连接，或最近idle_seconds内开始或结束过连接，才算活跃。
    并列时优先该来访原来的目标，再取靠前者。
    """

    def __init__(
        self,
        targets: tuple[Endpoint, ...],
        idle_seconds: float = IDLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not targets:
            raise ValueError("至少需要一个转发目标")
        self._targets, self._idle, self._clock = targets, idle_seconds, clock
        self._assigned: dict[str, int] = {}
        self._seen: dict[str, float] = {}
        self._open: Counter[str] = Counter()
        self._containers: set[str] = set()

    def _active(self, client: str, now: float, running: frozenset[str] | None) -> bool:
        if running is not None and client in self._containers:
            return True
        return self._open[client] > 0 or now - self._seen[client] <= self._idle

    def _forget_removed(self, running: frozenset[str]) -> None:
        for container in self._containers - running:
            self._containers.discard(container)
            self._assigned.pop(container, None)
            self._seen.pop(container, None)

    def acquire(self, client: str, running: frozenset[str] | None = None) -> Endpoint:
        """来访开始一条连接：返回它应转发到的目标，并把这条连接计为打开。

        running是此刻运行中容器的ID；None表示不知道，所有来访都按连接活动判断。
        """
        now = self._clock()
        if running is not None:
            self._forget_removed(running)
            if client in running:
                self._containers.add(client)
        previous = self._assigned.get(client)
        if previous is None or not self._active(client, now, running):
            load = Counter(
                index
                for other, index in self._assigned.items()
                if other != client and self._active(other, now, running)
            )
            self._assigned[client] = min(
                range(len(self._targets)), key=lambda i: (load[i], i != previous, i)
            )
            _LOG.info(
                "%s -> %s:%d (active per target before: %s)",
                client[:12],
                *self._targets[self._assigned[client]],
                [load[i] for i in range(len(self._targets))],
            )
        self._open[client] += 1
        self._seen[client] = now
        return self._targets[self._assigned[client]]

    def release(self, client: str) -> None:
        """来访的一条连接结束；结束时刻也算活动。已删除并被遗忘的容器不再记录。"""
        if self._open[client] > 0:
            self._open[client] -= 1
        if not self._open[client]:
            del self._open[client]
        if client in self._assigned:
            self._seen[client] = self._clock()


async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while chunk := await source.read(65_536):
            sink.write(chunk)
            await sink.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        sink.close()


async def start(
    listen: Endpoint,
    targets: tuple[Endpoint, ...],
    balancer: StickyBalancer | None = None,
    containers: Callable[[], Mapping[str, str]] | None = None,
) -> asyncio.Server:
    """逐连接识别来访、选目标、打开新连接并双向转发；目标不可达时直接关闭来访连接。

    containers返回运行中容器的地址到容器ID：来访地址属于运行中容器时以容器ID识别。查询失败时
    这条连接按来访地址与连接活动处理。查询与分配串行，避免较早的容器列表遗忘刚启动的容器。
    """
    chooser = balancer or StickyBalancer(targets)
    choosing = asyncio.Lock()
    docker_failed = False

    async def choose(address: str) -> tuple[str, Endpoint]:
        nonlocal docker_failed
        async with choosing:
            if containers is None:
                return address, chooser.acquire(address)
            try:
                by_address = await asyncio.to_thread(containers)
            except _DOCKER_LIST_ERRORS:
                if not docker_failed:
                    _LOG.warning("Docker容器列表不可用，按连接活动判断来访", exc_info=True)
                docker_failed = True
                return address, chooser.acquire(address)
            if docker_failed:
                _LOG.warning("Docker容器列表恢复")
            docker_failed = False
            client = by_address.get(address, address)
            return client, chooser.acquire(client, frozenset(by_address.values()))

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client, target = await choose(str(writer.get_extra_info("peername")[0]))
        try:
            try:
                upstream_reader, upstream_writer = await asyncio.open_connection(*target)
            except OSError:
                writer.close()
                return
            await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))
        finally:
            chooser.release(client)

    return await asyncio.start_server(handle, *listen)


async def _serve(specs: list[str], docker_socket: str | None, container: bool) -> None:
    containers = None
    if docker_socket is not None:
        path = docker_socket

        def containers() -> dict[str, str]:
            return running_containers(path)

    servers = [await start(*parse(spec, container), containers=containers) for spec in specs]
    try:
        await asyncio.gather(*(server.serve_forever() for server in servers))
    finally:
        for server in servers:
            server.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="模型端点TCP转发（前台运行）")
    parser.add_argument("--relay", action="append", required=True, help="监听=目标[|目标…]，可重复")
    parser.add_argument(
        "--docker-socket",
        help="宿主机Docker套接字：按运行中容器识别来访（只读调用GET /containers/json）",
    )
    parser.add_argument(
        "--container", action="store_true", help="在独立容器网络内运行，允许通配监听地址"
    )
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_serve(arguments.relay, arguments.docker_socket, arguments.container))


if __name__ == "__main__":
    main()
