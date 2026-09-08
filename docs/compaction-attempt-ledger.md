# 0.6.3 摘要尝试账本详细设计

- 更新日期：2026-09-08
- 状态：独立账本已由自动摘要和活动窗口运行时集成
- 前置：[窗口规划与候选校验](compaction-window-planning.md)
- 决策：[ADR 0059](adr/0059-compaction-attempt-ledger-and-purpose-costs.md)
- 集成：[自动Compaction运行时与活动窗口](compaction-runtime-and-windows.md)

## 1. 系统边界

摘要是独立计费请求，不是普通交付步骤，也不是新的执行权限。实现复用现有Model Attempt、Usage、Billing和价格计算，新增持久包装事件、共享累计差额规则、候选来源复核、成本报告及中断收尾。AgentRuntime默认仍不发起摘要HTTP请求，不激活候选，不删除原Item。

| 模块 | 职责 |
|---|---|
| `context/compaction_ledger_contracts.py` | CompactionRecord与六种正式包装事件 |
| `context/compaction.py` | 异步取消与同步Replay共用同一生成器算法；重算计划和候选 |
| `agent/attempt_accounting.py` | 普通/摘要请求共享用量后继、响应身份、Billing与差额规则 |
| `agent/compaction_reducer.py` | 阶段、来源、预算、时间、身份和候选事实守卫 |
| `agent/reducer.py` | 开放压缩期间的全局事件隔离；跨用途Attempt唯一性 |
| `agent/runtime.py::_finish` | 恢复、取消、超时统一结算开放摘要，不自动重发 |
| `models/costs.py` | Cost v1保持冻结；按用途完整汇总的Cost v2 |
| `evals/campaign*.py` | 全用途价格绑定、模型/请求计数及Session事实重算 |
| `session/sqlite.py` | Event和投影同事务提交、sequence CAS、最低reader标记 |

## 2. 数据与事件契约

`Turn.compactions`最多1000条。每条`CompactionRecord`持有原CompactionPlan、原计划前的已知输入/输出计数、创建/结束事件序号与时间，以及至多一个ModelAttempt。计划事件必须紧邻Plan绑定的来源sequence；结束事件必须晚于计划。目标普通步骤和索引绑定为`plan.model_step`及1；不增加普通`model_steps/usage_step`。

状态为`planned → sampling → summarized`；planned或sampling可终止为failed/cancelled/interrupted。sampling允许请求已结算而候选尚未提交。summarized必须具备已成功结算Attempt、同身份Summary、候选历史SHA256及完整投影Token计数。它不是活动窗口。

| 事件 | 前置与效果 |
|---|---|
| `CompactionPlanned(plan, decisions)` | PREPARING_CONTEXT；重算原sequence、来源、选择和预算；冻结精确新Tool Result决定 |
| `CompactionAttemptStarted(compaction_id, event)` | planned、有效时间/已知Token预算、索引1、线程级唯一Attempt ID；建立running请求 |
| `CompactionUsageObserved(compaction_id, event)` | 已开始且未结算；共享用量与Billing校验，差额加入Turn.usage |
| `CompactionAttemptFinished(compaction_id, event)` | 结算真实请求结果；成功必须有完整用量与响应身份 |
| `CompactionSummarized(compaction_id, summary, candidate_history_sha256, candidate_history_tokens)` | 成功请求、非取消状态、有效截止时间；重算候选再持久化 |
| `CompactionRejected(compaction_id, outcome, failure, unaccounted_request_possible)` | 如有running请求须先收尾；保存失败事实，不回退费用；违反HTTP前意图契约时保守标记未记账请求风险 |

Provider Event v3保持原字段和语义。包装事件通过集合表达用途；不把摘要index1伪装成普通重试index2。Compaction ID在线程内唯一；每个目标普通步骤最多一条记录，包括已经失败的记录，不存在隐式摘要重试。

同一步已提交Context或Model History检查时禁止再规划压缩；必须在普通请求边界冻结前运行。开放压缩期间只接受本账本推进及Turn进入CANCELLING；新的用户、Item、Context、普通请求或终态事件均被阻止，直到账本结算。摘要事件时间不得早于Thread最新事件、计划或Attempt；开始与候选提交还需处于原Turn截止时间内。

## 3. 来源复核与稳定视图

原Plan绑定的是规划前sequence。摘要事件会合法推进sequence，不能将Plan来源序号直接替换为最新值。记录保存紧邻来源的计划事件序号和候选/拒绝结束事件序号；后续窗口发布可要求候选结束事件仍为当前最新事实。Reducer依赖开放事件守卫保证账本期间没有来源变更，再以原sequence和原已知用量计数重建只读校验边界，调用与在线规划相同的同步算法。

该校验边界不落库、不修改当前预算、不替换当前Turn。候选实际提交仍以最新sequence执行SQLite CAS。重新计算原始历史、模型历史、Tool Result决定、覆盖/保留集、摘要来源、候选投影和预算；任一不匹配拒绝整个事务。

规划时冻结的新Tool Result决定写入Turn，但不提前提交普通Model History Inspection。冻结决定与原Item、Artifact义务保持不变。同步Replay不执行Artifact I/O；后续摘要运行时必须在HTTP前验证全部来源的真实scope、TTL、manifest和正文覆盖。仅持有Plan或candidate不能充当Artifact访问授权。

## 4. 用量与费用

