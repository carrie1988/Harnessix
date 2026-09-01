# ADR-0004：在 Action 快照中持久化 W3C Trace Context

- 状态：已采纳
- 日期：2026-09-01

## 背景

Harnessix 的 API 与 Worker 已经通过数据库持久队列解耦。普通进程内 Context 无法穿越等待时间和进程边界；如果只给 HTTP 和 Worker 分别创建 Trace，就无法回答“这个执行对应哪一次 Agent Action 提交”。

同时，`ActionRequest` 是不可变业务契约。同一个 `action_id` 的重试必须允许网络 Trace 发生变化，不能因为观测字段改变而触发请求冲突。

## 决策

1. 使用 W3C `traceparent` 和可选 `tracestate` 作为跨边界格式；
2. 新增运行时拥有的 `TraceContext`，保存在 `ActionSnapshot.trace_context`；
3. Trace Context 由首次创建 Action 的运行时写入，不属于调用方业务载荷；
4. Trace Context 不参与请求指纹和幂等冲突比较；
5. 重复 Action 返回原 Trace Context，不覆盖首次创建时的上下文；
6. Worker 创建 CONSUMER Span 时从 Journal 快照提取父上下文；
7. 领域代码只依赖内部 Observability 端口，不依赖 OpenTelemetry SDK 类型。

## 备选方案

### 仅保存 `trace_id`

无法保留采样标志、父 Span 和厂商状态，不能按标准传播，放弃。

### 把 Header 写进 `ActionRequest.metadata`

会污染业务契约和指纹语义，也可能把不受控 Header 带入 Journal，放弃。

### 使用内存消息上下文

无法覆盖数据库排队、进程重启和延迟执行，放弃。

### 单独建立 Trace 关联表

当前每个 Action 只需要一个首次提交上下文，单独建表增加事务和查询复杂度，收益不足，暂不采用。

## 结果

### 正向结果

- API、Journal、Worker、Executor 可形成一条连续 Trace；
- Action 业务幂等语义不受 Trace 变化影响；
- 历史 Action 没有上下文时仍可由 Worker 创建新 Trace；
- 未来可在不同 Trace 之间增加 Span Link，而不修改 Action Contract。

### 代价

- `actions` 增加一个可空 JSON 字段；
- 每次新建 Action 多一次小型序列化；
- Trace Context 属于诊断数据，需要按日志和 Trace 的留存策略治理。
