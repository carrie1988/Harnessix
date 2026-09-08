# 0.6.3 摘要尝试账本与窗口发布设计草案

- 更新日期：2026-09-08
- 状态：设计草案；尚未实现或冻结Schema，文中的新类型和事件名均为候选
- 前置：[窗口规划与候选校验](compaction-window-planning.md)
- 架构决策：[ADR 0058](adr/0058-compaction-windows-and-accounted-summary-attempts.md)

## 1. 目标与当前实现差距

首窗口规划已经能够生成可重算候选，但候选不是活动窗口，也不是收费请求的完成证据。本设计要求摘要调用成为与普通生成同等可审计、可取消、可恢复的正式请求，且不改变普通model_step的含义。

对现有实现的复核结果如下：

| 实现位置 | 当前约束 | 集成要求 |
|---|---|---|
| `agent/reducer.py::_model_attempt` | 普通尝试只在CALLING_MODEL开始，绑定当前开放step，累计用量按差额加入Turn | 摘要不能伪装成普通生成；复用观测规则，独立验证阶段和归属 |
| `agent/models.py::Turn.usage_is_complete` | 按普通尝试覆盖1至model_steps，并检查用量完整性 | 保留普通步骤覆盖，额外要求全部摘要尝试结算且用量完整 |
| `models/costs.py::_summarize` | `(step,index)`全局唯一，只接受普通步骤；价格绑定绑定CostAttempt快照 | 不能直接将摘要尝试塞入旧entries；新报告区分用途和Compaction身份 |
| `models/costs.py::build_cost_report` | 仅遍历`turn.model_attempts` | 新入口必须遍历两类尝试，禁止漏算摘要费用 |
| `evals/campaign_execution.py::_load_trial` | 仅为普通尝试绑定价格 | 摘要同样需要适用的价格/地域/模式证据，未知费用不能填零 |
| `evals/campaign.py` | 模型列表、尝试数及成本重算均只覆盖普通尝试 | 扩展用途维度及重算范围，并保留旧报告解释 |
| `agent/runtime.py::_finish/_recover` | 仅收尾普通running尝试，启动不自动重放中断Turn | 摘要running尝试也必须收尾；不重发可能已经计费的请求 |
| `models/openai_chat.py::stream`、`models/anthropic.py::stream` | `ModelAttemptStarted`先yield，下一次消费才进入SDK请求 | 包装事件必须先持久化，再消费下一事件，保留HTTP前意图边界 |

这些是本仓库当前接口事实，不代表待新增账本已经上线。

## 2. 领域记录与事件

### 2.1 Compaction运行记录

拟在Turn内增加独立Compaction记录集合。每条记录至少包含：

- 唯一`compaction_id`、所属Thread/Turn、原计划及来源序号；
- 触发原因、目标普通model_step、前一活动窗口身份；
- 请求输入指纹、宿主确定的摘要配置/算法版本及有效预算；
- 独立`ModelAttempt`、摘要响应身份、候选正文及其指纹；
- 运行阶段、失败原因、开始/结束时间，以及仅在发布成功后存在的window_id。

拟采用阶段`planned → sampling → summarized → activated`，任意未发布阶段可失败、取消或中断。`summarized`只说明候选证据已持久化，不能省略来源复核或直接驱动后续普通请求。首版每条Compaction最多一个摘要Attempt，不自动重试；每个目标普通步骤最多一个Compaction，同一目标不能叠加轮前与reactive请求。Turn内记录最多1000条，与普通步骤硬边界共同限制事件增长。

计划无Attempt即取消时，不能凭空生成“已发起请求”或用量为零的虚假观测；相反，HTTP前Attempt意图一旦提交，崩溃恢复就必须承认请求可能发生，直到具备用量事实。

### 2.2 候选事件

