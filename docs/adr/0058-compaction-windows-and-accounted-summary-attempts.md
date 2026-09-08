# ADR 0058：可审计压缩窗口与独立摘要尝试

- 状态：Accepted（实现、本地完整验收及[CI 34183895692](https://github.com/carrie1988/Harnessix/actions/runs/34183895692)通过）
- 日期：2026-09-08
- 范围：0.6.3
- 前置：ADR 0054、ADR 0057、ADR 0059、现有Model Attempt/Billing与Session恢复契约
- 源码依据：[Compaction与长会话窗口研究](../research/compaction-and-context-windows.md)
- 详细设计：[自动Compaction运行时与活动窗口](../compaction-runtime-and-windows.md)

## 1. 问题与边界

单结果裁剪不能控制累计历史长度。直接覆盖Thread.items、把摘要器藏在Context Planner中、或者失败后反复调用摘要器，分别会破坏审计、漏记费用和放大成本。0.6.3必须交付一个包含正式契约、用量结算、窗口发布、恢复和评测的长会话纵向切片，而不是只增加摘要函数。

本决策由三个连续门禁落地：首窗口规划及候选校验、Event/Thread v14独立摘要账本与Cost v2，以及Event/Thread v15自动摘要运行时和活动窗口。默认未配置时不产生摘要请求或行为变化。

## 2. 决策

### 2.1 独立活动窗口，不改事实历史

Thread增加活动模型窗口身份。窗口由首条原用户消息、版本化Summary、按原顺序保留的固定组和近期后缀构成。首条原用户消息满足既有Anthropic端口的user起始约束；Summary不伪装为用户或系统指令。原Event、Item、Approval、Effect、Artifact和工具视图决定保持不变。

`CompactionWindow v1`保存窗口及前序窗口身份、候选结束和激活序号、生效步骤、有序活动Item ID、候选摘要与估算Token，以及原始历史高水位、末Item ID和有序ID摘要。窗口不复制原Item正文。窗口形成严格线性链，活动ID必须指向链尾。

物化时从原始Session事实、稳定Summary投影和冻结Tool Result决定重建候选，再追加高水位之后的原始增量。重复压缩基于上一活动窗口和新增原始事实，不重新引入被覆盖正文。

### 2.2 闭合组选择

窗口选择从已准备的模型历史建立保守响应组：连续助手块与该响应的全部Tool Call、全部完成Tool Result不拆组。Item没有model_step时不猜测相邻纯文本的响应边界。必须保留最近完整组、首条/当前用户消息和显式锚点；其余闭合前缀作为摘要来源。

存在未完成Item、未结算请求、等待审批/Action、非法调用配对、来源变化或无可证明缩减时拒绝压缩。不通过字符串截断修复非法历史。

### 2.3 摘要请求显式记账

摘要使用独立Compaction Attempt集合，不冒充普通交付步骤。Attempt ID在Thread内跨用途唯一；普通与摘要请求共享Turn总Token预算和截止时间。累计观测、缓存/推理分项、响应身份、Billing和价格绑定复用已有规则。Cost Report v2和Coding Eval Campaign汇总所有用途。

摘要Provider必须先产生`ModelAttemptStarted`。Runtime提交`CompactionAttemptStarted`后才继续消费Provider流，从而让兼容Adapter把真实HTTP放在持久意图之后。首事件缺失、错误或首事件前异常以`provider_summary_accounting_required`失败，并保守标记未记账请求风险。

每个Compaction至多一个Attempt。请求只允许一个非空文本块、完整且一致的用量和成功终态；工具集合为空，任何Tool Call均拒绝且不执行。第二个Attempt在Provider继续第二次HTTP之前被拒绝。已经发生的用量始终结算，摘要失败不回退费用。

### 2.4 候选与窗口发布

~~~text
PREPARING_CONTEXT
  → 验证全部来源Artifact
  → 生成并提交CompactionPlan
  → 提交摘要Attempt意图
  → 消费、累计和结算Provider流
  → 重算Summary候选并提交CompactionSummarized
  → 紧邻提交CompactionWindowActivated
  → 从活动窗口准备Model History Inspection v2
  → Context规划与普通模型请求
~~~

候选和窗口是两个不同事实。候选提交要求请求成功结算、来源和策略可重算、投影在预算内且达到最小缩减。窗口发布要求候选结束事件仍是Thread尾部，并由Reducer按同一来源、候选、事件序号和时间完整重算。窗口发布使用Session sequence CAS。

候选提交后到窗口提交之间使用屏蔽外部Task取消的本地任务。进程退出时，已提交候选由首次重开纯计算发布为唯一窗口，不调用Provider或工具。窗口事务在Event或Projection后退出会整体回滚；commit后退出保留完整窗口，重开不重复发布。

### 2.5 轮前与reactive触发

轮前触发只在宿主同时配置`CompactionRuntimeConfig`和摘要Provider时启用。触发阈值必须严格高于目标窗口预算，避免压缩后立即再次触发。

reactive路径只接受已记账的`provider_context_overflow`。当前步骤全部Attempt必须结算，且不得已产生完成响应、Tool Call、Tool Result或其他语义Item。失败请求仍消耗普通model_step及已报告Token；下一步骤强制压缩。压缩后再次溢出而没有新闭合前缀时返回`context_compaction_no_progress`，不发起第二次付费摘要。其他Provider失败不自动重跑。

### 2.6 信任、约束与Artifact

摘要来源固定为`trust=untrusted_history/authority=none`，摘要投影固定为`trust=derived_history/authority=none`。摘要文字不能替代Runtime/User指令、Approval、Effect Journal或Process核对。

首条/当前用户消息和显式锚点逐字保留。目标、约束、未完成事项、文件/revision、测试结果和不确定效果通过版本化人工语义Oracle持续评测。Oracle报告只保存身份、摘要和布尔结果，不保存正文，也不充当运行时权限。

被覆盖来源中的Artifact也必须在摘要HTTP前完成Workspace scope、TTL、manifest、正文和覆盖验证；不能通过压缩隐藏过期、损坏或跨scope引用。

## 3. 失败与恢复

- 计划前失败：无Compaction事实，保留旧窗口；
- 已计划但未开始请求：恢复为Interrupted，不调用Provider；
- 请求意图后、结算前退出：结算已知Usage并转Interrupted，不重发；
- Attempt成功但候选前退出：不从内存补造候选；
- 候选已提交但窗口前退出：重开发布唯一窗口，零Provider请求；
- 取消或超时：关闭Provider流，结算开放Attempt和Compaction，不留后台任务；
- 非法摘要、超限、无缩减或语义评测失败：保留原事实和费用，不把候选解释为授权。

## 4. 数据版本

- 独立摘要账本：Agent Event/Thread v14、Session migration16；
- 活动窗口和Model History Inspection v2：Agent Event/Thread v15、Session migration17；
- 新增独立Schema：Compaction Runtime v1、Compaction Window v1、Compaction语义评测Case/Report v1；
- v1-v14 Agent Schema、Model History Inspection v1、Provider Event v3和Cost Report v1/v2保持冻结。

migration17只标记最低reader，不重写旧事件、投影正文或Artifact。真实独立wheel验证以`b20948e`的v14为旧基线：v15初始化仅追加migration17，原字节不变；v14已结算候选在v15重开时零请求发布唯一窗口；随后v14 reader以`schema_too_new`拒绝且不修改数据库。

## 5. 未采用方案

- 删除旧Event或覆写Thread.items：破坏唯一事实源和Replay；
- 无账本摘要调用：费用、取消和恢复不可审计；
- 将Summary写成系统或用户指令：提升不可信来源权限；
- 切开Tool Call与Result：破坏Provider配对和效果解释；
- 摘要失败自动重试：没有进展证明且成本无界；
- 将候选直接视为活动窗口：缺少发布CAS和重开边界；
- 对所有Provider失败执行压缩重试：会掩盖错误并重复有成本请求。

## 6. 验收

已完成以下本地门禁：

1. 闭合组、预算、来源、固定锚点、候选和活动窗口契约；
2. Compaction账本、全用途Token/Cost/Campaign和失败结算；
3. OpenAI Chat及Anthropic无工具摘要映射；
4. 轮前/reactive触发、连续溢出、部分语义输出拒绝；
5. Artifact损坏、取消、超时、流关闭和摘要重试阻断；
6. 重复窗口、六个运行时退出点和三个SQLite窗口事务退出点；
7. 六类关键工程语义保持及缺失、幻觉、无效语料反例；
8. 真实v14→v15独立wheel升级和旧reader拒绝；
9. 370项专项测试及严格全量2901 passed、2 skipped。

两个skip为本地未配置PostgreSQL。远端Python 3.12、Python 3.13、macOS和PostgreSQL矩阵以对应实现提交CI为准。
