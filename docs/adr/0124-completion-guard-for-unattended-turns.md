---
doc_type: adr
status: reviewing
version: 2
code_revision: pending
owners: [core]
modules: [agent, models, context, product-config, evals]
related_adrs:
  - docs/adr/0058-compaction-windows-and-accounted-summary-attempts.md
  - docs/adr/0109-recoverable-truncation-and-read-guidance.md
  - docs/adr/0120-system-neutral-evaluation-platform.md
related_tests:
  - tests/agent/test_completion_guard.py
  - tests/agent/test_reply_shape.py
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

第3.1节为第2版（见第6节）；第3.2～3.4节自第1版起未变。

### 3.1 判定

`harnessix.agent.completion_guard`在同一Thread CAS内判定本步无工具调用的回复（本步无Steering、无目录拒绝时）。
回复形态由纯文本函数[`reply_shape`](../../src/harnessix/agent/reply_shape.py)给出，自上而下取第一条成立的规则：

| 本步回复 | 处理 |
|---|---|
| 整条回复是只包着文本的线上外壳（`{"type":"text","text":…}`、内容块数组、助手消息外壳） | 去壳，按壳内正文继续判定；持久化的回复不变 |
| 空正文；照抄宿主记录（上下文摘要、占位）；整条回复是一个JSON对象 | 补发`NOT_A_REPLY_NOTICE` |
| 以JSON记录开头却无法解析（截断或写坏）；行首工具调用标记后没有完整JSON | 补发`CUT_OFF_REPLY_NOTICE`：说明回复被截断、什么都没执行，要求改小后经工具接口重发 |
| 整条回复是带工具调用的线上外壳；行首工具调用标记后跟着完整JSON | 补发`CALL_AS_TEXT_NOTICE`：要求改用真正的工具调用，宿主不执行正文里的调用 |
| 上一步是对补发的回应 | 照常完成（以下两行不再判定） |
| 回复宣布还要继续：开头或结尾句说了自己的下一步，或以冒号结尾 | 补发`UNFINISHED_STEP_NOTICE`；与是否已有补丁无关 |
| 本Turn没有成功补丁 | 补发`NO_CHANGE_NOTICE` |
| 其他 | 照常完成 |

- “成功补丁”：成功的Tool Result，其调用的Trusted Action审批投影为`patch_batch`（产品路径），或带旧补丁服务效果。
- 前四行是结构判定，这类回复不可能是答复，每次都补发；后两行靠措辞与状态推断，可能误判真实的最终答复，
  因此问一次即止：模型对提示再次给出纯文本结论即照常完成，用命令改文件或确实无需修改的任务只多花一步。
- **补发次数随可观察状态变化。** 看不出未完成迹象时每Turn至多2次；本Turn尚无成功补丁、最新计划有未完成步骤，
  或最近一次工具结果不是成功，任一成立即放宽到硬上限6次。硬上限与状态无关，守卫因此必然终止。
  步骤或Token预算已用尽时不补发。
- 每条提示末尾写明剩余模型调用次数（`max_steps - model_steps`，重试占用的次数未扣除，故称“至多”）。
- 判定只读持久事实与本步正文，重放确定。

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
- 已有补丁后又以一句进展停下（如旧版sympy-18698）：进展句在开头或结尾句说了下一步时被拦下，否则照常完成，不按长度猜测。
- 评测轨迹中，补发提示是下一步模型调用的输入（逐步记录的`user_message`）。
- 默认关闭，交互使用的结束语义不变。

## 5. 验证

- `tests/agent/test_reply_shape.py`：回复形态、去壳与继续意图的正反例，含真实案例的通用形状。
- `tests/agent/test_completion_guard.py`：判定规则、随状态变化的次数、终止性（全部状态组合下补发次数不超过硬上限，
  且与步骤预算无关）、预算、历史占位、真实Runtime重入与重放一致、默认关闭。
- `tests/context/test_compaction.py`：Steering与补发提示在压缩后保留且顺序不变。
- 配置测试：默认不序列化、严格布尔、Harbor配置开启。

## 6. 第2版修订（2026-11-11）：判定加固、次数随状态变化、历史占位

### 6.1 背景

SWE-bench Multilingual holdout30（运行447fe024，60个试次）里，无工具调用的真实回复共48条。第1版守卫在4个任务上漏判或误判：

| 试次 | 回复 | 第1版 | 问题 |
|---|---|---|---|
| lombok-3350 第33、36步 | 一段键值记录，缺收尾括号（输出仅三百多Token，不是输出上限截断） | 解析失败，当成真实答复；第36步因固定2次上限放行 | Turn在第36步以空补丁结束，剩64步。这一处决定了该试次 |
| terraform-35611 第68步 | 错误记录在字符串中间停下 | 同上，补发的是“尚未修改代码……请再回复一次最终总结” | 提示与事实不符 |
| laravel-48573 第42步 | 真实的最终总结被包成`{"text":…,"type":"text"}` | 判为JSON记录退回 | 模型此后58步反复“最后确认一次再总结”，约25分钟 |
| prometheus-15142 第4步 | 整条消息外壳，文本块后带`tool_use`块 | 判为JSON记录 | 结论对，提示没有说出该怎么做 |
| gson-1014 第61步、gson-2061 第17步 | “…My next step is to confirm…”“…I just need both on the runtime classpath.” | 只看开头几个词与结尾冒号，放行 | Turn带着未验证的补丁结束 |

