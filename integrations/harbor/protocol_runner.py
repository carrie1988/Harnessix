"""Harbor侧Agent Protocol客户端；不实现模型循环、自动审批或费用结算。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.protocol.contracts import PublicBudget, ThreadView, TurnView
from harnessix.sdk.agent_client import AgentClient, AgentSDKError, SubprocessAgentTransport

_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


async def _completed_evidence(client: AgentClient, thread_id: UUID, turn_id: UUID) -> dict:
    """完整重放本次Turn，仅保留完成回答摘要和成功工具计数，不输出原文或参数。"""
    cursor = 0
    succeeded = set()
    answer_digest = None
    async with asyncio.timeout(3):
        while True:
            page = await client.replay_events(
                thread_id,
                after_cursor=cursor,
                limit=min(64, client.initialized.limits.max_replay_events),
            )
            for event in page.events:
                if event.turn_id != turn_id or event.data.type != "item_finished":
                    continue
                item = event.data.item
                content = item.content
                if (
                    item.status == "completed"
                    and content.kind == "tool_result"
                    and content.outcome == "succeeded"
                ):
                    succeeded.add(item.item_id)
                if item.status == "completed" and content.kind == "assistant_message":
                    answer_digest = hashlib.sha256(content.text.encode()).hexdigest()
            if not page.has_more:
                return {
                    "protocol_evidence_complete": True,
                    "tool_results_succeeded": len(succeeded),
                    "final_assistant_sha256": answer_digest,
                }
            if page.scanned_through <= cursor:
                raise AgentSDKError("replay_stalled", "协议重放未推进")
            cursor = page.scanned_through


async def drive_protocol_trial(
    client: AgentClient,
    *,
    workspace: str,
    instruction: str,
    budget: PublicBudget,
    trial_id: UUID,
) -> dict:
    """驱动一次显式试次；遇到人工交互即取消，超时清理有界且不重试任务。"""

    thread: ThreadView | None = None
    turn: TurnView | None = None
    reason = "protocol_error"
    turn_terminal_confirmed = False
    cancelled = False
    try:
        async with asyncio.timeout(budget.timeout_seconds):
            await client.initialize()
            thread = await client.create_thread(workspace, request_id=f"harbor-{trial_id}-thread")
            turn = await client.start_turn(
                thread.thread_id,
                instruction,
                request_id=f"harbor-{trial_id}-turn",
                budget=budget,
            )
            cursor = 0
            while True:
                thread = await client.get_thread(thread.thread_id)
                turn = thread.latest_turn
                if turn is None:
                    raise AgentSDKError("missing_turn", "协议未返回本次Turn")
                if turn.status in _TERMINAL:
                    reason = turn.status
                    turn_terminal_confirmed = True
                    break
                if turn.status in {"waiting_approval", "waiting_input"}:
                    reason = "intervention_required"
                    break
                limit = min(64, client.initialized.limits.max_replay_events)
                page = await client.next_events(
                    thread.thread_id, after_cursor=cursor, wait_ms=1000, limit=limit
                )
                cursor = max(cursor, page.replay.scanned_through)
    except TimeoutError:
        reason = "deadline_exceeded"
    except AgentSDKError:
        reason = "protocol_error"
    except asyncio.CancelledError:
        cancelled = True
    finally:
        if thread is not None and (turn is None or turn.status not in _TERMINAL):
            try:
                async with asyncio.timeout(3):
                    current = await client.get_thread(thread.thread_id)
                    if current.latest_turn is not None:
                        turn = await client.cancel_turn(
                            thread.thread_id,
                            current.latest_turn.turn_id,
                            request_id=f"harbor-{trial_id}-cancel",
                        )
                        turn_terminal_confirmed = turn.status in _TERMINAL
            except (AgentSDKError, TimeoutError):
                pass
    if cancelled:
        raise asyncio.CancelledError
    evidence = {
        "protocol_evidence_complete": False,
        "tool_results_succeeded": None,
        "final_assistant_sha256": None,
    }
    if thread is not None and turn is not None and turn_terminal_confirmed:
        try:
            evidence = await _completed_evidence(client, thread.thread_id, turn.turn_id)
        except (AgentSDKError, TimeoutError):
            pass
    return {
        "spec_version": "harnessix.harbor-protocol-result/v1",
        "trial_id": str(trial_id),
        "reason": reason,
        "thread_id": str(thread.thread_id) if thread else None,
        "turn_id": str(turn.turn_id) if turn else None,
        "turn_status": turn.status if turn else None,
        "failure_category": turn.error.category if turn and turn.error else None,
        "model_steps": turn.model_steps if turn else None,
        "input_tokens": turn.usage.input_tokens if turn else None,
        "output_tokens": turn.usage.output_tokens if turn else None,
        "usage_source": "agent_protocol_turn",
        "usage_completeness": "unknown",
        "cost_completeness": "unknown",
        "turn_terminal_confirmed": turn_terminal_confirmed,
        **evidence,
    }


def _read_instruction(path: Path) -> str:
    with path.open("rb") as source:
        body = source.read(1_048_577)
    if len(body) > 1_048_576:
        raise ValueError("instruction too large")
    return body.decode("utf-8")


async def _run(arguments: argparse.Namespace, instruction: str) -> dict:
    budget = PublicBudget(
        max_steps=arguments.max_steps,
        max_tokens=arguments.max_tokens,
        timeout_seconds=arguments.timeout_seconds,
        max_output_chars=65_536,
        max_tool_calls_per_step=32,
    )
    command = [
        sys.executable,
        "-m",
        "harnessix",
        "agent-server",
        "--config",
        arguments.server_config,
        "--workspace",
        arguments.workspace,
        "--state-directory",
        arguments.state_directory,
    ]
    if arguments.action_config is not None:
        command.extend(["--action-config", arguments.action_config])
    transport = SubprocessAgentTransport(
        command, graceful_shutdown_timeout_seconds=2.0, terminate_timeout_seconds=2.0
    )
    async with AgentClient(transport) as client:
        return await drive_protocol_trial(
            client,
            workspace=arguments.workspace,
            instruction=instruction,
            budget=budget,
            trial_id=UUID(arguments.trial_id),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instruction-file", required=True)
    parser.add_argument("--server-config", required=True)
    parser.add_argument("--action-config")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--state-directory", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--max-steps", type=int, default=16)
    parser.add_argument("--max-tokens", type=int, default=50_000)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    arguments = parser.parse_args()
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        instruction = _read_instruction(Path(arguments.instruction_file))
        state = Path(arguments.state_directory)
        if state.exists() and any(state.iterdir()):
            raise ValueError("已有状态必须经显式恢复流程处理")
        # 在启动App Server前独占占位，重入不能先重复调用模型再发现结果文件已存在。
        descriptor = os.open(arguments.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            result = asyncio.run(_run(arguments, instruction))
            json.dump(result, output)
        raise SystemExit(0 if result["reason"] == "completed" else 1)
    except (AgentSDKError, OSError, ValueError):
        raise SystemExit(1) from None
    finally:
        logging.disable(previous_logging)


if __name__ == "__main__":
    main()
