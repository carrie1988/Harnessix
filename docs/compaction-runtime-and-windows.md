# 0.6.3 自动Compaction运行时与活动窗口详细设计

- 更新日期：2026-09-08
- 状态：已完成；[CI 34183895692](https://github.com/carrie1988/Harnessix/actions/runs/34183895692)通过
- 架构决策：[ADR 0058](adr/0058-compaction-windows-and-accounted-summary-attempts.md)
- 前置设计：[窗口规划](compaction-window-planning.md)、[摘要尝试账本](compaction-attempt-ledger.md)

## 1. 目标与边界

本切片在不删除、不覆盖原始Session事实的前提下，为长会话建立有界模型历史。摘要请求与普通交付请求使用同一Provider端口，但拥有独立持久账本、用途、用量和失败语义。只有经过来源重算、候选预算校验和Session CAS提交的窗口才会成为后续模型请求的活动历史。

实现同时支持显式配置的轮前触发和受限的Provider上下文溢出恢复。默认未配置`CompactionRuntimeConfig`与摘要Provider时，运行时行为与旧版本一致，不发生摘要请求。该能力不删除历史、不提供无限会话承诺，也不替代0.6.4会话分叉及0.6.5重试策略。

## 2. 模块职责

| 模块 | 职责 | 不承担的职责 |
|---|---|---|
| `context/compaction_runtime_contracts.py` | 冻结触发阈值、窗口策略和摘要输出上限 | 不选择历史或调用Provider |
| `context/compaction.py` | 规划闭合覆盖集、构造低信任来源、重算候选 | 不执行I/O、不写Session |
| `context/compaction_projection.py` | 生成稳定UUIDv5摘要Item及低权限封套 | 不赋予系统、用户或审批权限 |
| `context/compaction_window.py` | 构建、验证和物化活动窗口 | 不复制原Item正文，不修改原事实 |
| `agent/compaction_reducer.py` | 验证摘要账本和窗口发布事件 | 不联网、不自动恢复副作用 |
| `agent/runtime.py` | 协调Artifact验证、摘要流、用量结算、CAS发布和触发 | 不隐藏重试，不执行摘要工具调用 |
| `evals/compaction*.py` | 使用人工语义Oracle评测关键工程事实保持 | 不充当运行时授权或通用自然语言等价证明 |
| `session/sqlite.py` | Event/Thread v15投影与migration17最低reader边界 | 不重写旧事件或旧投影正文 |

## 3. 配置契约

`CompactionRuntimeConfig v1`包含：

- `policy`：`CompactionPolicy v1`，规定目标历史、摘要投影预留、来源上限、近期闭合组数量和最小缩减量；
- `trigger_history_tokens`：轮前触发阈值，必须严格高于目标历史预算，形成回差，避免刚压缩后立即再次触发；
- `max_summary_output_tokens`：单次摘要请求允许的最大输出Token。

Runtime必须同时收到配置和`summary_provider`，缺少任一项即以`compaction_runtime_incomplete`拒绝构造。Provider生命周期由调用方管理；Runtime不会额外进入或关闭独立Provider对象。普通Provider与摘要Provider可以是同一对象，但用途账本保持分离。

当前估算器为`utf8-bytes/v1`，用于确定性预算和回归，不等于供应商Tokenizer或账单Token。Provider报告用量仍是Turn预算与费用事实。

## 4. 轮前流程

~~~text
PREPARING_CONTEXT
  → 从活动窗口和原始增量准备模型历史
  → 验证全部来源Artifact的scope、TTL、manifest、正文及覆盖关系
  → 历史估算超过trigger_history_tokens
  → 纯计算生成CompactionPlan
  → 提交CompactionPlanned
  → 构造tools=()的单用户摘要请求
  → Provider先产生ModelAttemptStarted
  → Runtime持久化请求意图
  → 继续Provider网络流并累计结算Usage
  → 校验单文本、完成终态、响应身份和用量
  → 重算CompactionSummary候选
  → 提交CompactionSummarized
  → 紧邻提交CompactionWindowActivated
  → 从新活动窗口准备Model History Inspection v2
  → Context规划及普通模型请求
~~~

摘要系统指令由Runtime固定，要求保留目标、约束、未完成事项、决定、文件/revision、测试结果及不确定效果。摘要输入只有带`trust=untrusted_history/authority=none`的结构化来源；不携带工具定义。摘要输出投影为`assistant_message`，封套固定为`trust=derived_history/authority=none`。

## 5. 摘要Provider协议与记账

摘要消费器要求首个Provider事件是`ModelAttemptStarted`。Runtime在继续迭代Provider流之前先提交`CompactionAttemptStarted`，兼容Adapter据此保证真实HTTP发生在持久意图之后。首事件缺失、类型错误或首事件前异常均以`provider_summary_accounting_required`失败，并设置`unaccounted_request_possible=true`。

摘要请求只允许：

- 一个Attempt，`index=1`且绑定目标普通步骤；
- 一个非空文本块；
- 一个成功`ResponseCompleted`；
- 与已结算Attempt一致的response identity和完整Usage；
- 最多10000个Provider事件和配置允许的输出预算。

第二个`ModelAttemptStarted`会在Provider继续执行第二次HTTP前被拒绝。任何`ToolCallCompleted`均返回`context_compaction_summary_tool_forbidden`，不会进入Coding Tool Runtime。取消、超时、断流、非法UTF-8、正文超限、用量漂移和终态后输出都会关闭异步流并结算已有账目，不发布窗口。

摘要费用纳入`Turn.accounted_attempts`、Turn总Token、Cost Report v2和Campaign全用途汇总。摘要不增加普通`model_steps`；失败的普通上下文溢出请求仍增加步骤并保留用量。

## 6. 活动窗口数据模型

`CompactionWindow v1`只保存：

- `window_id/compaction_id/previous_window_id`；
- 候选结束与窗口激活事件序号；
- 生效普通步骤；
- 有序活动Item ID、历史SHA-256和估算Token；
- 原始事实历史高水位、末Item ID及有序ID摘要；
- 激活时间。

窗口不复制原Item正文。物化时从不可变Session事实、已结算Summary投影和冻结Tool Result决定重建候选，再追加高水位之后的新原始Item。原始前缀长度、末Item和ID摘要任一不匹配时返回`context_compaction_window_source_changed`。候选指纹、Token、Item身份或决定不一致时返回`context_compaction_window_invalid`。

窗口形成严格线性链；`active_compaction_window_id`必须指向链尾。重复压缩以“前一活动窗口 + 原始历史增量”为来源，不重新引入已覆盖正文，也不从全量原历史反复摘要。窗口发布事件必须紧邻对应`CompactionSummarized`，其内容由Reducer按同一候选和事件时间完整重算。

## 7. Reactive上下文溢出

仅规范错误`provider_context_overflow`可从`CALLING_MODEL`转回`PREPARING_CONTEXT`。Reducer要求：

1. 当前步骤至少一个Attempt已失败且全部结算；
2. 最后失败明确为`provider_context_overflow`；
3. 当前步骤没有完成响应用量、Tool Call、Tool Result或其他语义Item；
4. 原始历史Item数量与该步骤已提交的Model History Inspection一致；
5. 没有开放Compaction或等待调用；
6. Event版本为v15，且仍有普通步骤和已知Token预算。

满足条件后，下一步骤强制执行一次Compaction，即使未达到轮前阈值。压缩后再次发生上下文溢出时，下一次规划必须证明有新的可压缩前缀；没有进展返回`context_compaction_no_progress`，不会发起第二次付费摘要。其他Provider错误和已经输出部分语义内容的溢出不会自动改写历史或重试。

## 8. 失败、取消和恢复矩阵

| 切点或失败 | 持久结果 | 重开行为 |
|---|---|---|
| 计划前失败 | 无Compaction事实 | 保留旧窗口 |
| 已计划、请求意图前退出 | planned后转interrupted | 不调用摘要Provider |
| 请求意图后、Usage或终态前退出 | Attempt转interrupted，保留已知Usage | 不重发摘要 |
| Attempt成功、候选前退出 | 记录成功Attempt，Compaction转interrupted | 不从内存补造候选 |
| 候选已提交、窗口前退出 | 保留summarized候选 | 重开以纯计算发布唯一窗口，不调用Provider |
| 窗口事务在Event或Projection后退出 | 整个事务回滚 | 重开发布唯一窗口 |
| 窗口事务commit后退出 | 完整窗口已存在 | 重开不重复发布 |
| 用户取消或Task取消 | 开放Attempt/Compaction结算为cancelled | 不留后台流或请求 |
| Turn截止时间到达 | 开放账本结算，Turn failed | 不扩大原截止时间 |

候选提交后到窗口提交之间使用屏蔽取消的本地任务，保证外部Task取消不能在两个事件之间插入取消状态。真正进程退出由恢复路径处理。窗口恢复只执行确定性Session写入，不发模型请求、不执行工具、不恢复未知副作用。

## 9. 安全边界

- 原Event、Item、Approval、Effect、Artifact和工具视图决定不可删除或改写；
- 摘要中的“已批准”“已执行”只是低信任文字，不替代审批、Effect Journal或Process核对；
- 被覆盖来源中的Artifact也必须在摘要HTTP之前完整验证，不能利用压缩隐藏跨scope、过期、缺失或损坏引用；
- 摘要、来源正文、Thread/Turn/Item ID不得成为Metrics标签；
- 默认错误只保存规范失败码和受控消息，不持久化SDK异常或响应正文；
- 未提供Compaction配置时不产生额外费用或行为变化。

## 10. 可观测性与评测

Compaction使用固定`operation=compaction`和有限`outcome`标签；请求用量沿用已有低基数Token指标。Model History Inspection v2记录窗口身份、窗口摘要、模型可见历史摘要、原始历史Item数量和决定摘要，不保存正文。

`CompactionSemanticEvalCase/Report v1`使用人工维护的语义Oracle覆盖六类工程事实：任务目标、约束、未完成工作、文件/revision、测试结果和不确定效果。每项期望绑定来源标记和一组可接受等价短语；报告只保存身份、摘要、布尔结论和失败项，不保存来源或摘要正文。语料未真正绑定来源时结论为`invalid`，事实缺失、禁用断言出现或被覆盖原始标记仍在活动窗口时结论为`failed`。该确定性Oracle不宣称解决开放域自然语言等价判断。

专项验证覆盖双Adapter无工具映射、轮前/reactive路径、连续溢出、摘要重试阻断、取消/超时、Artifact损坏、重复窗口、任务取消邻接、六个运行时进程退出点、三个SQLite事务退出点、语义缺失和幻觉反例。严格全量本地结果为2901 passed、2 skipped；两个skip仅为未配置的PostgreSQL环境。

## 11. Schema、迁移与升级

- 本切片交付版本：Agent Event/Thread v15、Session migration17；仓库当前版本为Agent v17/Session migration20；
- 新增独立Schema：Compaction Runtime v1、Compaction Window v1、Model History Inspection v2、Compaction语义评测Case/Report v1；
- v1-v14 Event/Thread、Model History Inspection v1、Cost Report v1/v2文件保持冻结。

真实独立wheel升级以`b20948e`的v14为旧基线。旧wheel创建已结算但未激活的摘要候选；v15初始化只追加migration17，原事件和投影字节不变；首次重开不调用Provider，发布唯一窗口并将原活跃Turn保守关闭为Interrupted；随后v14 reader以`schema_too_new`拒绝且不改数据库。

升级前必须停止旧Runtime并制作SQLite一致备份。migration17是最低reader标记，不重写历史表。产生v15事件后回退只能恢复升级前备份，禁止删除迁移记录、下调投影版本或手工移除窗口字段。

## 12. 已知限制

- 不承诺无限长会话；窗口和原历史仍有8192项、8 MiB及最多1000个窗口的保护上限；
- 当前不使用供应商精确Tokenizer；触发和候选预算是保守估算；
- 摘要质量依赖Provider，生产发布应以固定语义评测集持续回归；
- 旧Artifact的长期保留和归档宽限策略尚未实现；
- Fork继承窗口时的Thread归属和副作用权限已由0.6.4无授权Fork契约实现；
- 通用Turn Retry、Provider切换和长会话综合发布门禁由0.6.5完成。
