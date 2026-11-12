"""启动时对旧配置的提示：模型核实过思考强度、配置里却没有设置时，建议重新configure。"""

from __future__ import annotations

import json
from pathlib import Path

from harnessix.product_config.codec import load_product_config
from harnessix.product_ui.config_hints import UNBOUNDED_THINKING_HINT, thinking_config_hint
from harnessix.product_ui.configure_cli import configure_main


def _configure(path: Path, *arguments: str) -> Path:
    configure_main(["--config", str(path), "--non-interactive", *arguments])
    return path


def _without(path: Path, target: Path, *, profile_fields=(), provider_fields=()) -> Path:
    """去掉若干字段，得到旧版本向导会写出的配置。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    for field in profile_fields:
        data["profiles"][0].pop(field, None)
    for field in provider_fields:
        data["providers"][0].pop(field, None)
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    target.chmod(0o600)
    load_product_config(target)
    return target


def test_new_configurations_get_no_hint(tmp_path, capsys):
    for name, arguments in (
        ("default", ("--vendor", "bailian")),
        ("low", ("--vendor", "bailian", "--thinking-effort", "low")),
        ("budget", ("--vendor", "bailian", "--thinking-budget", "4000")),
        ("off", ("--vendor", "bailian", "--thinking", "off")),
        ("deepseek", ("--vendor", "deepseek")),
    ):
        path = _configure(tmp_path / f"{name}.json", *arguments)
        assert thinking_config_hint(path, None) is None, name
    capsys.readouterr()


def test_configurations_written_before_intensity_tiers_get_the_hint(tmp_path, capsys):
    current = _configure(tmp_path / "current.json", "--vendor", "bailian")
    capsys.readouterr()
    # 0.10.0b19～b22：有思考协议、没有强度。
    b19 = _without(current, tmp_path / "b19.json", profile_fields=("thinking_effort",))
    assert thinking_config_hint(b19, None) == UNBOUNDED_THINKING_HINT
    # 更早：连思考协议都没有，服务端默认每一步都按最强档思考（产品负责人本机的配置就是这样）。
    older = _without(
        current,
        tmp_path / "older.json",
        profile_fields=("thinking_effort",),
        provider_fields=("thinking_protocol",),
    )
    assert thinking_config_hint(older, None) == UNBOUNDED_THINKING_HINT


def test_unreadable_configuration_gives_no_hint(tmp_path):
    assert thinking_config_hint(tmp_path / "missing.json", None) is None


async def test_an_empty_session_shows_the_hint_under_the_input_prompt(tmp_path):
    """旧配置的用户在输入第一条任务之前就能看到建议；有了对话内容后提示自然让位。"""
    from textual.widgets import Input, Static

    from harnessix.agent.runtime import AgentRuntime
    from harnessix.app_server.server import AgentProtocolServer
    from harnessix.app_server.service import AgentApplicationService
    from harnessix.models.scripted import ScriptedProvider
    from harnessix.product_ui.app import ProductApp
    from harnessix.product_ui.controller import ProductController, StartRequest
    from harnessix.product_ui.session import RecoverableAgentSession
    from harnessix.product_ui.state_store import ClientStateStore
    from harnessix.product_ui.transcript_pane import EMPTY_HINT
    from harnessix.protocol.requests import SQLiteProtocolRequestStore
    from harnessix.sdk import InProcessAgentTransport
    from harnessix.session.sqlite import SQLiteSessionStore
    from tests.agent.helpers import RecordingTools, answer
    from tests.product_ui.test_app_interactions import _turn_status, _wait_until

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    requests = SQLiteProtocolRequestStore(sessions.path)
    async with AgentRuntime(
        sessions, ScriptedProvider([answer("好的")]), RecordingTools()
    ) as runtime:

        def transport() -> InProcessAgentTransport:
            service = AgentApplicationService(
                runtime, sessions, requests, workspace=workspace.resolve()
            )
            return InProcessAgentTransport(AgentProtocolServer(service))

        with ClientStateStore(
            tmp_path / "client", workspace_identity=str(workspace.resolve())
        ) as store:
            controller = ProductController(RecoverableAgentSession(store, transport))
            request = StartRequest(workspace=str(workspace), config_hint=UNBOUNDED_THINKING_HINT)
            app = ProductApp(controller, request)
            async with app.run_test(size=(120, 30)) as pilot:
                composer = app.query_one("#composer", Input)
                await _wait_until(lambda: not composer.disabled)
                live = app.query_one("#live", Static)
                shown = str(live.content)
                assert EMPTY_HINT in shown and UNBOUNDED_THINKING_HINT in shown
                await pilot.press("h", "i", "enter")
                await _wait_until(lambda: _turn_status(controller) == "completed")
                assert UNBOUNDED_THINKING_HINT not in str(live.content)
