from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from secrets import token_hex
from tempfile import TemporaryDirectory

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import FailureCategory, KernelError
from harnessix.agent.models import (
    ApprovalRequestContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.ports import NoTools
from harnessix.agent.reducer import get_turn, replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import (
    ApprovalDecision,
    ApprovalOutcome,
    EffectClass,
    RiskLevel,
    ToolDescriptor,
)
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ModelProvider, ResponseFailed
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.smoke.contracts import Execution, SmokeConfig, SmokeReport

ProviderFactory = Callable[[SmokeConfig], AbstractAsyncContextManager[ModelProvider]]
TEXT_MARKER = "HARNESSIX_SMOKE_OK"


def _sdk_provider(config: SmokeConfig) -> AbstractAsyncContextManager[ModelProvider]:
    sdk_config = config.provider_config()
    if isinstance(sdk_config, OpenAIChatConfig):
        from harnessix.models.openai_chat import OpenAIChatProvider

        return OpenAIChatProvider(sdk_config)
    from harnessix.models.anthropic import AnthropicProvider

    return AnthropicProvider(sdk_config)


class _MarkerTool:
    def __init__(self, *, approval: bool) -> None:
        self.marker = "HARNESSIX_" + token_hex(16)
        self.approval = approval
        self.calls = 0

    def definitions(self) -> tuple[ToolDescriptor, ...]:
        return (
            ToolDescriptor(
                name="smoke.read_marker",
                version="1",
                description="读取本次内存验收标记；无参数，不访问文件或网络，仅允许调用一次",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                effect_class=EffectClass.READ_ONLY,
                risk_level=RiskLevel.LOW,
                requires_idempotency=False,
                requires_approval=self.approval,
                supports_reconciliation=False,
            ),
        )

    async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent:
        cancel.checkpoint()
        self.calls += 1
        if self.calls != 1 or call.tool != "smoke.read_marker" or call.arguments:
            return ToolResultContent(call_id=call.call_id, outcome="failed")
        return ToolResultContent(
            call_id=call.call_id, outcome="succeeded", output={"marker": self.marker}
        )


def _report(
    config: SmokeConfig,
    execution: Execution,
    turn: Turn,
    tool: _MarkerTool,
    *,
    approval_verified: bool,
    replay_verified: bool,
) -> SmokeReport:
    results = [
        i for i, item in enumerate(turn.items) if isinstance(item.content, ToolResultContent)
    ]
    final_items = turn.items[results[-1] + 1 :] if results else turn.items
    answer = "".join(
        item.content.text
        for item in final_items
        if isinstance(item.content, TextContent) and item.content.kind == "assistant_message"
    ).strip()
    expected = TEXT_MARKER if config.scenario == "text" else tool.marker
    content_verified = answer == expected
    calls_verified = tool.calls == (0 if config.scenario == "text" else 1) and all(
        item.content.outcome == "succeeded"
        for item in turn.items
        if isinstance(item.content, ToolResultContent)
    )
    passed = (
        turn.status == TurnStatus.COMPLETED
        and content_verified
        and calls_verified
        and turn.usage_is_complete
        and len(turn.model_attempts) == (1 if config.scenario == "text" else 2)
        and replay_verified
        and (config.scenario != "approval" or approval_verified)
    )
    provider_failure = None
    if turn.error is not None and turn.error.category == FailureCategory.PROVIDER:
        try:
            provider_failure = ResponseFailed.model_validate(
                {
                    "code": turn.error.code.removeprefix("provider_"),
                    "retryable": turn.error.retryable,
                }
            )
        except ValueError:
            provider_failure = ResponseFailed(code="unknown")
    return SmokeReport(
        provider=config.provider,
        scenario=config.scenario,
        execution=execution,
        reason="passed"
        if passed
        else "check_failed"
        if turn.status == TurnStatus.COMPLETED
        else "runtime_failed",
        turn_status=turn.status,
        failure_category=turn.error.category if turn.error else None,
        provider_failure=provider_failure,
        attempts_started=len(turn.model_attempts),
        known_input_tokens=turn.usage.input_tokens,
        known_output_tokens=turn.usage.output_tokens,
        usage_complete=turn.usage_is_complete,
        tool_calls=tool.calls,
        content_verified=content_verified,
        approval_restart_verified=approval_verified,
        replay_verified=replay_verified,
    )


async def _exercise(
    config: SmokeConfig, execution: Execution, provider: ModelProvider, root: Path
) -> SmokeReport:
    tool = _MarkerTool(approval=config.scenario == "approval")
    runtime_tools = NoTools() if config.scenario == "text" else tool
    prompt = (
        f"请只输出 {TEXT_MARKER}，不要添加其他内容。"
        if config.scenario == "text"
        else "请调用唯一的内存标记读取工具一次，参数为空对象；随后只输出结果中的 marker 值。"
    )
    store = SQLiteSessionStore(root / "session.db")
    async with AgentRuntime(store, provider, runtime_tools) as runtime:
        thread = await runtime.create_thread(str(root))
        turn = await runtime.run_turn(
            thread.thread_id, prompt, request_id="model-smoke", budget=config.budget()
        )
    reopened = SQLiteSessionStore(root / "session.db")
    approval_verified = False
    async with AgentRuntime(reopened, provider, runtime_tools) as runtime:
        recovered = get_turn(await reopened.get_thread(thread.thread_id), turn.turn_id)
        if config.scenario == "approval" and recovered.status == TurnStatus.WAITING_APPROVAL:
            requests = [
                i.content for i in recovered.items if isinstance(i.content, ApprovalRequestContent)
            ]
            if recovered == turn and len(requests) == 1 and tool.calls == 0:
                approval = requests[0]
                await runtime.reply_approval(
                    thread.thread_id,
                    turn.turn_id,
                    approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=ApprovalDecision(
                        outcome=ApprovalOutcome.APPROVED, actor="受控内存夹具验收"
                    ),
                )
                approval_verified = tool.calls == 0
                recovered = await runtime.resume_turn(thread.thread_id, turn.turn_id)
        snapshot = await reopened.get_thread(thread.thread_id)
        replay_verified = replay(await reopened.events(thread.thread_id)) == snapshot
        return _report(
            config,
            execution,
            recovered,
            tool,
            approval_verified=approval_verified,
            replay_verified=replay_verified,
        )


async def run_smoke(
    config: SmokeConfig,
    *,
    allow_network: bool = False,
    provider_factory: ProviderFactory | None = None,
) -> SmokeReport:
    """显式启用才创建 Provider；注入工厂由宿主负责，不能据此断言离线。"""
    execution: Execution = "injected" if provider_factory is not None else "sdk_default"
    if allow_network is not True:
        return SmokeReport(
            reason="network_not_enabled", execution=execution, attempts_started=0, tool_calls=0
        )
    try:
        checked = SmokeConfig.model_validate_json(config.model_dump_json())
    except ValueError:
        return SmokeReport(reason="configuration_invalid", execution=execution)
    try:
        context = (provider_factory or _sdk_provider)(checked)
    except ImportError:
        return SmokeReport(reason="dependency_missing", execution=execution)
    except ValueError:
        return SmokeReport(reason="configuration_invalid", execution=execution)
    except Exception:
        return SmokeReport(reason="internal_error", execution=execution)
    try:
        async with context as provider:
            with TemporaryDirectory(prefix="harnessix-smoke-") as directory:
                report = await _exercise(checked, execution, provider, Path(directory))
        return report
    except KernelError as error:
        return SmokeReport(
            reason="runtime_failed",
            execution=execution,
            failure_category=error.to_failure().category,
        )
    except Exception:
        # 不捕获 CancelledError / KeyboardInterrupt；上下文先完成资源清理。
        return SmokeReport(reason="internal_error", execution=execution)
