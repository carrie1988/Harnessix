from __future__ import annotations

import io
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.models.scripted import FakeProvider
from harnessix.product_config.cli import config_main
from harnessix.product_config.codec import load_product_config
from harnessix.product_config.contracts import ProductConfigSnapshot, ProductConfigV2
from harnessix.product_config.server import run_product_stdio
from harnessix.product_config.store import SQLiteProductConfigStore
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import AgentClient, AgentSDKError, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from tests.product_config.conftest import write_config
from tests.product_config.test_migration_and_store import legacy_body

CANARY = "product-cli-secret-canary"


def _credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIMARY_API_KEY", CANARY)
    monkeypatch.setenv("BACKUP_API_KEY", "backup-secret")


async def test_product_server_starts_and_closes_on_eof_without_model_request(
    tmp_path: Path,
    config: ProductConfigV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _credentials(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    path = write_config(tmp_path / "config.json", config)
    output = io.BytesIO()

    await run_product_stdio(
        config_path=path,
        profile_id=None,
        workspace=workspace,
        state_directory=state,
        input_stream=io.BytesIO(),
        output_stream=output,
    )

    assert output.getvalue() == b""
    snapshot = load_product_config(path)
    assert isinstance(snapshot, ProductConfigSnapshot)
    with SQLiteProductConfigStore(state / "product-config.db") as store:
        assert store.active() == (snapshot.config_sha256, "primary")
        assert [event.operation for event in store.config_events()] == ["loaded", "activated"]
    assert CANARY not in (state / "product-config.db").read_bytes().decode("utf-8", errors="ignore")


async def test_product_server_advertises_default_scoped_artifact_reader(
    tmp_path: Path,
    config: ProductConfigV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _credentials(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    path = write_config(tmp_path / "config.json", config)
    initialize = {
        "jsonrpc": "2.0",
        "id": "initialize-artifacts",
        "method": "initialize",
        "params": {
            "protocolVersion": "1.0",
            "clientInfo": {"name": "product-artifact-test", "version": "1"},
            "clientInstanceId": str(uuid4()),
        },
    }
    initialized = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }
    read_missing_artifact = {
        "jsonrpc": "2.0",
        "id": "read-missing-artifact",
        "method": "artifact/read",
        "params": {
            "threadId": str(uuid4()),
            "artifactId": str(uuid4()),
            "offset": 0,
            "limit": 1,
        },
    }
    source = io.BytesIO(
        (json.dumps(initialize, separators=(",", ":")) + "\n").encode()
        + (json.dumps(initialized, separators=(",", ":")) + "\n").encode()
        + (json.dumps(read_missing_artifact, separators=(",", ":")) + "\n").encode()
    )
    output = io.BytesIO()

    await run_product_stdio(
        config_path=path,
        profile_id=None,
        workspace=workspace,
        state_directory=state,
        input_stream=source,
        output_stream=output,
    )

    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(messages) == 2
    capabilities = messages[0]["result"]["capabilities"]
    assert capabilities["artifactPages"] is True
    assert "artifact/read" in capabilities["methods"]
    assert messages[1]["id"] == "read-missing-artifact"
    assert messages[1]["error"]["data"]["code"] == "thread_not_found"


async def test_provider_construction_failure_does_not_activate_config(
    tmp_path: Path,
    config: ProductConfigV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _credentials(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    path = write_config(tmp_path / "config.json", config)

    async def fail_build(*_args: object, **_kwargs: object) -> None:
        raise KernelError("product_provider_unavailable", "Provider构造失败")

    monkeypatch.setattr("harnessix.product_config.server.build_provider_bundle", fail_build)
    with pytest.raises(KernelError) as error:
        await run_product_stdio(
            config_path=path,
            profile_id=None,
            workspace=workspace,
            state_directory=state,
            input_stream=io.BytesIO(),
            output_stream=io.BytesIO(),
        )
    assert error.value.code == "product_provider_unavailable"
    with SQLiteProductConfigStore(state / "product-config.db") as store:
        assert store.active() is None
        assert [event.operation for event in store.config_events()] == ["loaded"]


async def test_session_initialization_failure_closes_provider_bundle(
    tmp_path: Path,
    config: ProductConfigV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _credentials(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    path = write_config(tmp_path / "config.json", config)

    class TrackingBundle:
        def __init__(self) -> None:
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            self.closed = True

    bundle = TrackingBundle()

    async def build(*_args: object, **_kwargs: object):
        return bundle

    async def fail_initialize(_store: SQLiteSessionStore) -> None:
        raise KernelError("storage_unavailable", "Session初始化失败")

    monkeypatch.setattr("harnessix.product_config.server.build_provider_bundle", build)
    monkeypatch.setattr(SQLiteSessionStore, "initialize", fail_initialize)
    with pytest.raises(KernelError) as error:
        await run_product_stdio(
            config_path=path,
            profile_id=None,
            workspace=workspace,
            state_directory=state,
            input_stream=io.BytesIO(),
            output_stream=io.BytesIO(),
        )
    assert error.value.code == "storage_unavailable"
    assert bundle.closed
    with SQLiteProductConfigStore(state / "product-config.db") as store:
        assert store.active() is None


async def test_runtime_owner_conflict_does_not_activate_or_open_protocol(
    tmp_path: Path,
    config: ProductConfigV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _credentials(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    path = write_config(tmp_path / "config.json", config)
    sessions = SQLiteSessionStore(state / "sessions.db")
    await sessions.initialize()
    output = io.BytesIO()

    async with sessions.runtime_owner():
        with pytest.raises(KernelError) as error:
            await run_product_stdio(
                config_path=path,
                profile_id=None,
                workspace=workspace,
                state_directory=state,
                input_stream=io.BytesIO(),
                output_stream=output,
            )
    assert error.value.code == "runtime_busy"
    assert output.getvalue() == b""
    with SQLiteProductConfigStore(state / "product-config.db") as store:
        assert store.active() is None
        assert [event.operation for event in store.config_events()] == ["loaded"]


async def test_product_server_rejects_configuration_inside_workspace(
    tmp_path: Path,
    config: ProductConfigV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _credentials(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = write_config(workspace / "config.json", config)
    with pytest.raises(KernelError) as error:
        await run_product_stdio(
            config_path=path,
            profile_id=None,
            workspace=workspace,
            state_directory=tmp_path / "state",
            input_stream=io.BytesIO(),
            output_stream=io.BytesIO(),
        )
    assert error.value.code == "product_config_overlap"
    assert not (tmp_path / "state").exists()

    outside = write_config(tmp_path / "outside.json", config)
    nested_state = workspace / "state"
    with pytest.raises(KernelError) as error:
        await run_product_stdio(
            config_path=outside,
            profile_id=None,
            workspace=workspace,
            state_directory=nested_state,
            input_stream=io.BytesIO(),
            output_stream=io.BytesIO(),
        )
    assert error.value.code == "product_state_overlap"
    assert not nested_state.exists()


async def test_fixed_workspace_rejects_other_thread_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        service = AgentApplicationService(
            runtime,
            store,
            SQLiteProtocolRequestStore(store.path),
            workspace=workspace,
        )
        client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
        await client.initialize()
        created = await client.create_thread(str(workspace), request_id="configured")
        assert created.workspace == str(workspace.resolve())
        if os.name == "nt":
            alias = await client.create_thread(
                str(workspace.resolve()).swapcase(), request_id="configured-case-alias"
            )
            assert Path(alias.workspace) == workspace.resolve()
        with pytest.raises(AgentSDKError) as error:
            await client.create_thread(str(other), request_id="other")
        assert error.value.code == "workspace_not_configured"
        with pytest.raises(AgentSDKError) as error:
            await client.create_thread(str(tmp_path / "missing"), request_id="missing")
        assert error.value.code == "workspace_not_configured"
        await client.close()


def test_config_diagnose_and_migrate_commands_emit_bounded_json(
    tmp_path: Path,
    config: ProductConfigV2,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _credentials(monkeypatch)
    path = write_config(tmp_path / "config.json", config)
    state = tmp_path / "diagnostic.db"
    with pytest.raises(SystemExit) as completed:
        config_main(["diagnose", "--config", str(path), "--state-database", str(state)])
    assert completed.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is True
    assert CANARY not in json.dumps(report)

    source = legacy_body(config)
    legacy = tmp_path / "legacy.json"
    legacy.write_bytes(source)
    legacy.chmod(0o600)
    import hashlib

    config_main(
        [
            "migrate",
            "--config",
            str(legacy),
            "--expected-source-sha256",
            hashlib.sha256(source).hexdigest(),
            "--state-database",
            str(tmp_path / "migration.db"),
        ]
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["changed"] is True
    assert CANARY not in json.dumps(receipt)


def test_config_cli_redacts_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_path: str) -> None:
        raise RuntimeError(CANARY)

    monkeypatch.setattr("harnessix.product_config.cli.load_product_config", fail)
    with pytest.raises(SystemExit) as failure:
        config_main(["diagnose", "--config", "/missing"])
    assert failure.value.code == 2
    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert error == {
        "code": "product_internal_failure",
        "message": "产品配置操作发生内部错误",
        "retryable": False,
    }
    assert CANARY not in captured.err and captured.out == ""