| 事件 | 前置条件 | 投影效果 |
|---|---|---|
| `CompactionPlanned` | PREPARING_CONTEXT、来源及预算有效、没有开放Compaction | 持久化计划与有效请求边界，不增加普通model_steps |
| `CompactionAttemptStarted` | 原计划处于planned，索引1，线程级Attempt ID未使用 | 包装原`ModelAttemptStarted`，建立独立running尝试 |
| `CompactionUsageObserved` | 原Compaction/Attempt处于sampling，响应身份不漂移 | 包装原`ModelUsageObserved`，按累计差额计入同一Turn.usage |
| `CompactionAttemptFinished` | 原Attempt未结算 | 包装原`ModelAttemptFinished`，保留Provider请求的真实结算结果 |
| `CompactionSummarized` | 请求已结算，正文/来源/预算等候选校验通过 | 持久化候选及指纹，不激活窗口 |
| `CompactionRejected` | 未发布；如存在running尝试必须先收尾 | 记录失败/取消/中断事实，原活动窗口不变 |
| `CompactionWindowActivated` | 候选及成功Attempt匹配，原窗口/来源复核、CAS成功 | 原子切换活动模型窗口；不修改原Item或效果事实 |

包装事件复用Provider Event v3已有字段，不往冻结Provider事件追加用途或新的含义。`ModelAttempt.step`在摘要集合中绑定计划的目标普通步骤，普通集合中的同名字段含义不变；用途由集合与包装记录决定，不能仅凭一个step数字混用两种账本。

## 3. 用量、预算与费用语义

### 3.1 共享预算但不共享普通步骤序号

普通生成和摘要共同消耗`Turn.usage`与原Turn截止时间。Token累加仍采用累计观测差额，绝不把缓存子项额外加到输入总量，也不把推理子项再次加到输出总量。未知输入或输出仍为未知，不因为计数累计计算暂用0差额而改写观测的完整性。

开始摘要前检查有效输入、输出预留、剩余Turn Token和时间。收到实际用量后立即更新总账；实际用量突破预算时停止后续请求及工具调度，不伪造一个更小计数。摘要生成不能新增普通model_step，也不能替普通失败请求抹去max_steps消耗。

Attempt ID必须在整个Thread的普通及摘要集合中唯一。响应ID、实际模型与已观测计费上下文不允许在后续观测中漂移。复用既有`UsageObservation.validate_successor`和`ResponseBillingMetadata`校验，禁止复制一个语义稍有不同的新计费实现。

### 3.2 Provider请求成功不等于压缩成功

例如Provider返回完整用量和正常终态，但摘要为空、包含Tool Call、超过投影预算或没有足够缩减量：请求本身可能已经成功且产生费用，Compaction却必须失败。应保留已结算Attempt，再记录候选拒绝，不能把费用或成功响应事实删除。

摘要消费器不执行任何工具。若遇到非法工具/正文输出，是否继续读取剩余事件必须受同一截止时间和事件上限约束；已接收的用量必须提交，未接收到的部分标记不完整，不能为获取用量无限排空。默认拒绝第二次摘要Attempt；由于现有Adapter在HTTP前yield开始意图，拒绝发生于下一付费请求之前，并关闭流。

摘要Provider准入必须满足HTTP前尝试意图契约，不允许复用普通循环的无Attempt兼容分支。违反协议时保留不完整账本及失败事实，不补造成功尝试、完整零用量或“确认未计费”的结论。对应反例测试必须证明重开不会再发请求。

### 3.3 成本报告版本

拟新增Cost Report v2，并保持v1 Schema与旧报告重算逻辑不变。新条目复用`AttemptCost/CostAttempt/PriceBinding/CostResult`的数据与计算，在外层增加用途和可选Compaction身份。普通尝试以普通step/index分组；摘要尝试以compaction_id/index分组；Attempt ID仍全局唯一。

不能复用旧`_summarize`直接压平全部条目：同一个普通目标step可以同时有普通index1与摘要index1，旧唯一性规则会冲突，或者被错误改成伪重试。v2汇总应单独验证普通步骤覆盖，再验证各Compaction请求和阶段，最后按币种合并已知费用。任何付费摘要的用量或适用价格缺失，都必须反映在整体完整性中。

`estimate_attempt`、十进制定点金额、价格周期/输入阶梯、地域、service tier、缓存TTL与推理模式规则保持复用。没有适用价格的摘要请求为unknown，不能直接套用普通模型的单价。无需摘要的旧运行继续使用原报告契约，禁止自动改写历史报告或Campaign指纹。

### 3.4 Eval与Smoke

