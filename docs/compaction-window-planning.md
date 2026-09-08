# 0.6.3 压缩窗口规划与候选校验详细设计

- 更新日期：2026-09-08
- 状态：首窗口领域契约与无副作用实现已落地；自动Compaction运行时尚未接入
- 总体方案：[ADR 0058](adr/0058-compaction-windows-and-accounted-summary-attempts.md)
- 前置能力：0.6.2c稳定Tool Result模型视图及Artifact绑定验证

## 1. 范围与完成边界

本模块解决三个确定性问题：从完成历史选择不拆工具组的覆盖集和保留集；冻结来源、策略和预算证据；校验摘要候选是否对应同一快照且确实缩减窗口。它不调用Provider、不读取文件或Artifact、不写Session、不授予执行权限。

本实现是0.6.3内部开发门禁，不是单独完成的生产切片。摘要尝试账本、费用报告、窗口CAS发布、付费请求中断恢复、重复压缩、轮前/reactive触发和语义保持Eval仍属于0.6.3的必要交付。当前`AgentRuntime`不会调用本模块，启用或传入`CompactionPolicy`也不会自动发起请求。

首窗口规划仍使用完整的原历史准备边界：8192个模型可见Item、8 MiB原始规范JSON。超过硬保护上限时明确失败，不借“压缩”绕过内存保护，不声明支持无限长会话。后续活动窗口接入必须在已有窗口上增量选择，不能反复从完整事实历史重新摘要。

## 2. 模块边界

| 模块 | 职责 | 非职责 |
|---|---|---|
| `context/compaction_contracts.py` | 策略、来源锚点、计划、摘要四类v1契约及内部一致性 | 不承担Session聚合或活动窗口发布 |
| `context/compaction.py` | 异步协作取消的闭合组选择、规范计量、来源复核和候选投影 | 不执行模型、工具、Artifact I/O或数据库事务 |
| `context/tool_result_view.py` | 复用原始事实到冻结模型视图的唯一转换 | 不改变旧模型已见前缀 |
| `agent/errors.py` | 新失败码的预算、输入和冲突分类 | 不自动重试 |
| `scripts/generate_specs.py` | 导出四份独立JSON Schema | 不升级现有Event/Thread/Provider Schema |

依赖方向为`Compaction Planner → Tool Result模型视图 → 既有Session领域模型`。运行时集成时仍由Agent Runtime协调Artifact访问和事件提交；Context Engine内部不能隐藏摘要调用。

## 3. 契约

### 3.1 CompactionPolicy

| 字段 | 含义与边界 |
|---|---|
| `target_history_tokens` | 候选历史的总预算，512至8,388,608；宿主应先扣除工具、指令及Provider预留 |
| `summary_reserve_tokens` | 完整摘要投影Item的预留，默认4096，256至1,000,000；必须小于目标历史预算 |
| `max_summary_input_tokens` | 结构化摘要来源正文的有效预算，512至8,388,608；宿主须另外扣除摘要请求自身的指令、协议及输出预留 |
| `retain_recent_groups` | 必须保留的最近闭合组数，默认2，1至8192 |
| `min_savings_tokens` | 按预留上界计算的最小缩减量，默认256，1至8,388,608 |

所有数值拒绝布尔、浮点及字符串代替整数。`utf8-bytes/v1`复用现有Context的保守估算：一个UTF-8字节计一个估算Token。它不是供应商Tokenizer，不得据此计算实际费用。

摘要预留计入完整Item、低信任JSON封套、转义及元数据，而不只统计正文字符。很小的合法预留仍可能容不下任何有效摘要，此时返回`context_compaction_summary_overflow`，不缩短正文或扩大配置。

### 3.2 CompactionAnchor

包含原`item_id`和该原始Item规范JSON的`source_sha256`。最多64个，身份不可重复。宿主固定一个Tool Result时，实际保留其所属完整助手/调用/结果组；不能只保留结果而丢弃调用。锚点必须匹配Session原始事实，不接受只匹配裁剪副本的指纹。

两类原用户消息自动固定，无需宿主重复提供：

1. 首条历史用户消息：保持既有Anthropic映射的user起始约束；
2. 当前活跃Turn的唯一用户消息：保持当前任务输入逐字不变。

固定组参与总预算。无法容纳时明确失败，不让摘要器改写或“概括”这些固定项。

### 3.3 CompactionPlan

