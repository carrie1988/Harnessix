"""模型请求进行中的生成阶段与用量估算：只用于显示，请求结束后以服务商报回的用量为准。

服务商在请求结束时才报回用量；一次请求可能持续几分钟，其间Token与费用一直不变，界面看起来像卡住。
这里从生成进度流（按类别累计的字节数，见agent/generation_progress.py）估算本次请求已经生成了多少，
并用上一次请求的用量估算本次请求的输入。估算按字节数换算，与实际值有偏差，界面一律标明“约”。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from harnessix.agent.generation_progress import latest_counts
from harnessix.product_ui.projection import ProductViewState

# 与上下文预算相同的换算口径：约3个UTF-8字节折合1个Token（中文一字约1个，英文与代码约4个字符1个）。
UTF8_BYTES_PER_TOKEN: Final = 3
_PATCH_TOOL: Final = "apply_patch_batch"
GenerationPhase = Literal["thinking", "editing", "calling", "replying"]
_PHASE_LABELS: Final[dict[GenerationPhase, str]] = {
    "thinking": "思考中",
    "editing": "正在生成修改内容",
    "calling": "正在生成操作内容",
    "replying": "生成回复中",
}


@dataclass(frozen=True, slots=True)
class InFlightRequest:
    """正在进行的一次模型请求：所处阶段，以及估算的已生成Token数与输入Token数。"""

    phase: GenerationPhase
    output_tokens: int
    input_tokens: int

    @property
    def phase_label(self) -> str:
        return _PHASE_LABELS[self.phase]

    @property
    def label(self) -> str:
        """运行提示里的文案，例如“正在生成修改内容 · 约 3,400 tokens”。"""

        return f"{self.phase_label} · 约 {approximate(self.output_tokens):,} tokens"

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def in_flight_request(view: ProductViewState | None) -> InFlightRequest | None:
    """当前Turn正在调用模型且已有生成进度时给出估算；否则返回None，界面照常显示已结算的用量。"""

    turn = None if view is None else view.current_turn
    if view is None or turn is None or turn.status != "calling_model":
        return None
    for stream in view.streams:
        # model_step是已结算用量的步数；正在调用模型的步骤号更大。
        if (
            stream.progress
            and stream.turn_id == turn.turn_id
            and stream.model_step > turn.model_step
        ):
            counts = latest_counts(stream.text)
            if counts is None:
                return None
            phase: GenerationPhase = "thinking"
            if counts.arguments:
                phase = "editing" if counts.tool == _PATCH_TOOL else "calling"
            elif counts.text:
                phase = "replying"
            generated = counts.reasoning + counts.text + counts.arguments
            return InFlightRequest(
                phase, generated // UTF8_BYTES_PER_TOKEN, view.last_request_tokens or 0
            )
    return None


def approximate(tokens: int) -> int:
    """估算值只保留两到三位有效数字：870、3,400、38,000。"""

    step = 10 if tokens < 1_000 else 100 if tokens < 10_000 else 1_000
    return max(step, round(tokens / step) * step)
