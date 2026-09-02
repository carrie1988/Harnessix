# Agent Loop 研究与 Harnessix 状态机

## 1. 研究基线

见[源码研究基线](baselines.md)。本主题研究 Codex `run_turn`、OpenCode V2 Session Runner 和 Claude Code 逆向仓库中的 Query Loop。

## 2. 用户可观察行为

一个 Coding Turn 不等于一次模型请求。用户提交一次意图后，Runtime 可能经历多轮“模型输出 Tool Call—执行工具—回送结果”，直到最终回答、失败、取消或预算耗尽。

用户应能观察：

- Turn 已接受、正在生成、正在执行工具、等待审批和终态；
- 文本与工具进度可以流式显示；
- 取消必须传播到模型流、工具和进程树；
- 进程崩溃后不能伪装成仍在运行，也不能盲目重复副作用；
- 模型重试与工具重试必须是两套不同规则。

## 3. 参考实现事实

### 3.1 Codex

**事实**

1. `run_turn` 在采样前检查上下文压缩，记录用户输入，再进入外层采样循环；
2. 每轮采样都从 Context Manager 复制并规范化历史，构造 Prompt 后调用 `run_sampling_request`；
3. 模型 Item 完成时先写入历史，再把 Tool Call 放入并发任务队列。这样即使工具阶段被取消，Call/Result 的恢复边界仍可解释；
4. 模型显式要求继续或队列中出现新用户输入时，外层循环进入下一次采样；
5. 上下文溢出和用量限制不会走普通传输重试；可重试网络错误由采样层处理；
6. 取消会产生明确的 TurnAborted 路径；流提前结束、无效图片状态和其他模型错误有独立失败分支；
7. Stop Hook 可以阻止结束并要求 Agent 继续，说明“模型结束”不必然等于“Turn 完成”。

**推断**

Codex 的主循环把“模型步骤”和“Turn”分开，并把历史一致性放在工具调度之前。它偏向先维护可恢复 Transcript，再执行可能失败的效果。

### 3.2 OpenCode

**事实**

1. V2 Runner 在启动时把遗留的 pending/running Tool 状态修正为失败，避免恢复后永远悬挂；
2. Provider 流被归一化为事件，Tool Call 先进入事件/投影，再由 Fiber 执行；
3. Tool 完成后重新加载历史并继续下一模型步骤；
4. Compaction 在模型请求前检查，上下文溢出后也有恢复分支；
5. 取消会中断 Provider 流并清理 Tool Fibers；
6. 当前文件顶部明确列出 durability、retry 和 cleanup 等未完成项。

**推断**

OpenCode V2 正在从以进程内消息为主的循环转向事件驱动 Runner。其“事件先行 + 投影”方向适合参考，但当前提交不能直接作为完整恢复语义的证明。

### 3.3 Claude Code 逆向仓库

**事实，仅作行为佐证**

1. Query Loop 在模型请求前执行 Tool 输出裁剪、微压缩和上下文折叠；
2. 观察到 Tool Use 后进入后续步骤；异常时补齐缺失 Tool Result，维持消息配对；
3. 流被取消时先排空或合成必要结果，再结束当前循环；
4. Prompt 过长、最大输出、Stop Hook 和 Token Budget 有不同恢复/终止路径；
5. QueryEngine 在进入循环前持久化用户消息，并在迭代过程中记录 Assistant、Tool 和压缩边界。

**推断**

其可观察行为同样表明：生产级 Agent Loop 是预算、Context、Tool 和取消共同驱动的状态机，不是一个 `while tool_calls` 示例。

## 4. 共同机制与关键差异

### 共同机制

- 用户输入在模型调用前进入会话事实；
- 模型步骤可以产生文本、推理摘要、Tool Call、用量和停止原因；
- Tool Call/Result 必须严格配对；
- Context 溢出与网络重试分开处理；
- 取消需要结构化传播；
- Loop 必须有预算或步数上限。

### 关键差异

