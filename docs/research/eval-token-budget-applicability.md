# Coding Eval Token预算适用性源码研究

- 日期：2026-09-06
- Harnessix问题：首轮真实Campaign的三个run均在第5个模型步骤后超过任务v1的20000累计Token预算
- 固定参考提交：Codex `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67`、OpenCode `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5`、Claude Code本地研究样本 `2ca5ddabfed5f220812ea11f029eda03b21bc4c1`

## 1. 待求证问题

首轮真实Campaign已经证明Provider、Function Calling、Usage、模型身份和费用聚合链路可运行，但0/3均为预算失败。需要先回答：

1. 主流Coding Agent是否把“当前请求的上下文窗口”“一次响应输出上限”和“一个任务的累计消费预算”视为同一概念；
2. Harnessix `Budget.max_tokens`当前究竟约束什么，是否应为避免失败而改成上下文窗口；
3. 应修改既有任务、修改Runtime，还是发布新任务版本；
4. 如何保留预算达到与超过临界点时的可解释证据。

## 2. Codex源码事实

`codex-rs/core/src/session/context_window.rs:7-23,57-120`同时维护：

- 当前活跃上下文Token；
- 自动压缩作用域已使用Token及其限制；
- 模型完整上下文硬限制；
- 按限制饱和扣减后的剩余Token；
- 自动压缩阈值与完整窗口是否到达。

这表明上下文窗口压力和自动压缩阈值是**当前上下文状态**，不是把一次任务内每个请求的输入重复累加后得到的消费总量。

`codex-rs/core/src/tasks/mod.rs:686-760`在Turn结束时，用会话总Usage减去Turn开始快照，分别记录输入、缓存输入、缓存写入、输出、推理输出和总Token，并发送Turn级遥测。上下文控制与Turn累计Usage在代码路径和用途上分离。

## 3. OpenCode源码事实

`packages/opencode/src/session/overflow.ts:8-34`先从模型输入/上下文限制中预留输出或压缩缓冲，再用**最近一次响应**的总Token判断是否需要压缩。该判断服务于上下文可用性，不是任务费用上限。

`packages/opencode/src/session/session.ts:361-404`把输入、输出、推理、缓存读写Token归一化后独立计算成本；上下文阶梯价格选择也与溢出判断分开。重试和压缩另有控制流，不能由累计Token预算隐式替代。

## 4. Claude Code本地研究样本事实

`src/services/compact/autoCompact.ts:28-145`从模型上下文窗口扣除摘要输出预留，分别计算自动压缩、警告、错误和阻断阈值，并输出剩余比例状态。

`src/QueryEngine.ts:971-1001`则在每条消息处理后独立检查累计USD金额；到达上限时返回`error_max_budget_usd`，同时保留`total_cost_usd`和Usage。费用预算可能在一个已完成响应后才发现达到，随后停止继续执行。

该样本只作为本地架构研究输入，不作为上游官方兼容承诺。

## 5. Harnessix现状与真实数据

Harnessix当前有三个不同边界：

| 边界 | 当前契约 | 语义 |
|---|---|---|
| 单响应输出 | Provider配置`max_output_tokens`与`Budget.max_output_chars` | 限制一次模型输出和持久文本大小 |
| Turn累计Token | `Budget.max_tokens` | 对同一Turn所有Provider已报告输入、输出Token累计计数 |
| Campaign累计费用 | `fee_stop_amount` | 完整试验之间按可重算已知成本停止新试验 |

首轮百炼三个run的第5步终态累计Token分别为21460、21429、21567；全部Usage完整，均已超过任务v1的20000上限。Runtime在请求前只能知道既有累计量，并把剩余额度传给Provider用于约束输出；下一请求的输入长度由完整历史、工具Schema和供应商计数共同决定，响应前不能得到权威值。因此终态可能超过上限，随后在工具调度前以`budget_exceeded`停止。这是保守的副作用边界，不是记账错误。

每次第5步模型都请求读取目标实现文件，但预算门禁先于工具执行；三个工作区无修改。该0/3只能说明任务v1预算不适配，不能用于估计模型修复成功率。

## 6. 决策输入

### 6.1 不修改Runtime语义

把`Budget.max_tokens`改成“最近一次上下文大小”会破坏既有累计消费门禁、尝试账本和恢复语义，也会把0.6才实现的Context Engine提前塞入0.5 Eval。Runtime继续执行以下规则：

- 请求前已知累计量达到上限，不发起下一模型尝试；
- 响应后累计量超过上限，保留真实Usage并禁止调度工具；
- 达到上限恰好完成最终回答可以完成Turn；若仍有工具调用，则禁止执行；
- 未知Usage不能伪造为零。

### 6.2 任务升级为v2

同一任务来源、Prompt、允许路径和检查集合保持不变，仅将累计Token预算从20000提高到100000。选择100000的理由是：

1. 与Kernel默认正式`Budget.max_tokens`一致，不发明第四套隐式默认值；
2. 是首轮目标文件读取前实际累计上界21567的4.6倍以上，可消除已证实的错误截断，而不是只贴近单次样本调参；
3. 仍保留16步、600秒、单步输出4096及Campaign费用停止等独立硬边界；
4. 真实质量与成本仍必须由新Campaign验证，100000不是成功保证。

v1必须永久保留，既有Campaign按`task_id + task_version + fingerprint`精确恢复；无版本查询只返回最新v2。不能改写v1指纹、报告或已完成Campaign。

### 6.3 临界证据

不新增重复持久化字段。原始事实已由正式契约保存：

- `Turn.budget`、`Turn.usage`、`Turn.model_attempts`和失败分类保存在Session；
- `CodingEvalReport.metrics`保留实际输入/输出和步骤数，`budget_respected`按任务版本预算评分；
- Campaign单次摘要保留实际输入/输出、Turn状态及`budget`主分类。

离线回归同时覆盖“恰好等于预算”和“超过预算一个Token”：前者通过预算检查，后者失败但仍保留141个实际Token，不能截成140或清零。Campaign还必须证明v1和v2均可按计划版本独立执行、重开和核对。

## 7. 结论与后续门禁

0.5.5c3a应只做版本化任务Catalog、精确版本恢复、临界回归和文档同步，不改Agent/Provider/Session/Campaign Schema，不调用真实API。0.5.5c3b必须创建新Campaign并取得新的费用与请求次数授权；只有新基线不再被错误预算截断，才可判断任务成功率并进入0.5.5d。
