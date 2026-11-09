---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [agent, context, product-config, evals]
related_adrs:
  - docs/adr/0058-compaction-windows-and-accounted-summary-attempts.md
  - docs/adr/0120-system-neutral-evaluation-platform.md
related_tests:
  - tests/agent/test_completion_guard.py
  - tests/context/test_compaction.py
  - tests/product_config/test_thinking_config.py
  - tests/integrations/test_harnessix_configs.py
supersedes: []
---

# ADR 0124 无人值守任务的完成守卫

## 1. 背景

- **结束规则：** 模型一步没有工具调用，Agent Loop即把这条回复当作最终答复，Turn以`completed`结束
  （`AgentRuntime._close_model_step`）。交互使用时，这表示“完成”或“轮到用户”，用户随后会接话。
- **无人值守时的问题：** 评测没有人接话。holdout第一部分重跑与第二部分前12题中，小模型三种提前收工都以空补丁结束：
  - 只写一句进展就停（astropy-7166：“The bug is confirmed.”）；
  - 照抄上下文摘要JSON（django-13346、django-13112：摘要以助手消息JSON投影，模型复制了它）；
  - 编造错误JSON（django-11149：`{"error":{"code":"provider_error",...}}`，服务端无此错误）。
  其中django-13112是旧版与mini-swe-agent都解出的题。mini要求模型执行约定的提交命令才结束，漏带命令只收到格式提示。
- **压缩约束：** 上下文压缩要求当前Turn恰好一条用户消息，否则规划失败并中断整轮；因此Steering之后一旦压缩，Turn失败。

## 2. 用户决策（2026-11-09）

实施“纯文本回复保护”（方案1）：产品侧实现，配置开关默认关闭，评测配置开启。摘要呈现方式（方案2）与评测侧补跑（方案3）不在本次范围。

## 3. 决策

### 3.1 判定

`harnessix.agent.completion_guard`在同一Thread CAS内判定本步无工具调用的回复（本步无Steering、无目录拒绝时）：

| 本步回复 | 处理 |
|---|---|
| 空正文、JSON对象或含`harnessix.compaction-summary`标记 | 补发`NOT_A_REPLY_NOTICE` |
| 本Turn没有成功补丁，且上一步不是对补发的回应 | 补发`NO_CHANGE_NOTICE` |
| 其他 | 照常完成 |

- “成功补丁”：成功的Tool Result，其调用的Trusted Action审批投影为`patch_batch`（产品路径），或带旧补丁服务效果。
- 模型对“尚未修改代码”提示再次给出纯文本结论即照常完成：用命令改文件或确实无需修改的任务只多花一步。
- 每个Turn最多补发两次；步骤或Token预算已用尽时不补发。判定只读持久事实与本步正文，重放确定。

### 3.2 事件语义

补发提示是用户消息Item（身份`uuid5(turn_id, "harnessix.completion-guard/v1:<step>")`，正文以`[Harnessix host notice]`开头），
随后`TurnStateChanged(PREPARING_CONTEXT, reason="steering")`。沿用Event v19的Steering重入校验，不新增事件类型或版本。
来源由身份命名空间与正文前缀区分；内容是宿主固定文本，不含模型或外部输入。

### 3.3 压缩

压缩把当前Turn的全部用户消息（初始输入、Steering、补发提示）作为锚点整组保留，不再要求恰好一条。
只有一条用户消息时保留集合与旧行为相同；既有Plan重放不受影响。

### 3.4 配置

`ProductConfigV2.completion_guard`：布尔，默认`false`且不序列化（已有配置摘要不变），向导不预设。
`AgentRuntime(completion_guard=...)`只接受布尔值。Harbor评测组别的`product.json`显式开启。

## 4. 后果

- 提前收工从“整题报废”变为“多花一步”；补发计入步骤预算。
- 已有补丁后又以一句进展停下（如旧版sympy-18698）只在回复为JSON时被拦下，不按长度猜测。
- 评测轨迹中，补发提示是下一步模型调用的输入（逐步记录的`user_message`）。
- 默认关闭，交互使用的结束语义不变。

## 5. 验证

- `tests/agent/test_completion_guard.py`：判定规则、上限、预算、真实Runtime重入与重放一致、默认关闭。
- `tests/context/test_compaction.py`：Steering与补发提示在压缩后保留且顺序不变。
- 配置测试：默认不序列化、严格布尔、Harbor配置开启。
