# ADR 0007：采用持久边界驱动的 Agent Loop 与分层取消

- 状态：已接受
- 日期：2026-09-02

## 背景

Coding Agent 的一个 Turn 通常包含多次模型调用和工具执行。简单的 `while tool_calls` 无法回答：

- 用户输入何时算已接受；
- Tool Call 在崩溃前后是否已经执行；
- Provider 断流能否重试；
- 取消如何传递到模型、工具和子进程；
- 何时压缩 Context；
- 预算耗尽或审批等待如何进入终态。

源码研究见[Agent Loop 研究](../research/agent-loop.md)。

## 决策

### 1. 状态机

Turn 使用以下执行状态：

~~~text
ACCEPTED
  → PREPARING_CONTEXT
  → CALLING_MODEL
  → EXECUTING_TOOLS | WAITING_APPROVAL | COMPACTING_CONTEXT
  → PREPARING_CONTEXT（需要下一模型步骤）
  → FINALIZING
  → COMPLETED

任意非终态 → CANCELLING → CANCELLED | INTERRUPTED
任意非终态 → FAILED
~~~

状态转换由领域命令和已提交 AgentEvent 驱动，不由 UI 文本或 Provider Stop Reason 直接决定。

### 2. 持久化顺序

严格顺序：

1. 原子提交 TurnStarted 与 UserMessage；
2. 发起 Provider 请求；
3. 将 Provider 流归一化为内部事件；
4. 完成 ToolCall Item 并提交；
5. 执行 Tool；
6. 完成 ToolResult Item 并提交；
7. 构造下一 Model View；
8. 最终 Assistant Item 提交后再提交 TurnCompleted。

任何依赖前一步事实的外部操作，都不得早于该事实提交。

### 3. 继续与终止条件

继续下一 Model Step 的条件：

- 当前 Step 产生已完成 Tool Call；
- Stop Hook 或显式 Runtime Policy 要求继续；
- Context Compaction 完成并需要重放当前意图；
- 运行中收到被允许合并的用户输入。

终止条件：

- 模型完成且无待处理 Tool/Hook；
- 达到步骤、Token、成本或墙钟预算；
- 不可恢复错误；
- 用户取消；
- 恢复安全性无法证明。

### 4. 分层 Cancel Token

~~~text
Turn Cancel Token
  ├── Provider Request Token
  ├── Context/Compaction Token
  └── Tool Call Token
        └── Process Group Token
~~~

Cancel 是幂等领域命令。收到后：

1. 持久化 CancelRequested；
2. 停止调度新 Model Step 和 Tool Call；
3. 触发所有子 Token；
4. 等待有界清理；
5. 根据效果事实写 CANCELLED、INTERRUPTED 或 UNKNOWN。

外部效果可能已提交时，不允许仅因用户取消就写 CANCELLED。

### 5. Retry 分层

Provider 自动重试仅在以下条件同时成立时允许：

- 错误被 Adapter 标记为 retryable；
- 当前 Provider Attempt 尚未提交语义 Item 终值或 Tool Call；
- 尚未把任何外部效果交给 Tool Runtime；
- 退避和最大尝试未超预算。

如果客户端已经收到 Live Delta，Runtime 必须先把该 Item 结束为 FAILED，再以新 Item/Attempt 重试；不能把新响应续写到旧 Item，避免形成无法重放的拼接内容。

Tool Retry 不复用 Provider Retry：

- PURE/READ_ONLY 可按显式策略重试；
- LOCAL_WRITE 先对比 Workspace 证据；
- 外部写由 Action Plane 根据幂等键或 Reconcile 决定；
- UNKNOWN 永不自动重放。

### 6. 恢复

进程重启时：

- CALLING_MODEL 转为 INTERRUPTED，不盲目重建旧网络请求；
- WAITING_APPROVAL 恢复原等待；
- EXECUTING_TOOLS 按 Effect Class 分类；
- 未完成 Delta 丢弃，以持久 Item 终值和 Snapshot 为准；
- 用户可从 INTERRUPTED 创建显式 Continue/Retry Turn。

## 不变量

1. 同一 Thread 最多一个主 Loop Owner；
2. CancelRequested 后不调度新 Tool；
3. Tool Call 在效果前持久化；
4. Tool Result 在下一 Provider 请求前持久化；
5. 终态只提交一次；
6. Retry 不跨越未知副作用边界。

## 结果

### 正向结果

- Agent Loop 可用 Fake Provider 确定性验证；
- 取消和恢复具有可解释终态；
- 模型网络错误不会意外重复 Tool；
- Action Plane 的 UNKNOWN 语义自然接入。

### 成本

- 需要精确的事务边界和失败注入；
- 流式 UI 与持久状态存在短暂差异；
- 不自动续接崩溃中的 Provider 请求，会牺牲少量便利性换取正确性。

## 被否决方案

### 进程重启后自动重放整个 Turn

无法证明已执行工具不会重复，风险不可接受。

### 只取消顶层 asyncio Task

不能保证 SDK 连接、工具任务和子进程树被清理。

### Provider 与 Tool 共用统一 Retry

两者副作用边界不同，会把传输重试错误地扩展到写操作。
