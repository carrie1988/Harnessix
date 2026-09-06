# Coding Agent多试验质量与成本证据研究

- 日期：2026-09-06
- 范围：终态、Token、时延、成本和失败分类；不研究具体模型排名
- 固定源码：Codex `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67`、OpenCode `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5`、Claude Code源码样本 `2ca5ddabfed5f220812ea11f029eda03b21bc4c1`

## 1. 源码事实

### 1.1 Codex

`sdk/typescript/src/thread.ts:97-140`遍历流事件，只在`turn.completed`取得结构化Usage，遇到`turn.failed`则保留失败并终止正常返回；`sdk/typescript/src/events.ts:20-44`将输入、缓存输入、缓存写入、输出和推理Token与Turn完成事件绑定。`codex-rs/cli/e2e_benches/codex_help.rs`使用固定样本数和单次样本规模执行确定性基准，说明基准参数必须先于测量固定，而不是在结果不理想后调整样本。

结论：单次成功、失败和Usage必须保留在同一Turn事实中；多次试验不能只汇总成功响应，也不能把一次运行外推成稳定能力。

### 1.2 OpenCode

`packages/opencode/src/session/session.ts:338-404`将供应商Usage拆为输入、输出、推理、缓存读写，并按模型价格计算成本；其中对缺失或非有限数字采用零值保护。`packages/opencode/src/session/processor.ts:452-470`在step完成时把finish reason、Token和成本写入Session消息及step-finish部分。

结论：Usage、成本与步骤终态应持久关联，但评测报告不能把未知Usage静默变成零。Harnessix继续使用`complete | partial | unknown`和固定价格快照，避免“零成本”掩盖缺失证据。

### 1.3 Claude Code源码样本

`src/QueryEngine.ts:618-637`的成功结果同时携带墙钟时延、API时延、轮次数、总成本、总Usage和分模型Usage。`src/cost-tracker.ts:181-239`按模型汇总输入、输出、缓存与成本，并在模型价格未知时显式提示估算可能不准确。

结论：质量、时延、Token和成本应在同一试验报告中联合观察；未知模型或价格不能伪装成完整成本。单次运行的总时延仍应与API时延区分，当前Harnessix先记录端到端墙钟时延，Provider专用API时延待后续可观测性契约支持。

## 2. Harnessix决策

1. Campaign在首个真实请求前固定任务指纹、Harnessix revision、Provider、精确模型、运行ID集合、价格快照和宿主核对的计费上下文。
2. 每个运行ID继续拥有独立物化目录、Session、Effect Journal、Patch账本和Eval报告；Campaign不共享模型上下文或工作区，避免后一次试验继承前一次修复。
3. Campaign只读取已完成运行状态、Eval报告、原Turn和可重算CostReport，不从日志、控制台文本或模型回答推断结果。
4. 主分类固定为`passed | provider | eval_infrastructure | runtime | task | budget`；同时保留0.5.5a的细分失败集合。Provider失败仅保存规范错误码和retryable，不保存第三方原文。
5. 成本只接受同一版本化价格快照与计费上下文的逐尝试绑定。任一尝试Usage未知时整体成本为`partial`或`unknown`，已知小计仍保留但不冒充总费用或供应商账单。
6. 时延采用完整Eval墙钟值；P50/P95使用有序样本的nearest-rank，报告同时给出最小值和最大值。小样本只形成基线事实，不宣称统计显著性。

## 3. 未解决问题

- 首轮真实基线已按同一百炼北京精确模型、3次独立试验、Provider不自动重试和人民币10元试验间停止线执行；结果0/3均为任务累计Token预算不适配，见验证记录；
- 供应商账单对账、实时硬费用上限和API专用时延尚未实现；
- 当前宿主检查没有OS Sandbox，只允许受信Catalog中的Harnessix历史任务；
- 多模型横向排名、温度/seed控制和置信区间不属于0.5.5c首个单模型基线。