计划绑定`compaction_id/thread_id/turn_id/source_event_sequence/model_step`、两个策略对象和来源锚点。原始来源、准备后模型历史、冻结结果决定、摘要来源正文、保留历史分别记录SHA-256。

有序集合包括`source_item_ids/covered_item_ids/retained_item_ids/pinned_item_ids`，每组最多8192项。覆盖集和保留集必须无交集且完整划分来源；固定集必须包含于保留集；每个集合内部唯一且遵守原顺序。计划只记录正文摘要和ID，不复制全部原始正文。

计划同时保存原模型历史、保留模型历史和结构化摘要来源的实际字节估算。验证器会重新执行原算法，而不是信任序列化计划自报的计量、分区或摘要。

### 3.4 CompactionSummary与候选

`CompactionSummary`绑定`compaction_id`，正文必须为非空白、无NUL的有效UTF-8文本，字符硬上限1,000,000。`ValidatedCompaction`返回内存中的计划、摘要、候选Item历史及其摘要和计量；其存在不证明模型尝试已经结算、窗口已经持久化或语义已经完整保持。

摘要投影ID由`UUIDv5(compaction_id, "harnessix.compaction-summary/v1")`生成。投影是普通`assistant_message`，带`trust=derived_history`和`authority=none`的版本化封套；它不是系统指令、原用户输入、Approval或Effect。

## 4. 核心流程

~~~text
只读Thread快照
  → 状态、步骤、已知Turn预算检查
  → 复用prepare_model_history，取得冻结视图及全部Artifact验证义务
  → 原始锚点指纹复核
  → 构建保守闭合组
  → 固定首条/当前用户及显式锚点，保留近期后缀
  → 在预算内向前扩展后缀
  → 生成来源分区、实际计量及SHA-256证据
  → PreparedCompaction（无Provider请求、无持久化）

原快照 + 原计划 + 摘要候选
  → 身份/序号复核
  → 原算法重算并逐字段比较计划
  → 摘要封套/稳定ID/完整投影预算复核
  → 首条原用户 + 低信任摘要 + 其余原序保留历史
  → 配对复核、候选指纹与实际缩减量
  → ValidatedCompaction（仍未发布）
~~~

### 4.1 闭合组

独立用户消息构成单项组。连续助手文本块与后续调用组成一个保守助手组；只有该组全部结果到齐后才能结束。结果允许按完成顺序排列，但第一个结果出现后，未完成的结果组不能插入文本、用户或新调用。相邻纯助手块在没有结果或用户边界时合并，不猜测缺失的model_step元数据。

这一做法可能少压缩，但不会为追求压缩率拆开一次响应的并行调用。重复Item、重复调用、重复结果、孤立结果、未结算调用、未支持的`reasoning_summary`均明确失败。原`Plan/Approval/Effect/CompactionContent`审计Item不会被当作普通模型历史或窗口激活依据。

### 4.2 选择算法

先保留最近N组及所有固定组，计算剩余预算。必选部分已超限立即失败；否则从后缀前方逐组向前扩展，遇到第一组无法容纳即停止，不跳过大组去拼接更旧碎片。固定组可以位于覆盖前缀内部，因此准确结构是“闭合前缀扣除固定组 + 连续近期后缀”，而不是无例外地截断一个索引。

必须满足：

~~~text
retained_tokens + summary_reserve_tokens <= target_history_tokens
source_tokens - retained_tokens - summary_reserve_tokens >= min_savings_tokens
summary_source_utf8_bytes <= max_summary_input_tokens
~~~

校验候选时改用实际完整摘要投影字节数再次检查。不存在可覆盖前缀、没有缩减空间或摘要输入本身太大时，不截断、不自动重试、不伪造成功窗口。

## 5. 信任与Artifact边界

摘要来源是带`trust=untrusted_history/authority=none`的结构化JSON，只包含覆盖集的已准备模型可见内容。Tool Call只保留工具名、调用ID和参数；Tool Result只保留公共结果、错误及公开差异引用。私有Action ID、审批标记、指纹以及Patch/Process效果投影不复制到摘要请求正文。

首条原用户消息被保留在摘要之前，避免为了满足Anthropic协议而把摘要伪装成用户指令。现有Adapter仍检查具体Provider请求的边界：例如Anthropic不支持assistant结尾的prefill。规划器不补造结尾用户消息；正式运行时只能在合法请求边界进入压缩，并在付费请求前完成Adapter准入。

