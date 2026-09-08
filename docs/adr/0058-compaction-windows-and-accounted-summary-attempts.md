# ADR 0058：可审计压缩窗口与独立摘要尝试

- 状态：Proposed
- 日期：2026-09-08
- 范围：0.6.3
- 前置：ADR 0054、ADR 0057、现有Model Attempt/Billing与Session恢复契约
- 源码依据：[Compaction与长会话窗口研究](../research/compaction-and-context-windows.md)

## 1. 问题与边界

单结果裁剪不能控制累计历史长度。直接覆盖Thread.items、把摘要器藏在Context Planner中、或者失败后反复调用摘要器，分别会破坏审计、漏记费用、放大成本。0.6.3必须交付一个完整的长会话窗口流程，而不是增加一个“生成摘要”函数。

本ADR提出待完整契约与反例测试确认的运行时设计。首窗口规划及候选校验已实现为独立、无副作用的v1领域契约，详见[窗口规划设计](../compaction-window-planning.md)；摘要账本和活动窗口尚未写入当前运行时。Event/Thread仍为v13，不能把下述运行时候选类型当作已发布API。

## 2. 拟采用设计

### 2.1 独立活动窗口，不改事实历史

Thread拟增加活动模型窗口身份，窗口由首条原用户消息、版本化Summary、按原顺序保留的固定组和近期后缀构成。保留首条原用户消息用于满足既有Anthropic端口的user起始约束，不把Summary伪装为用户或系统指令。原Event、Item、Approval、Effect、Artifact和工具视图决定保持不变。

窗口发布记录至少包含：

- `window_id`、`previous_window_id`和所属Thread/Turn；
- 来源事件序号、覆盖的原Item ID及来源规范摘要；
- 有序保留Item ID、选择算法版本和预算策略；
- Summary版本、内容摘要、派生来源、稳定投影Item ID；
- 生成Summary的Compaction及Attempt身份；
- 变换前后输入估算、验证结果和生效模型步骤。

后续模型请求只能使用已提交的活动窗口。历史检查记录应升级为带窗口身份的新版本；v13及更早事件继续按当时全历史规则Replay，不能由新的窗口算法重解释旧决定。

### 2.2 闭合组选择

窗口选择从真实完成历史建立保守响应组：连续助手块与该响应的全部Tool Call、全部完成Tool Result不拆组。Item本身没有model_step，因此不猜测相邻纯文本的响应边界。保留最近的完整组、首条/当前用户消息及明确固定的任务约束；其余闭合前缀扣除固定组后作为摘要来源。固定组参与预算，不能默默丢弃。

以下状态禁止压缩或发布：

- 有未完成Item、未结算模型尝试；
- 有等待审批或等待Action；
- 调用/结果身份重复、缺失或顺序不合法；
- 来源不属于当前活动窗口或在候选生成期间发生变化。

不通过截取字符串修复非法边界；没有可压缩前缀或保留集本身超限时明确失败。

### 2.3 摘要请求显式记账

新增Compaction运行记录及其尝试事件封装，复用`UsageObservation`、`ResponseBillingMetadata`、`ModelAttempt`和`estimate_attempt`的已有数据/计费语义，但不冒充普通CALLING_MODEL步骤。

具体原因：当前`reducer._model_attempt`约束尝试只属于正在打开的普通步骤，`build_cost_report`只遍历`turn.model_attempts`。仅在PREPARING_CONTEXT发起Provider请求会绕过两者；直接增加普通step又会伪造一次交付模型响应。

拟采用独立Compaction Attempt集合，Attempt ID在整个Thread唯一；普通与摘要集合共享Turn Token总预算和截止时间，累计观测按增量入账，完整性、缓存分项、响应身份和价格绑定仍沿用原规则。Cost Report、Eval与诊断必须同时汇总两类尝试，并明确用途，不能只修改核心Runtime而漏掉报告层。

摘要Provider复用供应商中立流端口，工具定义为空；出现Tool Call、拒绝、无有效正文或异常终态时拒绝候选。已发生用量依然结算。默认不自动重试摘要请求，禁止摘要器递归触发Compaction。

### 2.4 原子发布与恢复

流程为：

~~~text
PREPARING_CONTEXT
  → 选择闭合前缀与保留集
  → 持久化Compaction计划及预算边界
  → 开始摘要Attempt并持久化累计用量
  → 结算Attempt
  → 校验Summary结构、来源、关键约束与缩减效果
  → CAS原子发布新窗口
  → 新窗口的Tool Result/Artifact检查
  → Context规划
  → 普通模型请求
~~~

生成失败、校验失败、取消或超时不发布窗口；原窗口不变，但尝试费用和失败事实保留。发布事务必须比较计划来源与当前活动窗口身份，不接受仅凭内存中的候选直接替换。