步号是模型步骤号（事件里的`usage_updated.step`）；渲染记录把同一条回复标在上一步的`ASSISTANT`下，小1。

另外，判为“不是答复”的乱码原样留在模型历史里：lombok-3350的后两段记录与第一段字段值完全相同，模型在照抄自己的上一条回复。

### 6.2 决策

1. **截断或写坏的结构化片段不是答复**（第3.1节第3行）。提示如实说明回复被截断、什么都没执行，并要求改小重发。
   因输出上限而截断的纯文本回复仍由ADR 0109第4节的截断恢复先行处理，不进入守卫。
2. **正文里的工具调用不执行，要求重发**（第3.1节第4行）。只包着文本的外壳去壳后按正文判定（第1行）。理由见6.3。
3. **次数随状态变化，硬上限6次**（第3.1节）。
4. **继续意图检测扩展到结尾句**，只认第一人称的下一步（“my next step”“let me”“I'll”“I need”及对应中文），
   不认泛指的“next step”和“Let me know…”：结尾句常是给用户的建议或客套。
5. **提示写明剩余模型调用次数。** 产品路径的Provider包装（`SafeFallbackProvider`）不实现输入估算端口，
   逐请求的预算说明（`budget_instruction`）因此从不发送；守卫在自己的提示里给出这一宿主已知的事实。
6. **历史占位。** [`without_non_answers`](../../src/harnessix/agent/completion_guard.py)在出站映射
   （`messages_for`）处把守卫已判为不是答复的结构化乱码换成固定短占位。只替换同时满足以下条件的助手消息：
   紧跟一条“不是答复”类提示；自身形态仍是JSON对象、截断片段或写成文本的调用；比占位长。
   照抄宿主记录的回复不替换，因为它与宿主自己投影的上下文摘要无法区分，而摘要在压缩后可能紧挨着提示。
   持久Item、模型历史检查记录与重放不变；占位不长于原文，出站输入只减不增，输入预算估算保持保守。

### 6.3 为什么不执行正文里的工具调用

- 工具调用由Provider的调用通道在流正常结束后原子形成：带Provider调用身份，经参数还原与修复、目录校验、
  强制选择与互斥组检查、每步调用数上限，再进入审批与Trusted Action。从正文合成调用要在`_close_model_step`
  另开一条绕过这些检查的入口，且本步的停止原因已按“无调用”落账。
- 正文是不可信的自然语言：模型可能只是在引用工具输出或文件内容。执行它会把被引用的数据变成动作，
  是提示注入的直接通道。
- 重发只花一步，且结果确定。只包着文本的外壳不含任何动作，去壳只影响判定，因此可以直接采纳。

### 6.4 未采纳

- **“最近一次工具结果失败”作为触发条件**（复盘报告的建议）：gson-2061最后一条命令在宿主看来是成功的
  （复合命令以`echo`结尾，退出码0，失败写在标准输出里）；60个试次中唯一以失败结果收尾的nushell-13831
  已经完成并通过。该信号只用于放宽次数，不单独触发补发。
- **“计划有未完成步骤”作为触发条件**：7个以未完成计划收尾的已完成试次里，4个已经做完并通过。同样只用于放宽次数。
- **结尾句以动名词起头的进展句**（“Rerunning now.”）：能把带调用步骤的旁白识别率从77%提到94%，
  但48条真实回复里没有这类提前收工，且会误伤“Running the suite shows…”这类结果陈述。
- **正文后面跟着的裸JSON调用**（没有调用标记、不是整条回复）：没有真实案例，且与文档里的JSON示例无法区分。

### 6.5 验证

以48条真实回复连同当时的补丁、计划、工具结果状态重放判定：37条真实最终答复仍照常完成（没有新增误报）；
3条原先放行的改为补发（lombok-3350第36步、两个gson试次）；laravel的包装总结改为照常完成；
5条提示改为与事实相符。重放只比较逐条判定，判定改变之后的步骤是反事实，未重新运行模型。

### 6.6 合入主线（2026-11-14）：与交互模式并存

[ADR 0147](0147-interactive-prompt-for-announced-steps.md)在主线上为完成守卫增加了交互模式。合入后两种模式并存：

- 完整模式（无人值守）按本修订：回复形态判定、补发2～6次、提示写明剩余调用次数。
- 交互模式只有“宣布了下一步却没有动作”一条规则（`reply_shape.leaves_a_step_undone`，不认结尾句的意图）；
  补发次数取基础值2次，不按“尚无补丁”等未完成迹象放宽（问答本来就没有补丁）；提示不附剩余调用次数。
- 空回复与截断在两种模式下都先由回复恢复处理（ADR 0109第6版），完成守卫不再对它们判定。

