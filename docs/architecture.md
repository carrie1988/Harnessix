# Harnessix 总体架构

## 1. 目标

Harnessix 是 Framework-agnostic Agent Action Plane。它从结构化 Action 边界开始，不参与模型推理和 Agent 编排，负责把外部副作用变成可治理、可恢复、可审计的生产执行单元。

## 2. 分层

```text
┌───────────────────────────────────────────────┐
│ Agent 应用层                                  │
│ LangGraph / OpenAI Agents / 自研 Agent        │
└──────────────────────┬────────────────────────┘
                       │ ActionRequest
┌──────────────────────▼────────────────────────┐
│ 适配与 API 层                                 │
│ FastAPI / Python SDK / LangGraph Adapter       │
└──────────────────────┬────────────────────────┘
                       │
┌──────────────────────▼────────────────────────┐
│ 应用服务层                                    │
│ ActionService / Worker / 状态机 / 租约 / 对账 │
└───────────────┬──────────────────┬────────────┘
                │                  │
┌───────────────▼──────────┐ ┌─────▼─────────────┐
│ 领域层                   │ │ 基础设施层        │
│ Contract / Policy Port   │ │ SQLite/PostgreSQL │
│ Tool / Executor Port     │ │ Demo External DB  │
└───────────────┬──────────┘ └─────┬─────────────┘
                │                  │
                └────────┬─────────┘
                         ▼
                MCP / HTTP / DB / Shell
```

## 3. 核心模块

### 3.1 Domain

- `ActionRequest`：框架无关输入契约；
- `ToolDefinition`：运行时拥有的工具事实；
- `ActionStatus`：持久化生命周期；
- `PolicyDecision` 和 `ApprovalRecord`：机器策略与人工决策；
- `EffectReceipt`：外部副作用证据；
- `ExecutionOutcome` 和 `ReconciliationOutcome`：执行与对账结果。

### 3.2 ActionService

`ActionService` 是唯一能够驱动业务生命周期的组件。它负责：

1. 解析 ToolDefinition；
2. 创建 Action 和初始 Journal 事件；
3. 校验参数、副作用提示、幂等键和明文凭据；
4. 调用 Policy Engine；
5. 持久化审批；
6. 在 `inline` 模式获取执行租约，或在 `queued` 模式停留于 `READY`；
7. 调用 Executor 或等待 Worker Claim；
8. 将写操作异常保守地归类为 `UNKNOWN`；
9. 调用 Executor 的对账能力。

### 3.3 EffectJournal

`EffectJournal` 是领域端口，当前有两个实现：

- `SQLiteEffectJournal`：本地开发和单节点运行；
- `PostgresEffectJournal`：生产单节点和多 Worker 并发 Claim。

两个实现共享以下存储语义：

- `actions` 保存当前物化快照；
- `action_events` 保存不可更新的追加式事件；
- 状态更新与事件写入位于同一个事务；
- `(tenant_id, idempotency_key)` 建立条件唯一索引；
- `version` 用于快照演进和后续乐观并发控制；
- `lease_owner`、`lease_expires_at` 表示执行所有权；
- `READY` Action 本身就是持久队列，不依赖额外消息中间件。

PostgreSQL 使用 `FOR UPDATE SKIP LOCKED` 让多个 Worker 原子 Claim 不同 Action；SQLite 使用 `BEGIN IMMEDIATE` 保持单 Writer Claim 语义。

### 3.4 ActionWorker

独立 Worker 负责：

1. Claim 最早的 `READY` Action；
2. 在同一事务中写入 `LEASED` 快照和事件；
3. 转换到 `RUNNING` 后调用 Executor；
4. 长任务周期性续租；
5. 丢失租约后停止提交结果；
6. 周期性恢复过期租约。

## 4. Effect Journal 与 Trace 的区别

Trace 解释模型和工具调用过程；Effect Journal 是执行事实来源。Journal 必须回答：

- 谁请求了什么 Action；
- 使用了哪个 Tool 版本；
- 实际副作用分类是什么；
- 哪条策略允许、拒绝或要求审批；
- 谁批准了哪一个请求指纹；
- 哪个 Worker 在什么租约内执行；
- 外部副作用是否确定提交；
- `UNKNOWN` 如何通过对账得到最终结果。

## 5. 运行时不变量

1. Tool 副作用类型来自注册表，不能信任 Agent 自报。
2. 要求幂等的 Tool 没有幂等键时不得执行。
3. 审批绑定不可变请求指纹，审批后不能替换参数。
4. Action 事件序号严格递增，事件不更新。
5. 状态转换和事件追加必须处于同一个事务。
6. `RUNNING` 租约过期后进入 `UNKNOWN`，不能回到 `READY`。
7. 写操作出现未分类异常时默认进入 `UNKNOWN`。
8. `UNKNOWN` 不自动重放原 Action。
9. 对账只能观察外部系统，不能重复执行原操作。
10. 明文凭据不得进入参数、元数据、Journal 或 Trace。
11. 只有当前且未过期租约的 Owner 可以开始执行和提交结果。
12. Heartbeat 更新租约但不追加事件，避免长任务产生事件风暴。

## 6. 当前权衡

- 默认 `inline` 模式仍在 API 请求内执行，`queued` 模式已拆分独立 Worker；
- SQLite 只支持单节点开发形态，生产队列使用 PostgreSQL；
- 默认 Policy 是确定性静态规则，后续接入 OPA/Cedar；
- `demo.issue.create` 用独立 SQLite 事务模拟外部系统，不代表真实 SaaS 集成；
- 沙箱、Secret 解析和身份认证暂不属于第一阶段验收。