进程在请求开始后到结算前退出，恢复为Interrupted/用量不完整，不自动重新计费发起请求。结算后、窗口发布前退出也不猜测候选可以恢复；没有持久候选证据时仅保留已结算尝试。发布后退出恢复已提交窗口，不重新摘要，也不执行任何工具。

### 2.5 轮前与reactive触发分离

轮前触发使用宿主显式配置的阈值、输出预留和安全余量。首版默认不启用；配置、诊断和用量路径验收后再用于自动运行。

reactive路径仅允许明确的Provider context-overflow错误，且本次普通请求没有产生可用回答或Tool Call、所有尝试均已结算。压缩成功后进入新的普通model_step，因此max_steps仍计算失败请求；不能无限复用同一个步骤规避预算。每个失败步骤最多一次reactive压缩，来源或窗口没有进展时立即停止。

这一分支需要正式新增状态转移前置条件；不得通过捕获所有Provider异常并自动重跑来实现。

### 2.6 摘要信任、约束与Artifact

Summary是从不可信历史派生的数据，不得升级为Runtime/User指令或执行授权。持久化“已批准/已成功”的文字不能取代原Approval、Effect或Process核对。Runtime/User Fragment和当前用户输入继续独立进入Context。

必须区分：

- 逐字固定项：宿主明确提供、带来源的约束锚点及必须保留的原消息；
- 语义保持项：目标、未完成事项、决定、文件/revision、测试结果和不确定效果；
- 旧Artifact审计引用：不能在不可回读时继续向模型承诺完整证据可取回。

任何压缩流程都不能绕过当前Workspace访问策略。活动窗口只校验实际进入模型的引用，但被覆盖来源的摘要读取仍须遵守授权范围；跨scope失败不能靠压缩“洗掉”。首窗口规划保留整个来源历史的Artifact验证义务，运行时必须在摘要请求之前完成现有scope/TTL/正文验证，不能仅检查保留后缀。过期归档的后续宽限策略尚未启用，不能静默当作有效Artifact。

## 3. 数据版本方案

拟新增Agent Event/Thread v14、Model History Inspection v2和Session migration16；具体Schema在领域契约评审后冻结。v1-v13 Agent Schema、既有Provider Event和计费元数据保持原版本文件不变。新增尝试事件采用包装而不是就地改变既有Provider Event v3字段，从而避免无关供应商映射契约升级。

旧v13投影升级不自动生成Summary或计费请求；只有显式启用Compaction的后续运行才能产生新窗口。旧reader按最低迁移标记拒绝接管。未来Fork需要重新验证窗口来源归属，不继承效果执行许可。

## 4. 未采用方案

- 删除旧Event或覆写Thread.items：破坏唯一事实源和Replay。
- 无账本摘要调用：费用、取消和恢复不可审计。
- 把Summary写成系统指令：提升不可信来源的权限。
- 切开Tool Call与Result：破坏Provider配对和效果解释。
- 摘要失败无限重试：没有进展证明且成本无界。
- Summary宣称缩减即激活：必须用实际候选窗口重新计量、校验来源并CAS发布。

## 5. 接受前门禁与实施顺序

1. 窗口/闭合组规划和预算分摊的领域契约，明确每个有界集合、计量算法及失败代码。
2. Compaction尝试账本、Token增量、Cost Report/Eval覆盖和中断恢复反例测试。
3. 显式启用的端到端压缩路径：两种Provider、无工具摘要、候选验证和原子窗口发布。
4. 轮前与reactive触发，验证空前缀、无缩减、连续失败、超限摘要和预算耗尽。
5. 关键约束保持、重复压缩、长任务、Workspace变化与旧wheel升级验证。

任何一项缺少取消、超时、恢复、安全边界或完整回归，都不得将0.6.3标记完成。该ADR在上述契约反例评审完成前保持Proposed。

## 6. 已实现的内部门禁

首窗口`CompactionPolicy/Anchor/Plan/Summary v1`及`plan_compaction/validate_compaction`已落地，完成闭合组、预算、来源变化、固定项、取消/超时、计划JSON往返和实际SQLite Session验证。低信任摘要位于首条原用户之后，两个现有Adapter的请求映射均纳入回归。

该实现不调用Provider、不写Session、不发布窗口。仅凭内存候选无法证明尝试结算、语义保持或付费请求崩溃恢复；独立账本与发布门禁仍须按第5节推进，不将本ADR提前改为Accepted。

账本候选事件、Cost Report v2、Campaign聚合和崩溃窗口已细化为[摘要尝试账本与窗口发布设计草案](../compaction-attempt-ledger.md)。该文明确现有接口约束和未实施边界，不作为已发布Schema。
