# Session、Thread、Turn、Item 与事件模型研究

## 1. 研究基线

见[源码研究基线](baselines.md)。本主题关注“什么是会话事实”“如何重建状态”以及“断线与崩溃后哪些内容必须保留”。

## 2. 领域边界

Harnessix 使用 Session 作为持久化能力的统称，公共领域对象采用：

- **Thread**：一个可恢复、可分叉、可归档的 Coding 对话聚合；
- **Turn**：一次已接受的用户意图到终态的执行边界；
- **Item**：客户端可观察的语义单元；
- **AgentEvent**：驱动聚合状态变化的追加式事实；
- **Run State**：只在进程内存在的 Provider 流、任务句柄和 Cancel Token。

Provider Message、供应商 Content Block 和网络 Chunk 都不是公共领域对象。

## 3. 参考实现事实

### 3.1 Codex

**事实**

- App Server 协议直接定义 Thread、Turn 和 ThreadItem；
- Thread 保存会话标识、父/分叉关系、工作目录、模型、时间和状态等元数据；
- Turn 保存 Items、状态、错误和开始/完成信息；
- Item 覆盖用户消息、Agent 消息、Reasoning、命令、文件变更、MCP 调用、动态工具和 Context Compaction；
- Thread 状态区分未加载、空闲、系统错误和 Active，并能表达等待审批/输入；
- Rollout 采用追加记录；重建逻辑反向查找最近有效 Compaction，再合并其后的活动片段；
- Context Manager 会补齐 Call/Output 配对并移除孤儿输出，说明持久记录与送模历史需要不同视图；
- SQLite 保存 Thread 元数据和索引，但 Rollout 文件仍承担 Transcript 重建事实。

**推断**

Codex 将“客户端语义模型”和“模型历史”明确分开。恢复不是简单反序列化一份 messages，而是根据边界事件和压缩记录重建。

### 3.2 OpenCode

**事实**

- Session Info 包含父会话、项目、Agent/模型、成本、Token、位置和时间；
- User/Assistant/Tool/Compaction 均有显式 Schema；
- Tool 状态区分 pending、running、completed 和 error；
- V2 Event 为每个聚合分配单调 sequence，Event 提交与 Projector 更新位于同一数据库事务；
- Durable Event 不包含每个文本 Delta；Ended 事件保存可重放的完整值；
- Projector 将事件物化为 Session、Message 和 Part 查询表；
- 新 Turn 会把陈旧未完成的 Assistant 状态标记为 superseded，避免恢复后误认为仍活跃。

**推断**

OpenCode V2 展示了适合 Harnessix 的“事件事实 + 查询投影”结构，也暴露了迁移期必须处理的旧状态清理问题。

### 3.3 Claude Code 逆向仓库

**事实，仅作行为佐证**

- QueryEngine 在调用 Loop 前记录用户消息；
- 迭代期间记录 Assistant、Tool 和 Compact Boundary；
- Session History 支持分页读取远端历史。

由于仓库非官方且持久化依赖不完整，不据此推导事务、顺序或崩溃保证。

## 4. Harnessix 数据模型

### 4.1 Thread

最小字段：

| 字段 | 语义 |
|---|---|
| thread_id | UUIDv7，稳定聚合 ID |
| parent_thread_id | Fork 来源，可空 |
| workspace_id / root | 工作区身份与创建时根路径 |
| status | ACTIVE、IDLE、ARCHIVED、ERROR |
| active_turn_id | 当前主 Turn，可空 |
| configuration_snapshot | Agent、Provider、Policy 等非 Secret 配置快照 |
| event_sequence | 该 Thread 最后提交的单调序号 |
| created_at / updated_at | UTC 时间 |

### 4.2 Turn

状态集合：

~~~text
ACCEPTED
PREPARING_CONTEXT
CALLING_MODEL
EXECUTING_TOOLS
WAITING_APPROVAL
COMPACTING_CONTEXT
CANCELLING
COMPLETED | FAILED | CANCELLED | INTERRUPTED
~~~

一个 Turn 可以包含多个 Model Step。Step 首版是内部诊断对象，不提升为顶级公共聚合。

### 4.3 Item

首版 Item 类型：

- `user_message`；
- `assistant_message`；
- `reasoning_summary`，只保存可公开摘要，不保存私有原始思维链；
- `tool_call`；
- `tool_result`；
- `approval_request`；
- `plan`；
- `context_compaction`；
- `error`。

