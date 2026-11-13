"""F1帮助：任务以错误结束后显示该错误的原因和处理办法，而不是通用使用说明（0.10.0b7）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from harnessix.product_ui.controller import ControllerPhase, ProductControllerState
from harnessix.product_ui.error_help import current_error_help
from harnessix.product_ui.interaction_presenter import InteractionPresenter
from harnessix.product_ui.interaction_screens import HelpScreen
from harnessix.product_ui.projection import ProductViewState, TurnProjection
from harnessix.protocol.contracts import PublicFailure
from tests.product_ui.test_tui_usability import _thread


def test_failed_turn_shows_its_own_help() -> None:
    item = current_error_help(None, "failed", "provider_authentication")
    assert item is not None and item.title == "模型服务拒绝了 API Key"
    assert "doctor --probe-model" in item.action
    unknown = current_error_help(None, "interrupted", "uncertain_effect")
    assert unknown is not None and unknown.title == "操作结果无法确认"


def test_context_exhaustion_says_how_to_go_on() -> None:
    """Beta Issue #3：上下文耗尽后只有一行原始报错，没有说该新建会话、窗口太小该怎么改。"""
    item = current_error_help(None, "failed", "context_budget_exceeded")
    assert item is not None and item.title == "会话内容放不进上下文窗口"
    assert "--context-window-tokens" in item.action and "选中这个会话" in item.action
    assert "Ctrl+N" in item.action
    assert "保留" in item.impact


def test_malformed_model_output_says_that_continuing_works() -> None:
    """Beta Issue #1：模型返回的内容不合规时只显示“Provider 返回结构化失败”，没有恢复办法。"""
    item = current_error_help(None, "failed", "provider_invalid_provider_output")
    assert item is not None and item.title == "模型返回的内容不完整或格式不对"
    assert "继续" in item.action


def test_a_slow_history_check_says_how_to_go_on() -> None:
    """Beta Issue #8：context_artifact_timeout落到通用说明，没有说可以继续或新建会话。"""
    item = current_error_help(None, "failed", "context_artifact_timeout")
    assert item is not None and item.title == "核对会话记录超时"
    assert "继续" in item.action and "Ctrl+N" in item.action


def test_unknown_turn_failure_is_generic_and_does_not_echo_the_code() -> None:
    item = current_error_help(None, "failed", "provider-secret-/private/path")
    assert item is not None and item.code == "turn_failed"
    assert "provider-secret" not in repr(item)


def test_connection_notice_comes_first_and_no_error_means_usage_guide() -> None:
    notice = current_error_help("server_closed", "failed", "provider_authentication")
    assert notice is not None and notice.code == "connection_failure"
    for status in ("completed", "cancelled", "calling_model", None):
        assert current_error_help(None, status, None) is None
    assert current_error_help(None, "completed", "provider_authentication") is None


async def test_f1_after_a_wrong_api_key_opens_the_api_key_help() -> None:
    # 0.10.0b5在macOS实测：API Key错误后按F1仍显示通用使用说明，因为只看连接通知。
    turn = TurnProjection(
        turn_id=uuid4(),
        request_id="r",
        status="failed",
        budget=None,
        usage=None,
        model_step=1,
        error=PublicFailure(
            code="provider_authentication", message="Provider 返回结构化失败", category="provider"
        ),
    )
    view = ProductViewState(thread=_thread(), durable_cursor=1, current_turn=turn)
    state = ProductControllerState(phase=ControllerPhase.READY, thread_view=view)
    shown: list[HelpScreen] = []

    async def push_screen_wait(screen: HelpScreen) -> None:
        shown.append(screen)

    app: Any = SimpleNamespace(push_screen_wait=push_screen_wait)
    controller: Any = SimpleNamespace(state=state)
    await InteractionPresenter(app, controller).help()

    assert shown[0].help_item is not None
    assert shown[0].help_item.title == "模型服务拒绝了 API Key"