Campaign必须按同一集合生成价格绑定、模型身份列表、用途计数、总用量和成本完整性，并通过Session事实重算报告。包含摘要的费用应进入同一Campaign支出上限；不允许只报告压缩后节省的Token而省略压缩费用。

现有Smoke严格验证文本/工具/审批场景的请求数，本阶段不在其内部隐式启用压缩。新增摘要Smoke或真实长任务Eval时，应使用显式新配置及报告契约，不能为了隐藏额外请求放宽原精确计数断言。

## 4. 发布事务与故障恢复

### 4.1 来源序号与账本推进

纯窗口验证器要求同一原来源快照；实际摘要账本会合法地增加事件序号。因此发布阶段必须同时验证：

1. 原计划对应的来源窗口和原始事实指纹；
2. 来源以后只有本Compaction允许的账本/候选推进，没有新的用户、工具或权限变化；
3. 读取当前投影后按当前sequence执行Session CAS。

不能把`source_event_sequence`直接改成最新序号来绕过验证，也不能完全移除序号比较。Reducer在正式实现时需要明确这些允许的中间事件及其状态守卫；候选本文本通过不构成发布许可。

### 4.2 崩溃矩阵

| 退出窗口 | 恢复结果 | 禁止行为 |
|---|---|---|
| 计划提交前 | 没有压缩运行事实 | 推断曾发送模型请求 |
| 计划提交后、Attempt前 | 计划中断，无已知请求；后续操作需新显式运行 | 将计划当作成功窗口 |
| Attempt意图后、HTTP前后 | Interrupted，保留未知/部分用量 | 因为“可能没发出”而自动重发 |
| 累计用量提交后、终态前 | 保留已知差额并结算Interrupted | 重复累计或填完整零用量 |
| Attempt已结算、候选未提交 | 保留费用；原活动窗口不变 | 从内存或网络响应缓存猜测候选 |
| 候选已提交、激活前 | 只从持久候选与原Attempt证据恢复验证 | 新摘要请求或提前切换窗口 |
| 激活事务提交后 | 恢复已提交窗口及原历史事实 | 重新摘要、重新执行已完成工具 |

取消、超时和宿主退出必须统一结算所有running普通/摘要尝试，并保留未发布Compaction的终态。付费请求恢复验证必须使用真实子进程退出和SQLite重开，不能只以异常Mock代替。

## 5. 安全、可观测性与版本

摘要请求之前验证完整来源的Artifact能力，激活之后仅检查实际活动窗口引用。前者不能被后者替代，跨scope或过期引用不得借压缩消失。Summary不得赋予执行许可；Fork继承摘要时仍必须重新证明Thread来源和效果权限。

新增观测建议为固定`purpose=generation|compaction`、阶段及结果分类，加计数/Token/耗时指标；不把compaction_id、窗口ID、路径、摘要或响应正文作为标签。`Context Inspect`可在Session权限内显示窗口身份、来源范围、用量完整性和失败阶段，不默认输出摘要正文。

候选版本仍为Event/Thread v14、Model History Inspection v2、Cost Report v2与Session migration16。Provider Event v3不变。具体Schema需在账本与发布反例通过后冻结；旧v13数据库升级不得自动摘要或发起任何模型请求，旧reader必须拒绝接管新最低版本。

## 6. 下一实施门禁

1. 写出账本Reducer反例：阶段错绑、跨Turn/跨用途重复ID、用量回退、响应身份漂移、预算耗尽、成功请求但失败候选。
2. 实现独立包装事件与共享观测规则，证明普通尝试旧Replay、Cost v1及冻结Schema不变。
3. 增加Cost v2与Campaign全用途汇总，测试未知价格/用量、混合币种及摘要模型差异。
4. 接入无工具摘要消费器，并用两种实际SDK的MockTransport验证HTTP前意图与取消排空。
5. 接入持久候选和CAS窗口，完成上述子进程崩溃矩阵、旧wheel升级和一次请求不重复证明。
6. 在此基础上接入有界重复压缩及轮前/reactive触发，再进行预算可核算的真实工程Eval。

本草案不改变当前运行行为；上述步骤尚未通过前，自动Compaction继续保持未完成状态。
