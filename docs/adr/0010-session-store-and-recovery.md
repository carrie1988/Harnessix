# ADR 0010：SQLite Event Log、事务投影与保守恢复

- 状态：已接受
- 日期：2026-09-02

## 背景

Coding Agent 需要在 CLI 退出、App Server 重启、Provider 断流和 Tool 执行中断后恢复到明确状态。只保存最终聊天记录无法判断：

- Tool 是否已经开始或产生效果；
- Approval 是否仍有效；
- Context 是否已经压缩；
- 客户端漏掉哪些事件；
- 哪些状态只是进程内假象。

同时，本地优先第一版不需要多实例数据库复杂度。

## 决策

### 1. SQLite 为 v1 Session Store

0.3 使用独立 SQLite Session Store，不与 Action Plane Effect Journal 混表。两者通过 thread/turn/item/action ID 关联。

启用：

- WAL；
- foreign keys；
- busy timeout；
- 显式 Schema version 和 migration；
- 单 Writer 事务边界；
- 启动时 integrity/migration 检查。

### 2. Event Log + Materialized Views

核心表职责：

| 表 | 职责 |
|---|---|
| agent_events | Thread 内追加事实 |
| threads | Thread 当前快照 |
| turns | Turn 当前快照 |
| items | Item 终值与查询字段 |
| approvals | 持久等待与答复 |
| snapshots | 可选重放加速点 |
| artifacts | 有界内容引用和生命周期 |

提交事件与更新投影位于同一事务。Event Log 是恢复事实，投影可重建。

### 3. 序列与并发

- 每个 Thread 在事务中分配下一个 sequence；
- `(thread_id, sequence)` 和 event_id 唯一；
- active Turn 由数据库约束/条件更新保护；
- command 使用 requestId 做幂等；
- 乐观并发冲突返回 expected/actual sequence；
- 首版本进程单实例，但存储语义不依赖内存锁保证正确性。

### 4. 持久化粒度

持久化：

- Turn/Item 生命周期；
- 完整 Assistant、Tool、Approval 和 Compaction 终值；
- Usage、Error、Cancel 和 Recovery Decision；
- Artifact 元数据。

默认不持久化每个文本 Token 或 stdout Chunk。Live Delta 可以有界合并，不作为恢复前提。

### 5. 恢复分类

启动时扫描非终态 Turn：

| 中断位置 | 恢复决策 |
|---|---|
| ACCEPTED/PREPARING_CONTEXT | 标记 INTERRUPTED，可显式继续 |
| CALLING_MODEL | 标记 INTERRUPTED，不自动重放网络请求 |
| WAITING_APPROVAL | 保持等待并恢复通知 |
| READ_ONLY Tool | 只有 Definition 明确允许时创建恢复尝试 |
| LOCAL_WRITE Tool | 检查 pre/post hash、Diff 和 Artifact |
| EXTERNAL_WRITE Tool | 查询 Action Plane；UNKNOWN 进入 Reconcile |
| CANCELLING | 完成资源扫描后写 CANCELLED 或 INTERRUPTED |

恢复永不删除原事件；Recovery Attempt 使用新 ID 并引用被中断 Attempt。

### 6. Snapshot 与重建

- Snapshot 记录所覆盖的最后 sequence 和 Schema version；
- 从最近兼容 Snapshot 重放其后 Event；
- Snapshot 校验失败时从 Event Log 全量重建；
- Compaction 只改变 Model View，不删除事件；
- 投影重建结果通过哈希与现有 Snapshot 比较。

### 7. 数据生命周期

- Thread Archive 不删除事件；
- 大 Tool 输出放 Artifact，数据库保存摘要和引用；
- Artifact 有保留期、大小限制和引用计数；
- Secret 在写入 Store 前脱敏；
- 删除功能必须区分用户数据清理和审计保留策略，1.0 前另行 ADR。

## 测试门禁

1. Event + Projection 事务原子性；
2. sequence 并发、重复和缺口；
3. 从空库执行全部 Migration；
4. 从每个已发布 Schema 升级；
5. 任意事件边界 Crash Injection；
6. 投影删除后重建一致；
7. WAITING_APPROVAL 重启恢复；
8. Tool Call 已提交/Effect 已发生/Result 未提交三阶段恢复；
9. 数据库 busy、磁盘满、损坏和只读错误；
10. Transcript Replay 产生相同终态。

## 结果

### 正向结果

- 本地安装简单；
- 恢复语义由事实驱动；
- 查询不必每次全量重放；
- 与现有 Effect Journal 职责清晰；
- 将来可按相同端口增加 PostgreSQL。

### 成本

- Event 与投影双写需要严格事务；
- SQLite 单 Writer 限制并发上限；
- Artifact 生命周期成为独立运维问题；
- 保守恢复可能要求用户显式继续。

## 被否决方案

### 只保存 JSON Transcript

查询、事务、幂等和审批恢复能力不足。

### 直接复用 Effect Journal 表

Agent 会话与外部副作用具有不同事件粒度和保留策略，混合会放大耦合。

### 第一版使用 PostgreSQL

本地优先单实例暂无收益；现有 Action Plane 已验证后续增加 PostgreSQL 的路径。

### 自动恢复所有 running Tool

无法区分效果未发生和结果丢失，会重复副作用。
