# M1 独立 Worker 与 PostgreSQL 设计

## 1. 目标

M1 第一阶段把 API 请求生命周期与外部副作用执行解耦，并让 Effect Journal 可以运行在 PostgreSQL 上。核心验收是：API 进程退出不丢 Action，多个 Worker 不重复 Claim，同一 Worker 长任务持续续租，失联 Worker 的写操作进入 `UNKNOWN`。

## 2. 不引入独立消息中间件

Action 的权威状态已经存储在 Journal 中。如果再引入 Redis、RabbitMQ 或 Kafka，会产生“数据库状态与消息投递是否一致”的双写问题。

本阶段直接把 `actions.status = READY` 视为持久队列：

- PostgreSQL 使用行锁和 `SKIP LOCKED` 并发 Claim；
- SQLite 使用 `BEGIN IMMEDIATE` 支持单节点单 Writer；
- Claim 与 `READY → LEASED`、Journal 事件处于同一事务；
- Worker 崩溃后由租约恢复，不依赖消息重新投递。

## 3. 组件关系

```text
Agent / Adapter
      │
      ▼
FastAPI
      │ validate / policy / approval
      ▼
 READY Action ───────────────┐
      │                      │
      │ claim                │ query / approval / reconcile
      ▼                      │
Independent Worker           │
      │ LEASED → RUNNING     │
      │ heartbeat            │
      ▼                      │
Executor                     │
      │                      │
      └──── Effect Journal ◄─┘
               │
          PostgreSQL
```

## 4. Worker 循环

1. 周期性恢复过期租约；
2. 原子 Claim 最早的 `READY` Action；
3. 写入 `execution_leased` 事件；
4. 启动心跳任务；
5. 转换为 `RUNNING` 并调用 Executor；
6. 写入最终结果和事件；
7. 停止心跳并释放本地任务资源；
8. 没有可执行 Action 时按配置退避轮询。

## 5. Claim 语义

PostgreSQL 在一个事务中执行：

```sql
SELECT *
FROM actions
WHERE status = 'ready'
ORDER BY created_at, action_id
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

随后更新同一行到 `LEASED` 并追加事件。`SKIP LOCKED` 只用于队列消费者，不用于普通业务查询。

## 6. 租约所有权

- `lease_owner` 标识 Worker 实例；
- `lease_expires_at` 表示租约截止时间；
- `LEASED → RUNNING` 和 `RUNNING → 最终状态` 必须校验租约 Owner；
- Heartbeat 只更新当前 Owner 持有且仍在执行中的 Action；
- Heartbeat 不追加 Journal 事件，避免长任务产生无意义事件风暴；
- Worker 丢失租约后不得再提交执行结果。

## 7. 恢复规则

| 过期状态 | 恢复状态 | 原因 |
|---|---|---|
| `LEASED` | `READY` | 尚未进入外部调用，可重新 Claim |
| `RUNNING` | `UNKNOWN` | 外部副作用可能已经提交 |
| `RECONCILING` | `UNKNOWN` | 对账过程没有形成确定结论 |

## 8. 部署模式

开发模式保留 `inline` 执行，方便单进程调试。生产模式使用：

```text
HARNESSIX_EXECUTION_MODE=queued
HARNESSIX_DATABASE_URL=postgresql://...
```

并分别启动：

```bash
harnessix serve
harnessix worker
```

## 9. 第一阶段限制

- 对账请求仍由 API 同步驱动，后续再进入独立 Reconcile Worker；
- 当前 Worker 使用数据库轮询，后续可用 PostgreSQL `LISTEN/NOTIFY` 降低空轮询；
- M1 单节点部署不承诺跨区域容灾；
- Demo Issue 仍使用独立 SQLite 文件模拟外部系统。

## 10. 实现与验收状态

- [x] `EffectJournal` 领域端口；
- [x] SQLite 原子 Claim、续租、Owner 校验和恢复；
- [x] PostgreSQL 迁移、幂等创建、状态事务和事件事务；
- [x] PostgreSQL 多 Worker `SKIP LOCKED` Claim；
- [x] `harnessix worker` 与 `--once` 运维入口；
- [x] `inline` / `queued` 两种执行模式；
- [x] SQLite 单元/集成测试；
- [x] PostgreSQL 17 真实环境集成测试；
- [x] CI PostgreSQL 17 服务测试。

PostgreSQL 集成测试通过 `HARNESSIX_TEST_POSTGRES_URL` 显式启用；未配置时本地测试会跳过，不要求每位贡献者都安装 PostgreSQL。

## 11. 参考资料

- [PostgreSQL 17：SELECT 锁与 SKIP LOCKED](https://www.postgresql.org/docs/17/sql-select.html#SQL-FOR-UPDATE-SHARE)
- [PostgreSQL 17：Advisory Lock](https://www.postgresql.org/docs/17/explicit-locking.html#ADVISORY-LOCKS)
- [asyncpg：连接池与事务](https://magicstack.github.io/asyncpg/current/usage.html#connection-pools)