| 维度 | Codex | OpenCode V2 | Claude 逆向仓库 |
|---|---|---|---|
| 主协调模型 | Rust async Loop + 有序 Tool Futures | Effect/Fiber + 事件投影 | AsyncGenerator Query Loop |
| 持久边界 | Rollout/Context 历史先行 | Durable Event + Projector | Transcript/QueryEngine |
| 继续条件 | 模型 follow-up 或待处理用户输入 | Tool 结算后重新装载历史 | 观察到 Tool Use、Hook 或预算恢复 |
| 当前证据成熟度 | 高 | 迁移中 | 非官方、仅佐证 |

差异主要来自运行语言、客户端形态和持久化模型，而不是 Agent Loop 的基本闭环。

## 5. Harnessix Code 决策

采用以下确定性状态机：

~~~text
ACCEPTED
  → PREPARING_CONTEXT
  → CALLING_MODEL
  → APPLYING_MODEL_EVENTS
  ├─→ WAITING_APPROVAL → EXECUTING_TOOLS ─┐
  ├─→ EXECUTING_TOOLS ────────────────────┤
  ├─→ COMPACTING_CONTEXT ─────────────────┤
  └─→ FINALIZING → COMPLETED              │
                                          └→ PREPARING_CONTEXT

任意非终态 → CANCELLING → CANCELLED | INTERRUPTED
任意非终态 → FAILED
~~~

核心规则：

1. 创建 Turn 和 UserMessage 的事务提交成功后，才允许发起 Provider 请求；
2. Provider 事件先归一化，Runtime 不消费供应商原始对象；
3. Tool Call Item 必须持久化完成后才可执行；
4. Tool Result 必须持久化完成后才可发起依赖它的下一模型步骤；
5. 同一 Thread 同时最多一个活跃主 Turn；
6. Turn Cancel Token 向 Provider、Tool Runtime、Process Runtime 分层传播；
7. Provider 尝试只有在尚未提交语义终值或 Tool Call 时才可自动重试；已经直播的 Delta 必须先结束为 failed Item，新尝试不得续写原 Item；
8. Tool 是否重试由副作用分类、幂等性和对账能力决定，不能继承 Provider 重试策略；
9. 到达最大步骤、Token、成本或时间预算时，以结构化终态结束；
10. 进程崩溃后，正在进行的 Provider 请求首版统一转为 INTERRUPTED，不自动重放。

## 6. 失败语义

| 失败点 | 持久事实 | 恢复动作 |
|---|---|---|
| UserMessage 提交前失败 | 无新 Turn | 客户端可用幂等键重试 |
| Provider 建连失败且无输出 | Turn/Step 已开始 | 按 Provider 错误分类有限重试 |
| Provider 已输出部分文本后断流 | 已完成语义 Item 保留 | 当前 Step 失败或 Interrupted，不拼接未知续流 |
| Tool Call 已记录、执行前崩溃 | Tool Call 存在、无 Result | 按 Tool 效果分类恢复 |
| 本地写执行中崩溃 | 结果未知 | 检查 Workspace pre/post 证据，不盲目重放 |
| 外部写执行中崩溃 | Action 可能 UNKNOWN | 进入 Action Plane 对账 |
| 等待审批时重启 | Approval 持久存在 | 恢复等待，不重新请求同一效果 |
| 用户取消 | CancelRequested 已记录 | 停止接收新 Tool Call，清理后写终态 |

## 7. 运行时不变量与测试

- 同一 Turn 只有一个主 Loop Owner；
- 终态不可被普通事件重新打开；
- Tool Result 引用唯一、已存在的 Tool Call；
- CancelRequested 之后不再调度新工具；
- 相同 Scripted Provider Transcript 产生相同的持久事件序列；
- 在每个事务边界注入进程退出，恢复结果必须落入可解释状态；
- 对 Provider 流、审批等待、只读工具、本地写工具和外部 Action 分别测试取消。

这些决策由 [ADR 0007](../adr/0007-agent-loop-and-cancellation.md) 固化。

## 8. 源码索引

- Codex：[session/turn.rs](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/core/src/session/turn.rs)、[stream_events_utils.rs](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/core/src/stream_events_utils.rs)
- OpenCode：[session/runner/llm.ts](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/session/runner/llm.ts)
- Claude 逆向仓库：[query.ts](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/query.ts)、[QueryEngine.ts](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/QueryEngine.ts)
