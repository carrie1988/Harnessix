"""本机stdio MCP的产品接入（ADR 0126）：配置、信任、未信任不启动、逐次审批、会话结束回收进程。"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, EffectClass, RiskLevel
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config.action_composition import build_fixed_product_action_environment
from harnessix.product_config.mcp_composition import open_product_mcp
from harnessix.product_config.mcp_config import load_mcp_config, resolve_mcp_server
from harnessix.product_config.mcp_status import mcp_server_statuses, trusted_mcp_servers
from harnessix.product_config.mcp_trust import mcp_trust_path, trust_mcp_server
from harnessix.product_ui.mcp_cli import mcp_main
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from tests.agent.helpers import answer
from tests.agent.test_trusted_action_runtime import approval

FIXTURE = Path(__file__).parents[1] / "mcp/fixtures/host_stdio_server.py"


def _write_config(home: Path, servers: dict[str, object]) -> None:
    path = home / ".harnessix/mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _fixture_server(pid_file: Path, **env: str) -> dict[str, object]:
    return {"command": sys.executable, "args": [str(FIXTURE), str(pid_file)], "env": env}


def test_config_accepts_the_common_format_and_isolates_bad_entries(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "good": {"command": "python3", "args": ["-m", "server"], "env": {"A": "1"}},
            "remote": {"url": "https://mcp.example/sse"},
            "extra": {"command": "x", "cwd": "/tmp"},
            "9bad": {"command": "x"},
            "typed": {"type": "stdio", "command": "x"},
        },
    )
    report = load_mcp_config(tmp_path / ".harnessix/mcp.json")
    assert [item.server_id for item in report.servers] == ["good", "typed"]
    assert dict(report.problems) == {
        "9bad": "mcp_server_id_invalid",
        "extra": "mcp_server_field_unsupported",
        "remote": "mcp_remote_unsupported",
    }
    assert load_mcp_config(tmp_path / "missing.json").servers == ()
    (tmp_path / "dup.json").write_text('{"mcpServers": {}, "mcpServers": {}}')
    assert load_mcp_config(tmp_path / "dup.json").file_problem == "mcp_config_invalid_json"


def test_env_references_are_expanded_at_launch_and_kept_out_of_the_fingerprint(
    tmp_path: Path,
) -> None:
    _write_config(
        tmp_path,
        {"s": {"command": sys.executable, "args": ["${ROOT}/x"], "env": {"TOKEN": "${SECRET}"}}},
    )
    definition = load_mcp_config(tmp_path / ".harnessix/mcp.json").servers[0]
    first, problem = resolve_mcp_server(definition, {"SECRET": "value-1", "ROOT": "/r"})
    second, _ = resolve_mcp_server(definition, {"SECRET": "value-2", "ROOT": "/r"})
    assert problem is None and first is not None and second is not None
    assert first.arguments == ("/r/x",) and first.environment == {"TOKEN": "value-1"}
    assert first.secret_values == ("value-1",)
    assert first.fingerprint == second.fingerprint
    assert "value-1" not in first.fingerprint
    missing, code = resolve_mcp_server(definition, {"ROOT": "/r"})
    assert missing is None and code == "mcp_env_missing:SECRET"


@pytest.mark.parametrize(
    ("command", "problem"),
    [("./server", "mcp_command_not_found"), ("no-such-command-xyz", "mcp_command_not_found")],
)
def test_relative_or_missing_commands_are_unavailable(
    tmp_path: Path, command: str, problem: str
) -> None:
    _write_config(tmp_path, {"s": {"command": command}})
    definition = load_mcp_config(tmp_path / ".harnessix/mcp.json").servers[0]
    assert resolve_mcp_server(definition, {"PATH": "/usr/bin"}) == (None, problem)


def test_trust_is_bound_to_the_definition(tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    _write_config(tmp_path, {"fixture": _fixture_server(pid)})
    environ = {"PATH": os.environ.get("PATH", "")}
    assert mcp_server_statuses(environ, home=tmp_path).servers[0].state == "untrusted"
    assert trusted_mcp_servers(environ, home=tmp_path) == ()

    resolved = mcp_server_statuses(environ, home=tmp_path).servers[0].resolved
    assert resolved is not None
    trust_mcp_server(mcp_trust_path(tmp_path), "fixture", resolved.fingerprint)
    assert [item.definition.server_id for item in trusted_mcp_servers(environ, home=tmp_path)] == [
        "fixture"
    ]

    _write_config(tmp_path, {"fixture": {**_fixture_server(pid), "args": [str(FIXTURE), "other"]}})
    assert mcp_server_statuses(environ, home=tmp_path).servers[0].state == "changed"
    assert trusted_mcp_servers(environ, home=tmp_path) == ()


def test_cli_lists_trusts_and_untrusts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_config(tmp_path, {"fixture": _fixture_server(tmp_path / "pid")})
    with pytest.raises(SystemExit) as listed:
        mcp_main(["list"])
    assert listed.value.code == 0 and "fixture  [未信任]" in capsys.readouterr().out
    with pytest.raises(SystemExit) as trusted:
        mcp_main(["trust", "fixture"])
    out = capsys.readouterr().out
    assert trusted.value.code == 0 and "以你的权限在本机运行" in out and "已信任" in out
    with pytest.raises(SystemExit):
        mcp_main(["list"])
    assert "fixture  [已信任]" in capsys.readouterr().out
    mcp_main(["untrust", "fixture"])
    assert "已撤销信任" in capsys.readouterr().out
    with pytest.raises(SystemExit) as unknown:
        mcp_main(["trust", "nope"])
    assert unknown.value.code == 2


def _process_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(os.name != "posix", reason="进程存活检查使用POSIX信号0")
async def test_host_server_tools_wait_for_approval_and_run_after_it(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    pid_file = tmp_path / "server.pid"
    _write_config(tmp_path, {"fixture": _fixture_server(pid_file, FIXTURE_TOKEN="${TOKEN}")})
    environ = {"PATH": os.environ.get("PATH", ""), "TOKEN": "token-from-user-env"}
    resolved = mcp_server_statuses(environ, home=tmp_path).servers[0].resolved
    assert resolved is not None
    plans = SQLiteExecutionPlanStore(tmp_path / "state/plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "state/audit.db")
    router = TrustedActionRouter(plans=plans, audit=audit, workspace_root=lambda _: workspace)
    environment = build_fixed_product_action_environment(workspace)
    step = [
        ResponseStarted(response_id="mcp-response"),
        ToolCallCompleted(
            call_id="mcp-call", tool="mcp__fixture__write_marker", arguments={"name": "marker"}
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]
    async with open_product_mcp(router, environment, workspace, (resolved,)) as mcp:
        assert mcp is not None and mcp.failures == () and len(mcp.gateways) == 1
        gateway = mcp.gateways[0]
        descriptors = {item.name: item for item in gateway.definitions()}
        assert set(descriptors) == {"mcp__fixture__echo", "mcp__fixture__write_marker"}
        # Server自称只读也不能免审批。
        echo = descriptors["mcp__fixture__echo"]
        assert echo.requires_approval and echo.effect_class is EffectClass.IDEMPOTENT_WRITE
        assert echo.risk_level is RiskLevel.HIGH and echo.description.startswith("[MCP fixture]")

        provider = ScriptedProvider([step, answer("done")])
        store = SQLiteSessionStore(tmp_path / "session.db")
        async with AgentRuntime(store, provider, trusted_actions=gateway) as runtime:
            thread = await runtime.create_thread(str(workspace))
            turn = await runtime.run_turn(thread.thread_id, "go", request_id="mcp")
            assert turn.status is TurnStatus.WAITING_APPROVAL
            assert not (workspace / "marker").exists()
            request = approval(turn)
            await runtime.reply_approval(
                thread.thread_id,
                turn.turn_id,
                request.approval_id,
                fingerprint=request.request_fingerprint,
                decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="user"),
            )
            turn = await runtime.resume_turn(thread.thread_id, turn.turn_id)
        assert turn.status is TurnStatus.COMPLETED
        results = [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]
        assert [result.outcome for result in results] == ["succeeded"]
        assert (workspace / "marker").read_text() == "token-from-user-env"
        assert "token-from-user-env" not in json.dumps(results[0].model_dump(mode="json"))
        pid = int(pid_file.read_text())
    assert _process_gone(pid)
    audit.close()
    plans.close()


async def test_unreachable_server_does_not_block_the_session(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    script = tmp_path / "exit.py"
    script.write_text("import sys\nsys.exit(3)\n")
    _write_config(tmp_path, {"broken": {"command": sys.executable, "args": [str(script)]}})
    resolved = mcp_server_statuses({}, home=tmp_path).servers[0].resolved
    assert resolved is not None
    plans = SQLiteExecutionPlanStore(tmp_path / "state/plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "state/audit.db")
    router = TrustedActionRouter(plans=plans, audit=audit, workspace_root=lambda _: workspace)
    environment = build_fixed_product_action_environment(workspace)
    async with open_product_mcp(router, environment, workspace, (resolved,)) as mcp:
        assert mcp is not None and mcp.gateways == ()
        assert [server for server, _ in mcp.failures] == ["broken"]
    assert router.bindings(source="mcp") == ()
    audit.close()
    plans.close()


def test_doctor_explains_each_server_and_stays_silent_without_config(tmp_path: Path) -> None:
    from harnessix.product_config.mcp_status import mcp_doctor_lines

    assert mcp_doctor_lines({}, home=tmp_path) == []
    _write_config(
        tmp_path,
        {
            "fixture": _fixture_server(tmp_path / "pid"),
            "remote": {"url": "https://mcp.example/sse"},
        },
    )
    lines = mcp_doctor_lines({"PATH": os.environ.get("PATH", "")}, home=tmp_path)
    assert lines == [
        "- MCP fixture：未信任，不会启动；运行 harnessix mcp trust fixture",
        "- MCP remote：不可用（暂不支持远端 MCP（url 或非 stdio 类型））",
    ]
