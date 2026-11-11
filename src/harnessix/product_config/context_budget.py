"""正式Coding Agent的Context输入上限与压缩阈值换算。

职责：按Profile链声明的模型窗口（未声明时用产品固定上限）换算估算器口径的输入上限、压缩触发点与目标、
摘要容量，并据此给出Context限额与压缩运行配置；声明窗口时另给出真实Token口径的窗口，供Runtime在
观测到真实输入用量后校准（agent/compaction_calibration，ADR 0132）。非职责：系统指令、动态Context
Source与Runtime装配（见agent_context），也不读取或校验产品配置文件。
"""

from __future__ import annotations

from dataclasses import dataclass

from harnessix.context.compaction_contracts import CompactionPolicy
from harnessix.context.compaction_runtime_contracts import (
    CompactionRuntimeConfig,
    CompactionTokenWindow,
)
from harnessix.context.contracts import ContextLimits
from harnessix.product_config.contracts import CONTEXT_OVERHEAD_TOKENS, ContextCompaction
from harnessix.secrets.publication import MAX_SCAN_BYTES

PRODUCT_CONTEXT_INPUT_LIMIT = 262_144
# 估算器按UTF-8字节计（一个字节至多一个Token）。声明了模型窗口时按每Token 3字节换算：
# 实测代码与英文约3.5～4字节/Token（第10百分位3.53），偶发超窗由Provider超限后的被动压缩兜底。
# 这个比例只在还没有真实用量时使用：工具结果以紧凑文本发送时实测为3.7～6.3（中位4.9），按3换算的
# “窗口一半”实际在真实窗口的30%左右就触发。
_WINDOW_UTF8_BYTES_PER_TOKEN = 3
# 校准比例的上限：观测比例高于它时视为用量上报不可信（例如只报未命中缓存的输入），按它封顶。
_MAX_UTF8_BYTES_PER_TOKEN = 8
# 出站模型请求整体须通过公开输出保护扫描，其规范JSON至多MAX_SCAN_BYTES。估算已含历史、可执行工具
# 与指令的规范JSON；请求另带稳定线上目录与信封字段，为其保留四分之一扫描容量。
_REQUEST_INPUT_CAP = MAX_SCAN_BYTES * 3 // 4
# 声明窗口时压缩触发点与目标各是换算窗口的几分之一（触发分母，目标分母）。产品默认在一半处压缩：
# 压缩有损且每次都要等摘要，交互使用不为累计Token提前压缩；三分之一只用于有累计Token预算的
# 无人值守任务（如评测）。
_COMPACTION_DIVISORS: dict[ContextCompaction, tuple[int, int]] = {
    "half_window": (2, 4),
    "third_window": (3, 6),
}


@dataclass(frozen=True, slots=True)
class ProductContextBudget:
    """Context输入上限与压缩阈值；除摘要输出Token外，单位与估算器一致（UTF-8字节）。"""

    input_limit: int
    compaction_trigger: int
    compaction_target: int
    summary_output_tokens: int
    summary_reserve: int
    # 声明窗口时的真实Token口径；未声明窗口时为None，Runtime不校准。
    token_window: CompactionTokenWindow | None = None

    def context_limits(self, max_output_tokens: int) -> ContextLimits:
        """窗口按输入上限加输出预留与固定封装开销还原，Planner据此得到同一输入上限。"""
        return ContextLimits(
            context_window_tokens=self.input_limit + max_output_tokens + CONTEXT_OVERHEAD_TOKENS,
            reserved_output_tokens=max_output_tokens,
        )

    def compaction_runtime(self, max_output_tokens: int) -> CompactionRuntimeConfig:
        """摘要输出不超过当前链的输出上限；压缩后原样保留最近两组。"""
        return CompactionRuntimeConfig(
            policy=CompactionPolicy(
                target_history_tokens=self.compaction_target,
                summary_reserve_tokens=self.summary_reserve,
                max_summary_input_tokens=self.input_limit,
                retain_recent_groups=2,
            ),
            trigger_history_tokens=self.compaction_trigger,
            max_summary_output_tokens=min(max_output_tokens, self.summary_output_tokens),
        )


def product_context_budget(
    context_window_tokens: int | None, max_output_tokens: int, compaction: ContextCompaction
) -> ProductContextBudget:
    """声明窗口时按压缩点策略换算触发点与目标；未声明窗口时沿用固定输入上限与原摘要容量。

    half_window在窗口一半处压缩、压到四分之一；third_window在三分之一处压缩、压到六分之一。
    换算后的窗口与输入上限不超过出站请求扫描容量的四分之三，大窗口不会产生无法发出的请求。
    声明窗口时摘要预留至多占压缩目标的四分之一（上限20480字节；half_window时即窗口的十六分之一），
    摘要输出按每Token约4字节另加封装换算，至多4096 Token。
    声明窗口时另返回真实Token口径的窗口：Runtime观测到真实输入用量后，用实际的字节/Token比例重算
    触发点、目标与输入上限（只放宽），输入上限同样不超过出站请求扫描容量的四分之三。
    窗口或压缩点不合法时抛出ValueError，无副作用。
    """
    if type(compaction) is not str or compaction not in _COMPACTION_DIVISORS:
        raise ValueError("压缩点策略必须是half_window或third_window")
    if context_window_tokens is None:
        return ProductContextBudget(
            input_limit=PRODUCT_CONTEXT_INPUT_LIMIT,
            compaction_trigger=PRODUCT_CONTEXT_INPUT_LIMIT // 2,
            compaction_target=PRODUCT_CONTEXT_INPUT_LIMIT // 4,
            summary_output_tokens=2048,
            summary_reserve=12_288,
        )
    if (
        type(context_window_tokens) is not int
        or context_window_tokens <= 2 * (max_output_tokens + CONTEXT_OVERHEAD_TOKENS)
        or context_window_tokens > 2_097_152
    ):
        raise ValueError("模型上下文窗口须大于两倍输出上限与1536之和，且不超过2097152 Token")
    window = min(context_window_tokens * _WINDOW_UTF8_BYTES_PER_TOKEN, _REQUEST_INPUT_CAP)
    trigger_divisor, target_divisor = _COMPACTION_DIVISORS[compaction]
    compaction_target = window // target_divisor
    summary_reserve = min(20_480, compaction_target // 4)
    return ProductContextBudget(
        input_limit=min(
            (context_window_tokens - max_output_tokens - CONTEXT_OVERHEAD_TOKENS)
            * _WINDOW_UTF8_BYTES_PER_TOKEN,
            _REQUEST_INPUT_CAP,
        ),
        compaction_trigger=window // trigger_divisor,
        compaction_target=compaction_target,
        summary_output_tokens=min(4096, summary_reserve // 5),
        summary_reserve=summary_reserve,
        token_window=CompactionTokenWindow(
            window_tokens=context_window_tokens,
            input_limit_tokens=context_window_tokens - max_output_tokens - CONTEXT_OVERHEAD_TOKENS,
            trigger_divisor=trigger_divisor,
            target_divisor=target_divisor,
            max_input_bytes=min(
                context_window_tokens * _MAX_UTF8_BYTES_PER_TOKEN, _REQUEST_INPUT_CAP
            ),
        ),
    )