`PreparedCompaction.model_history.references`保留**整个来源历史**的验证义务，包括将被摘要覆盖的引用。规划函数不读取Artifact，也不声称任何引用已经验证。运行时必须先通过实际Workspace scope、归属、TTL、manifest、正文与覆盖证明检查，才可把`summary_source`送给摘要Provider；不能只验证最终保留后缀，借压缩隐藏跨scope或过期引用。

摘要中的“已批准”“已经执行”始终是普通文字，不能替代原审批和效果记录。逐字保留的锚点由选择算法保证；目标、未完成工作、revision及不确定效果的语义保持仍须后续真实Eval，当前文本校验不宣称具备此能力。

## 6. 失败、取消与恢复

| 失败码 | 类别 | 行为 |
|---|---|---|
| `context_compaction_unsafe_state` | input | 非准备态、有开放Item/尝试或其他活跃Turn；不生成计划 |
| `context_compaction_invalid_history` | input | 非法分组、身份或消息边界；不截断修复 |
| `context_compaction_anchor_mismatch` | input | 锚点缺失、重复、超限或原指纹不匹配 |
| `context_compaction_retained_overflow` | budget | 固定项及近期完整组无法容纳 |
| `context_compaction_source_overflow` | budget | 摘要来源超过有效输入预算 |
| `context_compaction_summary_overflow` | budget | 完整摘要投影超过预留或文本上限 |
| `context_compaction_no_progress` | budget | 无覆盖前缀或无足够缩减量 |
| `context_compaction_source_changed` | conflict | 候选身份、来源序号、内容或重算计划不一致 |
| `context_compaction_candidate_invalid` | input | 计划/摘要契约不合法或稳定投影ID冲突 |

原Tool Result/Context校验失败码原样保留。Turn模型步骤或已知Token预算耗尽返回既有`budget_exceeded`；不能靠摘要规划越过总预算。

规划和候选校验使用`CancelToken`，在入口、逐Item分组、向前选择及关键阶段协作让出事件循环。父Task取消和`asyncio.timeout`同样传播，不吞掉取消，不留下后台任务。原有同步JSON准备仍受8192项/8 MiB硬上限保护，不提供硬实时CPU抢占保证。

由于本模块没有持久化副作用，取消、超时和异常均不改写Session。计划可以序列化后在**同一来源快照**重算；来源变化必须拒绝。这不是付费摘要请求的崩溃恢复证明。后续账本推进会增加事件序号，发布事务须区分允许的本Compaction账本事件与真正来源变化，不能删除序号比较或简单复用过期快照作为CAS依据。

## 7. 持久化、部署和可观测性

本次新增四份独立v1 JSON Schema；Agent Event/Thread仍为v13，Provider Event仍为v3，Session migration仍为15。不修改旧Schema、旧事件、原Item、Tool Result决定或Artifact，不需要新增中间件。

`PreparedCompaction`与`ValidatedCompaction`是宿主进程内对象，正文属性不出现在默认repr中；不要把完整计划或摘要作为Metrics标签。计划指纹可供受Session权限保护的诊断；固定失败码、来源/保留/摘要字节数是后续运行时遥测允许的低基数统计。本阶段未新增运行时Metrics或Context Inspect命令，不把未接入的观测路径描述为已上线。

## 8. 验证与后续门禁

当前测试包含闭合组所有三调用结果顺序、当前/首条用户保留、单结果锚点扩展、快照篡改、计划往返、原JSON非法值、Unicode及转义预算、恰好上限/多一字节、来源数量/字节保护、取消/超时/父Task退出、冻结视图和私有字段排除。

实际文件验证使用正式Coding Tool Runtime读取临时工作区源码，保存SQLite Session，再对新Turn的只读快照生成计划；重开数据库和Replay后验证同一候选，原事件及源文件指纹不变。OpenAI与Anthropic映射均验证原用户起始、低信任摘要及保留工具组。这些验证不产生真实模型请求费用。

下一门禁按ADR 0058继续：

1. 独立Compaction Attempt包装事件、与普通Attempt共享的Token增量和线程级唯一身份；
2. Cost Report/Eval聚合两类尝试，保留未知/不完整费用语义；
3. 摘要事件消费、无工具/拒绝/空输出校验、取消和中断结算；
4. 持久活动窗口、候选与尝试绑定、原子发布和新旧reader迁移；
5. 重复压缩、轮前/reactive入口、真实工程任务及语义保持Eval。

所有后续门禁完成前，0.6.3保持进行中，整体0.6及V1.0商用验收均未关闭。
