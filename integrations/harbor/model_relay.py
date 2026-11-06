"""模型端点TCP转发：只做字节双向转发，不解析、不记录内容；只依赖标准库。

两处使用：
- 任务容器内由协议驱动启动，监听容器本机回环端口（产品只允许回环地址使用明文HTTP）；
- 宿主机上独立运行，监听Docker网桥地址，转发到只绑定宿主机回环的vLLM。
监听地址必须显式给出，拒绝0.0.0.0和::等通配地址，避免把模型服务暴露到外部网络。
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress

Endpoint = tuple[str, int]


def _endpoint(value: str) -> Endpoint:
    host, _, port = value.rpartition(":")
    host = host.strip("[]")
    if not host or not port.isdecimal() or not 0 <= int(port) < 65536:
        raise ValueError("转发端点须为主机:端口")
    return host, int(port)


def parse(spec: str) -> tuple[Endpoint, Endpoint]:
    """解析“监听主机:端口=目标主机:端口”；监听地址不能是通配地址。"""
    listen, separator, target = spec.partition("=")
    if not separator:
        raise ValueError("转发规则须为 监听=目标")
    listen_endpoint, target_endpoint = _endpoint(listen), _endpoint(target)
    try:
        wildcard = ipaddress.ip_address(listen_endpoint[0]).is_unspecified
    except ValueError:
        wildcard = False
    if wildcard:
        raise ValueError("转发不能监听通配地址")
    return listen_endpoint, target_endpoint


async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while chunk := await source.read(65_536):
            sink.write(chunk)
            await sink.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        sink.close()


async def start(listen: Endpoint, target: Endpoint) -> asyncio.Server:
    """逐连接打开到目标的新连接并双向转发；目标不可达时直接关闭来访连接。"""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(*target)
        except OSError:
            writer.close()
            return
        await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))

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
    parser.add_argument("--relay", action="append", required=True, help="监听=目标，可重复")
    asyncio.run(_serve(parser.parse_args().relay))


if __name__ == "__main__":
    main()
