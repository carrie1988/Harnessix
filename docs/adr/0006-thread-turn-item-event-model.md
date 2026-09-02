# ADR 0006：采用 Thread、Turn、Item 与 AgentEvent 领域模型

- 状态：已接受
- 日期：2026-09-02

## 背景

Harnessix 0.1 只有 Action Plane 的 Action 与 Effect Journal。完整 Coding Agent 还需要表达多轮会话、一次用户意图中的多次模型步骤、流式文本、Tool Call/Result、审批、压缩、取消和恢复。

直接使用某个 Provider 的 Message/Content Block 会导致：

- 切换 Provider 时重写 Session；
- 网络 Chunk 和持久化事实混杂；
- 客户端依赖供应商私有事件；
- 无法稳定表达审批、Context Compaction 和 Workspace 变化；
- 恢复时难以区分“已发生的语义事实”和“进程内任务”。

源码研究见[Session 模型研究](../research/session-model.md)。

## 决策

### 1. Thread 是会话聚合根

Thread 表示一个可恢复、可分叉、可归档的 Coding 会话，拥有：

- 稳定 UUIDv7；
- Workspace 身份和配置快照；
- 父 Thread 关系；
- 当前 active Turn；
- Thread 内单调事件序列；
- 创建、更新和归档时间。

同一 Thread 同时最多存在一个活跃主 Turn。

### 2. Turn 是用户意图边界

Turn 从用户输入被持久接受开始，到 `COMPLETED`、`FAILED`、`CANCELLED` 或 `INTERRUPTED` 结束。一个 Turn 可包含多个 Model Step 和多个 Tool Call。

Model Step 首版作为内部诊断对象，不作为顶级公共聚合，避免把 Provider 实现细节固化到协议。

### 3. Item 是语义单元

v1 Item 类型：

- user_message；
- assistant_message；
- reasoning_summary；
- tool_call；
- tool_result；
- approval_request；
- plan；
- context_compaction；
- error。

Item 生命周期为 `STARTED → DELTA* → COMPLETED|FAILED|CANCELLED`。Delta 默认是可丢弃直播信息；Completed 的终值是可重放事实。

不持久化或公开模型私有原始思维链，只允许 Provider 明确提供的可公开 Reasoning Summary。

### 4. AgentEvent 是追加式事实

Thread 内每个持久事件具有严格单调 `sequence`，事件至少包含 event/thread/turn/item ID、类型、Schema 版本、时间、载荷、因果与 Trace 标识。

约束：

- `event_id` 全局唯一；
- `(thread_id, sequence)` 唯一；
- 相同 event_id + 相同载荷幂等；
- 相同 event_id 或 sequence + 不同载荷冲突；
- Event 与物化投影在一个事务提交。

### 5. 分离四种表示

| 表示 | 用途 |
|---|---|
| AgentEvent | 不可变恢复事实 |
| Thread/Turn/Item Snapshot | 查询与客户端状态 |
| Model View | Context Engine 生成的供应商中立历史 |
| Run State | Provider Stream、Task、Cancel Token 等进程内状态 |

Run State 不能作为崩溃后的事实；Provider 原始事件不能成为公共协议。

### 6. Resume、Fork 与 Retry

- Resume 继续同一 Thread；
- Fork 从明确边界创建新 Thread 并记录 parent；
- Turn Retry 创建新 Turn 并引用原 Turn，不重开原终态；
- Archive 不删除事件；
- v1 不支持从执行中的 Tool 中间状态直接 Fork。

## 不变量

1. 终态 Turn 不被普通事件重新打开；
2. Tool Result 引用唯一且已存在的 Tool Call；
3. Context Compaction 新建 Item，不删除原 Event；
4. Snapshot 可以从 Event Log 重建；
5. Live Delta 丢失不影响终态恢复；
6. Provider 切换通过语义 Item 重建，不复用供应商原始消息。

## 结果

### 正向结果

- Runtime、Provider、Protocol 和 Store 解耦；
- 可实现确定性 Transcript Replay；
- 客户端拥有稳定领域语义；
- Compaction、审批和恢复成为一等对象；
- 为后续 PostgreSQL 或远端 App Server 保留清晰边界。

### 成本

- Event、Snapshot 和 Model View 之间需要映射；
- Schema 演进和投影重建需要额外测试；
- Delta 与终值的合并逻辑必须严格；
- 多一层领域模型会增加 0.3 初期代码量。

## 被否决方案

### 直接保存 Provider Messages

供应商耦合过强，无法稳定表达非模型语义。

### 只保存最终 Transcript

不能恢复审批、执行中 Tool 和取消状态，也无法做可靠审计。

### 每个 Token 都写数据库

写放大、数据库膨胀和重放成本高；完整 Item 终值足以恢复。