Item 生命周期统一为：

~~~text
STARTED → DELTA* → COMPLETED
                   FAILED
                   CANCELLED
~~~

Delta 可丢弃；Item 终值是可重放事实。

### 4.4 AgentEvent

每个事件至少包含：

- `event_id`：全局唯一 UUIDv7；
- `thread_id`、`turn_id`、可选 `item_id`；
- `sequence`：Thread 内严格单调；
- `event_type`、`schema_version`；
- `occurred_at`；
- `payload`；
- `trace_id`、`causation_id`、`correlation_id`；
- `redaction_version`。

唯一约束：`(thread_id, sequence)` 和 `event_id`。

## 5. 事实、快照与流

| 数据 | 是否持久化 | 原因 |
|---|---|---|
| Turn/Item 开始与终态 | 是 | 恢复和审计事实 |
| 完整 Assistant/Reasoning Summary | 是 | 可重放 Transcript |
| Tool Call/Result | 是 | 配对、恢复和副作用证据 |
| Approval 请求/答复 | 是 | 崩溃恢复和授权证据 |
| Context Compaction | 是 | 模型历史重建 |
| Token Delta | 否，默认只直播 | 防止数据库无限增长 |
| Tool stdout 增量 | 否或有界合并 | 终值和 Artifact 引用足够 |
| 物化 Thread/Turn/Item | 是，可重建 | 快速查询 |
| Provider 原始载荷 | 默认否 | 稳定性和 Secret 风险 |

Event 与 Projector 在一个事务提交；客户端 Snapshot 由投影产生，Live Delta 不是恢复事实。

## 6. 恢复算法

1. 加载 Thread/Turn/Item 快照；
2. 校验投影的最后 sequence 与 Event Log；
3. 必要时从 Snapshot 起重放 Event；
4. 规范化 Tool Call/Result 配对；
5. 将进程内的 CALLING_MODEL 标记为 INTERRUPTED；
6. 对 running Tool 按 effect class 分类：
   - 只读且可证明安全：可创建显式 Recovery Attempt；
   - 本地写：先比对 pre/post hash、Git Diff 和 Artifact；
   - 外部写：委托 Action Plane 查询 UNKNOWN/Reconcile；
7. WAITING_APPROVAL 保持等待，复用原审批指纹；
8. 向客户端发布恢复后的权威 Snapshot，而不是伪造丢失 Delta。

## 7. Fork、Resume 与 Retry

- **Resume**：继续同一 Thread，保留事件序列；
- **Fork**：从明确的 completed Item/Turn 边界创建新 Thread，并记录 parent；
- **Turn Retry**：创建新 Turn，引用原 Turn；不修改原终态；
- **中途 Fork**：首版不支持从正在执行的 Tool 中间态克隆；
- **Archive**：仅改变可见性，不删除审计事实。

## 8. 失败语义与测试

- 重复 event_id 必须幂等返回原结果；
- sequence 缺口、倒序或同序不同载荷必须拒绝；
- Event 已提交但投影失败必须整体回滚；
- 投影损坏可以从 Event Log 重建；
- Tool Result 缺失、重复或引用错误在加载时进入结构化恢复错误；
- Compaction 不能删除原始事件，只替换“送模视图”；
- 对每一个事件提交边界执行 crash matrix；
- 同一 Event Transcript 重放得到相同 Snapshot 和客户端终态序列。

数据模型由 [ADR 0006](../adr/0006-thread-turn-item-event-model.md) 固化，存储与恢复由 [ADR 0010](../adr/0010-session-store-and-recovery.md) 固化。

## 9. 源码索引

- Codex：[thread_data.rs](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/app-server-protocol/src/protocol/v2/thread_data.rs)、[turn.rs](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/app-server-protocol/src/protocol/v2/turn.rs)、[item.rs](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/app-server-protocol/src/protocol/v2/item.rs)、[rollout_reconstruction.rs](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/core/src/session/rollout_reconstruction.rs)
- OpenCode：[session.ts](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/schema/src/session.ts)、[session-message.ts](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/schema/src/session-message.ts)、[session-event.ts](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/schema/src/session-event.ts)、[event.ts](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/event.ts)、[projector.ts](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/session/projector.ts)
- Claude 逆向仓库：[QueryEngine.ts](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/QueryEngine.ts)、[sessionHistory.ts](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/assistant/sessionHistory.ts)
