"""产品客户端（TUI与exec）发起Turn时使用的默认预算。"""

from __future__ import annotations

from harnessix.protocol.contracts import PublicBudget

# 运行时内核的Budget默认值（16步/10万Token/120秒）面向单元与持久化夹具；真实编码任务
# 平均需要数十步与数十万Token，产品客户端显式传入下列预算。墙钟包含等待人工审批的时间。
PRODUCT_TURN_BUDGET = PublicBudget(
    max_steps=100,
    max_tokens=1_000_000,
    timeout_seconds=3_600.0,
    max_output_chars=65_536,
    max_tool_calls_per_step=32,
)
