"""模型端点TCP转发：只做字节双向转发，不解析、不记录内容；只依赖标准库。

两处使用：
- 任务容器内由协议驱动启动，监听容器本机回环端口（产品只允许回环地址使用明文HTTP）；
- 宿主机上独立运行，监听Docker网桥地址，转发到只绑定宿主机回环的vLLM。
监听地址必须显式给出，拒绝0.0.0.0和::等通配地址，避免把模型服务暴露到外部网络。

宿主机规则可以列出多个目标（以`|`分隔），用于在多个模型副本间分摊并发试次：同一来访地址
（即同一个试次容器）始终转发到同一副本，保留该会话在副本上的前缀缓存；新来访地址分给近期活跃
来访最少的副本。
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import time
from collections import Counter
from collections.abc import Callable

Endpoint = tuple[str, int]
TARGET_SEPARATOR = "|"
# 来访地址超过该时长没有连接活动即不再计入副本负载；须长于单条命令的最长耗时与模型调用间隔。
IDLE_SECONDS = 600.0


def _endpoint(value: str) -> Endpoint:
    host, _, port = value.rpartition(":")
    host = host.strip("[]")
    if not host or not port.isdecimal() or not 0 <= int(port) < 65536:
        raise ValueError("转发端点须为主机:端口")
    return host, int(port)


def parse(spec: str) -> tuple[Endpoint, tuple[Endpoint, ...]]:
    """解析“监听主机:端口=目标主机:端口[|目标主机:端口…]”；监听地址不能是通配地址。"""
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
    if wildcard:
        raise ValueError("转发不能监听通配地址")
    return listen_endpoint, target_endpoints


class StickyBalancer:
    """来访地址到目标的固定分配：首次出现时分给近期活跃来访最少的目标（并列取靠前者）。"""

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

    def target_for(self, client: str) -> Endpoint:
        now = self._clock()
        if client not in self._assigned:
            live = Counter(
                index
                for other, index in self._assigned.items()
                if now - self._seen[other] <= self._idle
            )
            self._assigned[client] = min(range(len(self._targets)), key=lambda i: (live[i], i))
        self._seen[client] = now
        return self._targets[self._assigned[client]]

    def touch(self, client: str) -> None:
        """连接结束也算活动：长时间运行的命令结束前，该来访仍计入其副本负载。"""
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
    listen: Endpoint, targets: tuple[Endpoint, ...], balancer: StickyBalancer | None = None
) -> asyncio.Server:
    """逐连接按来访地址选目标、打开新连接并双向转发；目标不可达时直接关闭来访连接。"""
    chooser = balancer or StickyBalancer(targets)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = str(writer.get_extra_info("peername")[0])
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                *chooser.target_for(client)
            )
        except OSError:
            writer.close()
            return
        try:
            await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))
        finally:
            chooser.touch(client)

    return await asyncio.start_server(handle, *listen)


async def _serve(specs: list[str]) -> None:
    servers = [await start(*parse(spec)) for spec in specs]
    try:
        await asyncio.gather(*(server.serve_forever() for server in servers))
    finally:
        for server in servers:
            server.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="宿主机模型端点转发（前台运行）")
    parser.add_argument("--relay", action="append", required=True, help="监听=目标[|目标…]，可重复")
    asyncio.run(_serve(parser.parse_args().relay))


if __name__ == "__main__":
    main()
