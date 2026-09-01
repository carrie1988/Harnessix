# ADR 0003：使用 Journal 状态实现持久化 Worker Queue

- 状态：已接受
- 日期：2026-09-01

## 背景

M0 在 API 请求中直接执行 Executor，长任务会占用请求连接，API 退出也会中断执行。引入独立消息中间件会增加 Action 状态与消息投递之间的双写一致性问题。

## 决策

将 Journal 中的 `READY` Action 作为持久队列。SQLite 使用单 Writer 事务 Claim，PostgreSQL 使用 `FOR UPDATE SKIP LOCKED`。Claim、租约写入和事件追加必须在同一个事务中完成。

## 结果

- API 与 Worker 可以独立扩缩；
- 不需要额外消息中间件；
- PostgreSQL 是队列和 Action 状态的唯一事实来源；
- 数据库轮询会产生固定开销，后续可使用 `LISTEN/NOTIFY` 优化唤醒；
- Worker 必须实现心跳、Owner 校验和过期恢复。
