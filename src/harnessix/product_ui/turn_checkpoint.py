"""终端界面的运行时间检查点：任务实际运行每满一段时间，问用户是否继续（ADR 0148）。

内核给每个Turn一个固定的墙钟预算，到时把Turn判为失败，等待审批的时间也算在内。交互使用时
用户就在屏幕前：界面自己累计任务实际运行的时间（等待审批、等待回答不算），满60分钟时询问；
5分钟内没有回应才暂停本轮，避免没人看着时一直运行。计时只在界面进程内，不写入会话记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal
from uuid import UUID

# 收尾说明与宿主的判定共用一份文本（Beta Issue #4）。
from harnessix.product_config.wrap_up import WRAP_UP_PROMPT as WRAP_UP_PROMPT

CHECKPOINT_SECONDS: Final = 3600.0
UNANSWERED_PAUSE_SECONDS: Final = 300.0
# 交互模式下内核的墙钟预算放到合同允许的最大值：到时询问由本模块负责，步数上限照旧。
INTERACTIVE_TIMEOUT_SECONDS: Final = 86_400.0
# Agent自己在运行的状态；等待用户审批、回答的状态不计入运行时间。
_RUNNING_STATES: Final = frozenset(
    {"accepted", "preparing_context", "calling_model", "executing_tools", "finalizing"}
)
CONTINUE_PROMPT: Final = "继续完成上一轮没有做完的任务。"
CheckpointChoice = Literal["continue", "wrap_up", "stop"]


@dataclass(slots=True)
class ActiveTimeMeter:
    """累计界面观察到的任务实际运行时间；两次观察之间只有前一次处于运行状态才计时。"""

    limit: float = CHECKPOINT_SECONDS
    active: float = 0.0
    _turn_id: UUID | None = None
    _running_since: float | None = None

    def observe(self, turn_id: UUID | None, status: str | None, now: float) -> None:
        if turn_id is not None and turn_id != self._turn_id:
            # 新的一轮（用户发了新任务，或暂停后继续）：重新计时。
            self._turn_id, self.active, self._running_since = turn_id, 0.0, None
        if self._running_since is not None:
            self.active += max(0.0, now - self._running_since)
        self._running_since = now if status in _RUNNING_STATES else None

    @property
    def due(self) -> bool:
        return self.active >= self.limit

    def restart(self) -> None:
        """用户已经回答了这次询问：同一轮接着运行时重新计时。"""
        self.active = 0.0


def checkpoint_minutes(seconds: float) -> int:
    return max(1, round(seconds / 60))
