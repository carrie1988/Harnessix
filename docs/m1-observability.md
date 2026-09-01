# M1.2 可观测性与运行保障设计

## 1. 阶段目标

M1.2 解决 API、持久化队列和独立 Worker 分离之后的生产诊断问题：一个 Action 从 HTTP 接收、策略判断、进入数据库队列，到被另一个进程执行时，仍能通过同一条 Trace 关联；运行人员也能从指标和结构化日志判断队列是否积压、租约是否异常、数据库是否可用。

本阶段不引入 Agent Loop、模型调用或新的业务中间件，也不使用任何大模型 API Key。

## 2. 范围与边界

### 2.1 已实现

- 领域无关的 `Observability` 端口；
- 默认零开销的 `NoOpObservability`；
- 可选 OpenTelemetry Trace 与 Metrics 适配器；
- W3C `traceparent`、`tracestate` 的接收、持久化和 Worker 恢复；
- HTTP、Action、Policy、Worker Consumer、Executor、Reconcile Span；
- JSON 结构化日志及 Action 安全上下文；
- Action、HTTP、Worker、租约、队列、审批、对账指标；
- `/healthz` 存活检查和 `/readyz` Journal 就绪检查；
- SQLite/PostgreSQL 版本化迁移；
- API 到 Worker 的跨进程 Trace 测试、迁移测试和 PostgreSQL 测试。

### 2.2 有意延后

- OpenTelemetry Logs Signal：当前使用 Python 标准日志输出 JSON；
- Grafana Dashboard 和告警规则的仓库内固化；
- Worker 实例注册表与单实例心跳；
- 日志采集 Agent；
- OIDC、Secret Provider、MCP Executor。

## 3. 架构

```text
上游 Agent / HTTP Client
        │ traceparent / tracestate
        ▼
FastAPI SERVER Span
        │
        ▼
ActionService.submit Span
        │ 生成并读取当前 W3C Trace Context
        ▼
Effect Journal
actions.trace_context_json
        │ 持久队列边界
        ▼
Worker Claim
        │ 从快照提取父上下文
        ▼
Worker CONSUMER Span
        ├── Action execute Span
        └── Executor / Reconcile Span

Trace / Metrics ──OTLP/HTTP──► OpenTelemetry Collector
JSON Logs ────────────────────► stdout / 日志采集系统
```

领域层不引用 OpenTelemetry SDK 类型。`ActionService`、`ActionWorker` 和 API 只依赖内部 `Observability` 协议，因此测试、未启用观测的本地开发、后续替换采集后端都不受 SDK 绑定。

## 4. Durable Trace Context

### 4.1 为什么不放进 `ActionRequest`

`ActionRequest` 是调用方提交的不可变业务契约，也是 `action_id` 冲突校验的一部分。如果运行时自动把每次请求产生的新 Trace Context 写进请求，同一个 `action_id` 的合法重试会因为 Trace 不同而被误判为载荷冲突。

因此 Trace Context 是运行时拥有的快照字段：

```json
{
  "trace_context": {
    "traceparent": "00-...-...-01",
    "tracestate": "vendor=value"
  }
}
```

它不参与业务幂等指纹。首次创建 Action 时固定保存；重复提交返回原快照，不覆盖原始 Trace。

### 4.2 跨进程传播

1. API 从 HTTP Header 提取 W3C 上下文并创建 SERVER Span；
2. `ActionService.submit` 获取当前上下文；
3. Journal 将上下文与 Action 快照原子持久化；
4. Worker Claim Action 后，用快照上下文创建 CONSUMER Span；
5. Executor Span 成为 Consumer Span 的子节点。

非法或无法解析的远端上下文不会成为父节点，OpenTelemetry 会创建本地有效 Trace。

## 5. 结构化日志

默认日志格式为单行 JSON，固定基础字段：

- `timestamp`；
- `level`；
- `logger`；
- `message`。

在 Action 上下文中按需增加：

- `action_id`；
- `tenant_id`；
- `tool`；
- `worker_id`；
- `trace_id`；
- `span_id`。

日志禁止写入 Action 参数、HTTP Header、数据库 URL、Secret 值和外部响应正文。上下文绑定器只接受白名单字段，减少误把凭据打入日志的风险。

## 6. Metrics

### 6.1 Counter

