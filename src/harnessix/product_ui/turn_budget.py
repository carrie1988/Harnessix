"""产品客户端（TUI与exec）发起Turn时使用的默认预算。"""

from __future__ import annotations

from harnessix.protocol.contracts import PublicBudget

# 运行时内核的Budget默认值（16步/10万Token/120秒）面向单元与持久化夹具；真实编码任务
# 平均需要数十步与数十万Token，产品客户端显式传入下列预算。墙钟包含等待人工审批的时间。
# 无人值守运行的上限；累计Token含缓存命中、不等于费用，此前的100万会截断约三分之一的真实长任务。
PRODUCT_TURN_BUDGET = PublicBudget(
    max_steps=100,
    max_tokens=3_000_000,
    timeout_seconds=3_600.0,
    max_output_chars=65_536,
    max_tool_calls_per_step=32,
)

# 本地自托管端点没有价格，前缀缓存使累计输入多为重复读取（实测约80%命中缓存，200万约75步即耗尽）：
# 累计token上限放宽到600万，由步数与时限约束，步数与时限不变。
LOCAL_TURN_BUDGET = PRODUCT_TURN_BUDGET.model_copy(update={"max_tokens": 6_000_000})


# 交互模式下用户能看到费用并随时取消，累计Token不再作为任务上限；步数和时限仍然生效。
UNCAPPED_TOKENS = 1_000_000_000


def turn_budget_for(pricing: str) -> PublicBudget:
    return LOCAL_TURN_BUDGET if pricing == "free" else PRODUCT_TURN_BUDGET


def interactive_turn_budget(pricing: str, *, cost_visible: bool) -> PublicBudget:
    """本地端点或费用可见时取消累计Token上限；费用未知的云模型保留上限。"""

    if pricing != "free" and not cost_visible:
        return PRODUCT_TURN_BUDGET
    return PRODUCT_TURN_BUDGET.model_copy(update={"max_tokens": UNCAPPED_TOKENS})