`settle_observation`返回更新后的Attempt和输入/输出差额。缓存是输入子项，推理是输出子项，均不二次累计。重复同一累计观测差额为0；用量回退、完整性退化、响应身份或Billing漂移拒绝。未知输入或输出仍保留unknown/partial，不能因差额累计暂按0处理而改写完整性。

实际观测超过Turn Token上限时仍完整入账，后续普通请求被原预算门禁阻断，不截断账目。请求成功而候选为空、过大或无缩减时，请求仍可为completed，Compaction为failed；两者状态独立。

`Turn.accounted_attempts`提供普通及摘要请求全集；`usage_is_complete`保留普通步骤覆盖要求，并额外检查摘要账本闭合及全部请求用量。摘要不能替代缺失的普通步骤。

### 4.1 Cost Report v2

没有Compaction的运行继续由`build_cost_report`返回原CostReport v1。存在Compaction时返回v2；`COST_REPORT_ADAPTER`根据spec_version读取两种报告，旧报告不自动改写。

v2的`AccountedAttemptCost`复用AttemptCost的Attempt快照、PriceBinding及成本结果，并增加purpose和compaction_id。`CompactionCostState`保存目标普通步骤、阶段及可选Attempt身份，不复制摘要、提示词、响应ID或错误正文。

如果摘要Provider在首个持久Attempt意图之前输出、抛错或可能完成网络请求，Compaction以`unaccounted_request_possible=true`失败或中断，只能在没有Attempt的记录上设置该标志。Cost v2即使其他请求价格完整也保持partial，避免把协议违例产生的未知费用伪装为零。计划后且确认尚未调用Provider的取消不设置该标志。

普通请求仍以(step,index)检查连续重试和步骤覆盖；摘要以compaction_id分组且只允许index1；Attempt ID跨用途唯一。未产生请求的取消计划不伪造0 Token请求。已开始摘要必须存在对应成本条目，缺失条目、错绑、重复、超前目标、伪成功状态和篡改小计均拒绝。

报告最多33000条请求（1000普通步骤×32尝试，加1000摘要），最多1000条压缩状态。按币种分别汇总定点金额，不隐式换汇。任何摘要用量、模型或适用价格未知，都降低整体完整性；只有Turn终止、普通步骤无缺口、全部已发请求费用已估算且没有开放压缩时才报告complete。

### 4.2 Campaign

Campaign遍历全部请求绑定价格，并根据同一Turn重算Cost v1/v2。请求计数和实际模型列表包含摘要，费用计入原Campaign上限。故意提供只含普通请求的v1报告会与Turn重算结果冲突，拒绝发布。

当前Campaign计划仍固定一份模型价格和Billing上下文。若摘要使用不同模型，固定价格不适用时保留unknown/partial，而不是套用普通模型单价；支持多模型价格目录需要后续显式版本。Campaign v1保留最多32000次请求的既有上限，超过上限拒绝报告，不截断费用。现有Smoke默认不启用摘要，原精确请求数断言不放宽。

## 5. 恢复、事务与失败语义

| 已提交事实 | 重开结果 |
|---|---|
| 没有计划 | 普通Turn按原恢复流程Interrupted，不补造摘要 |
| planned且无Attempt | Compaction Interrupted，无虚构请求 |
| running Attempt，无用量 | 请求与Compaction Interrupted，账目unknown |
| 部分用量 | 保留原累计计数及partial，统一中断收尾 |
| 完整用量、Attempt未结束 | 保留完整已知用量，请求Interrupted |
| 请求completed、候选未提交 | 保留费用；Compaction Interrupted |
| summarized候选已提交 | 候选原样保留；Turn Interrupted，不激活、不再请求 |

结束请求、拒绝开放Compaction、Error Item与Turn终态由同一Session事务提交。事务内退出时未提交事件和投影一起回滚；提交后退出可重放；反复启动不新增重复结算，不发模型请求或工具调用。自动摘要消费器在该账本上增加HTTP前意图、流关闭和窗口发布验证；完整退出矩阵见[运行时详细设计](compaction-runtime-and-windows.md)。

新事件违反守卫统一为`invalid_event`，整个追加原子回滚。成本报告失败使用契约ValueError，Campaign转换为`eval_campaign_cost_invalid`或证据错误，不返回残缺成功报告。SQLite的存储不可用、并发CAS及迁移错误沿用既有Session错误契约。

## 6. 安全、观测与部署

摘要正文只保存在有权限的Session记录中，不写入Cost/Campaign报告或指标标签。Session数据库及侧文件沿用本地权限、单Runtime所有权和WAL事务策略。运行时以固定`operation=compaction`及有限结果标签记录操作，不使用路径、正文或ID作为指标标签。

数据版本为Agent Event/Thread v14、Cost Report v2、Session migration16；Provider Event v3、Model History Inspection v1、Context Inspection v3不变。`0016_compaction_attempt_ledger.sql`仅推进最低reader，不增加外部数据库或重写旧事件。v1-v13事件及投影继续兼容读取；新摘要包装事件不得标记为旧版本。

## 7. 后续集成边界

无工具摘要消费器、HTTP前意图、流关闭、请求预算、窗口CAS、活动历史、重复压缩、轮前/reactive触发及工程语义保持Eval已在Event/Thread v15与migration17中实现，未修改冻结v14 Schema。该账本仍只负责摘要请求事实和费用，不负责0.6.4会话生命周期或0.6.5通用重试策略。