| 指标 | 主要标签 | 含义 |
|---|---|---|
| `harnessix.actions.submitted` | `tool`、`effect_class` | 收到的 Action 提交 |
| `harnessix.actions.completed` | `tool`、`status` | 到达确定终态的 Action |
| `harnessix.actions.submit_errors` | `tool`、`effect_class` | 提交管线异常 |
| `harnessix.executions.completed` | `tool`、`status` | Executor 执行结果 |
| `harnessix.worker.claims` | `tool` | Worker Claim 数 |
| `harnessix.worker.lease_renewal_failures` | `tool` | 续租失败数 |
| `harnessix.lease.recoveries` | 无 | 恢复的过期租约数 |
| `harnessix.approvals.decisions` | `tool`、`outcome` | 审批决策数 |
| `harnessix.reconciliation` | `tool`、`outcome` | 对账结果数 |
| `harnessix.http.requests` | `method`、`route`、`status` | HTTP 请求数 |

### 6.2 Histogram

- `harnessix.action.duration`：提交、校验和策略管线耗时；
- `harnessix.executor.duration`：外部执行耗时；
- `harnessix.reconciliation.duration`：对账耗时；
- `harnessix.http.duration`：HTTP 处理耗时。

### 6.3 Gauge

- `harnessix.queue.ready`；
- `harnessix.queue.oldest_ready_age`；
- `harnessix.actions.pending_approval`；
- `harnessix.actions.unknown`。

Metric 标签只使用 Tool、状态、结果和路由模板等低基数维度，明确禁止 `action_id`、`tenant_id`、`worker_id` 等高基数字段。

## 7. 健康检查

| 接口 | 语义 | 失败行为 |
|---|---|---|
| `GET /healthz` | 进程能够响应 | 进程故障时无法返回 |
| `GET /readyz` | Journal 已初始化且可执行 `SELECT 1` | 返回 `503` 和固定原因码 |

`/readyz` 不返回数据库地址、驱动错误或凭据，避免健康接口泄漏基础设施细节。

## 8. 配置

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `HARNESSIX_SERVICE_NAME` | `harnessix` | OTel 服务名前缀，进程自动附加 `.api` 或 `.worker` |
| `HARNESSIX_LOG_LEVEL` | `INFO` | Python 日志级别 |
| `HARNESSIX_LOG_FORMAT` | `json` | `json` 或 `console` |
| `HARNESSIX_OTEL_ENDPOINT` | 空 | OTLP/HTTP Collector 基础地址，如 `http://collector:4318` |
| `HARNESSIX_OTEL_EXPORT_INTERVAL_MILLIS` | `10000` | Metrics 导出周期 |

未配置 OTel Endpoint 时使用 NoOp 适配器，不创建后台导出线程，也不要求安装可选依赖。启用时安装：

```bash
pip install 'harnessix[observability]'
```

## 9. 数据迁移

数据库新增 `actions.trace_context_json`：

- SQLite：`0002_observability.sql`；
- PostgreSQL：`0002_observability.sql`。

Journal 启动时按文件版本顺序执行尚未应用的迁移。PostgreSQL 继续使用事务级 Advisory Lock，保证 API 与多个 Worker 同时启动时只有一个迁移执行者。迁移只新增可空列，已有 Action 兼容为 `trace_context = null`。

## 10. 验收标准

1. 未配置 OTel 时现有行为和性能路径保持兼容；
2. 配置 OTel 时能产生合法 W3C Trace Context；
3. API 创建的 Trace Context 经数据库重载后保持一致；
4. Worker Consumer Span 使用持久化上下文作为父上下文；
5. 指标标签不包含 Action 或租户 ID；
6. SQLite 既有 v1 数据库可幂等升级到 v2；
7. `/readyz` 能区分可用和不可用 Journal；
8. 全量格式、静态类型、单元、SQLite 集成和 PostgreSQL 集成检查通过。

## 11. 官方依据

- [OpenTelemetry Python 手工插桩](https://opentelemetry.io/docs/languages/python/instrumentation/)；
- [OpenTelemetry Python 上下文传播](https://opentelemetry.io/docs/languages/python/propagation/)；
- [OpenTelemetry Python 导出器与 Collector 示例](https://opentelemetry.io/docs/languages/python/exporters/)；
- [OTLP Exporter Endpoint 规范](https://opentelemetry.io/docs/specs/otel/protocol/exporter/)；
- [OpenTelemetry Python 信号状态](https://opentelemetry.io/docs/languages/python/)。
