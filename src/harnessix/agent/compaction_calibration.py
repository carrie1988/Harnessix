"""按上一次模型响应的真实输入Token校准压缩点与输入上限（ADR 0132）。

压缩点、目标与输入上限都以估算器口径（UTF-8字节）表示，声明模型窗口时按固定的每Token 3字节换算。
真实比例随内容与线上格式变化（Multilingual正式评测实测3.7～6.3），固定比例使“窗口一半”在真实
窗口的30%左右就触发。这里用最近一次已完成主请求的事实校准：该步Context检查记录的估算字节数，
与Provider为同一请求报告的真实输入Token数。

- 只读已持久化的事实（模型尝试与Context检查），同一Session重算结果相同；
- 没有声明窗口、没有Context Planner或还没有完整用量时返回原配置，行为不变；
- 校准只放宽：触发点、目标与输入上限都不低于固定比例换算出的数值；
- 输入上限不超过CompactionTokenWindow.max_input_bytes（出站请求容量），触发点另在输入上限之下
  留出一次退避的余量，所以请求大小始终受出站请求容量约束。

非职责：不决定是否压缩（见model_history_runtime），不修改压缩计划、摘要来源或其校验。
"""

from __future__ import annotations

from dataclasses import dataclass

from harnessix.agent.models import Thread
from harnessix.context.compaction_runtime_contracts import (
    CompactionRuntimeConfig,
    CompactionTokenWindow,
)

# 主动压缩被跳过（规划不成立或摘要被截断）后，历史再增长四分之一才重试。校准后的触发点留出同样的
# 余量：重试发生时请求仍在输入上限之内，一次失败的压缩不会让Turn因超出输入上限而终止。
SKIPPED_COMPACTION_GROWTH = (5, 4)


@dataclass(frozen=True, slots=True)
class InputObservation:
    """最近一次已完成主请求：估算器口径的输入总量、其中的历史部分，以及真实输入Token。"""

    estimated_input_bytes: int
    history_bytes: int
    input_tokens: int

    @property
    def fixed_bytes(self) -> int:
        """工具定义与指令：每个请求都带、不随历史压缩变化的部分。"""
        return self.estimated_input_bytes - self.history_bytes

    def input_limit(self, window: CompactionTokenWindow) -> int:
        """窗口扣除输出预留与封装开销后的输入上限，按观测比例换算，封顶于出站请求容量。"""
        return min(self._bytes(window.input_limit_tokens), window.max_input_bytes)

    def history_share(self, window: CompactionTokenWindow, divisor: int) -> int:
        """整个请求的真实输入占窗口1/divisor时的历史字节数（已扣除工具定义与指令）。"""
        return self._bytes(window.window_tokens) // divisor - self.fixed_bytes

    def _bytes(self, tokens: int) -> int:
        return tokens * self.estimated_input_bytes // self.input_tokens


def observed_input(thread: Thread) -> InputObservation | None:
    """Thread内最近一次带完整用量的已完成主请求；跨Turn查找，新Turn的首个请求也能校准。"""
    for turn in reversed(thread.turns):
        inspections = {record.model_step: record for record in turn.context_inspections}
        for attempt in reversed(turn.model_attempts):
            inspection = inspections.get(attempt.step)
            if attempt.status == "completed" and attempt.usage.input_tokens and inspection:
                return InputObservation(
                    estimated_input_bytes=inspection.estimated_input_tokens,
                    history_bytes=inspection.history_tokens,
                    input_tokens=attempt.usage.input_tokens,
                )
    return None


def calibrated_compaction(
    config: CompactionRuntimeConfig, window: CompactionTokenWindow | None, thread: Thread
) -> CompactionRuntimeConfig:
    """按真实用量校准后的压缩配置：触发点与目标对应真实窗口的同一比例，其余字段不变。

    触发点不超过“输入上限扣除工具定义与指令后的五分之四”：出站请求容量先于真实窗口的一半
    到达时，触发点停在容量允许的位置；目标同步保持与触发点的原有比例，压缩间隔不被压短。
    """
    observation = observed_input(thread) if window is not None else None
    if window is None or observation is None:
        return config
    limit = observation.input_limit(window)
    grown, base = SKIPPED_COMPACTION_GROWTH
    ceiling = (limit - observation.fixed_bytes) * base // grown
    trigger = max(
        config.trigger_history_tokens,
        min(observation.history_share(window, window.trigger_divisor), ceiling),
    )
    policy = config.policy
    target = max(
        policy.target_history_tokens,
        min(
            observation.history_share(window, window.target_divisor),
            trigger * window.trigger_divisor // window.target_divisor,
        ),
    )
    return config.model_copy(
        update={
            "trigger_history_tokens": trigger,
            "policy": policy.model_copy(
                update={
                    "target_history_tokens": target,
                    "max_summary_input_tokens": max(policy.max_summary_input_tokens, limit),
                }
            ),
        }
    )


def calibrated_limit(window: CompactionTokenWindow | None, thread: Thread) -> int | None:
    """按真实用量校准的输入上限（估算器口径）；没有窗口或观测时为None，沿用Planner固定上限。"""
    observation = observed_input(thread) if window is not None else None
    if window is None or observation is None:
        return None
    return observation.input_limit(window)
