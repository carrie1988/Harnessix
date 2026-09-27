"""真实产品CLI子进程、Provider装配和OS管道；仅握手/查询，不创建模型Turn。"""

from __future__ import annotations

import asyncio
import json
import sys

from harnessix.product_config.contracts import (
    EnvironmentSecretSourceConfig,
    ModelCapabilities,
    ModelProfile,
    ProductConfigV2,
    ProviderDefinition,
    SecretReference,
)
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.test_publication import CANARY
from tests.app_server.test_frame_publication import frame, initialization
from tests.product_config.conftest import write_config


async def test_actual_product_cli_does_not_echo_sensitive_rpc_metadata(tmp_path, monkeypatch):
    reference = SecretReference(name="api", version="9")
    config = ProductConfigV2(
        active_profile="offline",
        secret_sources=(
            EnvironmentSecretSourceConfig(
                secret=reference, environment_variable="HARNESSIX_FRAME_TEST_KEY"
            ),
        ),
        providers=(
            ProviderDefinition(
                provider_id="offline",
                kind="openai_chat",
                base_url="https://api.invalid.test/v1",
                credential=reference,
            ),
        ),
        profiles=(
            ModelProfile(
                profile_id="offline",
                provider_id="offline",
                model="unused",
                capabilities=ModelCapabilities(),
            ),
        ),
    )
    path = write_config(tmp_path / "config.json", config)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("HARNESSIX_FRAME_TEST_KEY", CANARY)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "harnessix",
        "agent-server",
        "--config",
        str(path),
        "--workspace",
        str(workspace),
        "--state-directory",
        str(state),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    responses = []

    async def exchange(body):
        process.stdin.write(body)
        await process.stdin.drain()
        result = await asyncio.wait_for(process.stdout.readline(), 20)
        assert result, "产品子进程在响应前关闭"
        responses.append(result)
        return json.loads(result)

    try:
        denied = await exchange(frame("initialize", initialization(), CANARY))
        assert (
            denied["id"] is None and denied["error"]["data"]["code"] == "public_input_secret_leak"
        )
        params = initialization()
        params[CANARY] = "safe"
        denied = await exchange(frame("initialize", params, 2))
        assert denied["id"] == 2 and denied["error"]["data"]["code"] == "public_input_secret_leak"
        success = await exchange(frame("initialize", initialization(), 3))
        assert "result" in success
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n')
        await process.stdin.drain()
        listed = await exchange(frame("thread/list", {}, 4))
        assert listed["result"]["threads"] == []
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 20)
        stderr = await process.stderr.read()
        assert process.returncode == 0 and not stderr
        assert CANARY.encode() not in b"".join(responses)
        # 无Thread和Turn接受，真实Provider仅构造/关闭，不触发网络模型消费。
        store = SQLiteSessionStore(state / "sessions.db")
        assert await store.list_thread_page(after=None, archived=None, limit=200) == ((), False)
        assert not any(CANARY.encode() in p.read_bytes() for p in state.rglob("*.db*"))
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
