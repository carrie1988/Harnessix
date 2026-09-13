---
doc_type: deployment-design
status: current
version: 4
code_revision: 71a479439edcdd29b863ec3a9bad7a52586dd1bf
owners:
  - core
modules:
  - deployment
  - observability
  - api
  - product_config
  - product_ui
related_adrs:
  - docs/adr/0004-durable-trace-context.md
  - docs/adr/0013-kernel-contracts-and-telemetry.md
  - docs/adr/0079-preflight-and-native-read-port.md
related_tests:
  - tests/integration/test_api.py
  - tests/integration/test_observability_flow.py
  - tests/agent/test_telemetry.py
  - tests/product_config/test_server_and_cli.py
  - tests/product_config/test_preflight.py
  - tests/product_ui/test_cli.py
  - tests/product_ui/test_interaction_screens.py
  - tests/product_ui/test_app_interactions.py
supersedes: []
---

# Harnessix Code诊断与可观测性

## 1. 诊断目标

诊断应回答“哪个部署面、哪个持久身份、哪个状态边界、哪类失败、是否可能发生外部效果”，而不是只收集进程日志。
Journal、Session Event和专用账本是业务事实；Log、Trace和Metric用于关联与告警，丢失时不得改变领域结果。

当前Action Plane具备结构化日志、OTLP/HTTP Trace/Metric、Health/Readiness和Action查询；Product Config具备离线诊断；
Product UI状态行显示连接代际、Turn、Token、费用未知原因、待决交互和稳定Notice，并通过`F1`提供静态脱敏错误自助。
Coding Agent产品入口已有共享的离线`harnessix code doctor`和Startup Preflight；自动支持包与完整观测装配尚未实现。

## 2. 诊断顺序

```mermaid
flowchart TD
    Symptom[故障现象] --> Surface{部署面}
    Surface -- Action HTTP --> Live[healthz与readyz]
    Surface -- Agent stdio --> Process[进程退出码与协议握手]
    Surface -- 产品启动 --> Preflight[code doctor]
    Surface -- 配置内部 --> Config[config diagnose]
    Live --> Identity[固定Action/Trace/Worker身份]
    Process --> Identity
    Preflight --> Identity
    Config --> Identity
    Identity --> Durable[读取Journal/Event/Snapshot/审计]
    Durable --> Effect{副作用可能发生?}
    Effect -- 否 --> Root[定位代码/依赖/配置]
    Effect -- 是或未知 --> Reconcile[停止重试并对账]
    Root --> Evidence[生成脱敏证据]
    Reconcile --> Evidence
```

先固定身份和状态，再扩大日志范围。不要通过提高日志级别把Prompt、Tool参数、Workspace内容或Secret写入公共日志。

## 3. Health与Readiness

Action Plane HTTP端点：

```bash
curl --fail http://127.0.0.1:8787/healthz
curl --fail http://127.0.0.1:8787/readyz
```

| 端点 | 成功 | 失败 | 能证明 | 不能证明 |
|---|---|---|---|---|
| `/healthz` | `200 {"status":"ok"}` | 进程/网络失败 | ASGI进程可响应 | Journal、Worker、Provider或外部效果可用 |
| `/readyz` | `200 {"status":"ready"}` | `503`且`reason=journal_unavailable` | Journal当前`ping`成功 | Schema完整、队列被消费、外部服务可用 |

`/readyz`失败时应摘除流量；`/healthz`仍成功不能覆盖Readiness失败。当前没有Agent stdio专用Health端点，
Agent Protocol握手和初始化失败退出码承担启动可用性信号。

## 4. Product Config离线诊断

```bash
uv run harnessix config diagnose \
  --config ./private/product-config.json \
  --profile primary \
  --state-database ./private/state/product-config.db
```

输出是稳定JSON `ConfigurationDiagnosticReport`，包含配置/选择摘要、选中Profile、`ready`、排序后的检查和报告摘要。
检查范围：

| Scope | 代码 | 解释 |
|---|---|---|
| `config` | `config_contract_valid` | 严格v2合同已解析 |
| `profile` | `config_capabilities_satisfied` | 候选满足首选所需能力 |
| `dependency` | `config_dependency_available` | 对应Python SDK可导入 |
| `secret` | `config_secret_version_available` | Secret名称、版本和值存在 |
| `provider` | `config_provider_auth_usable` | Key可按ASCII及Header注入规则使用 |

诊断不联网、不校验Provider账户、模型存在性、地域、额度、价格或Egress。`ready=false`退出2；内部或合同错误也退出2，
错误JSON写stderr且不回显原配置和Secret。

### 4.1 产品级Doctor

```bash
uv run harnessix code doctor /absolute/workspace --config /absolute/config.json --json
```

`ProductPreflightReport`在上述配置诊断之外检查配置文件/v2合同、Profile、平台读取端口、Workspace、State、TUI和可选Git。
Required全部通过退出0，否则退出2并保留独立检查结果；前置失败只跳过其依赖项。报告只有稳定代码、修复动作ID、摘要、
Profile和脱敏Workspace指纹，不含绝对路径、环境值或原始异常。Doctor离线只读，不创建状态目录、数据库、Session或网络请求。
它只代表一次瞬时观察，不能替代`agent-server`启动时的重新校验。

## 5. 结构化日志

Action Plane由`HARNESSIX_LOG_LEVEL`和`HARNESSIX_LOG_FORMAT`控制，默认单行JSON。允许绑定的上下文字段仅为：

```text
action_id, tenant_id, tool, worker_id, trace_id, span_id
```

JSON基础字段为`timestamp`、`level`、`logger`、`message`，异常时可包含`exception`。当前异常正文可能来自第三方库，
进入集中日志前仍需外层Secret Canary和访问控制；白名单上下文字段不等于异常正文已完整脱敏。

`agent-server`在顶层CLI分派时不会执行Action Plane的`configure_logging`，stdout被stdio协议独占，启动错误以脱敏JSON写
stderr。当前Agent Runtime Trace/Metric未通过产品启动统一接到OTel，这属于明确缺口。

## 6. OpenTelemetry

当`HARNESSIX_OTEL_ENDPOINT`或`OTEL_EXPORTER_OTLP_ENDPOINT`非空时，Action API/Worker构造OTLP/HTTP实现；为空时
使用No-op。安装环境必须包含`observability` Extra。

```bash
export HARNESSIX_OTEL_ENDPOINT='http://127.0.0.1:4318'
export HARNESSIX_OTEL_EXPORT_INTERVAL_MILLIS=10000
export HARNESSIX_SERVICE_NAME='harnessix'
uv run harnessix serve
```

Service Name分别为`harnessix.api`和`harnessix.worker`。仓库的
[`deploy/otel-collector.debug.yaml`](../../deploy/otel-collector.debug.yaml)只用于连通性验证，不是生产存储方案。

### 6.1 主要Trace

| Span | 位置 | 说明 |
|---|---|---|
| `harnessix.http.request` | API Middleware | HTTP Server请求 |
| `harnessix.action.submit` | Action Runtime | 准入与提交 |
| `harnessix.worker.consume` | Worker | 持久Trace跨队列消费 |
| `harnessix.action.execute` | Executor | Action效果执行 |
| `harnessix.action.reconcile` | Reconcile | 未知效果对账 |

### 6.2 主要Metric

| Metric | 类型语义 | 主要用途 |
|---|---|---|
| `harnessix.http.requests` | Counter | Route/Method/Status请求数 |
| `harnessix.http.duration` | Histogram | HTTP时延 |
| `harnessix.actions.submitted` | Counter | 工具和状态提交数 |
| `harnessix.actions.completed` | Counter | 工具和终态完成数 |
| `harnessix.executions.completed` | Counter | Executor终态数 |
| `harnessix.executor.duration` | Histogram | 执行时延 |
| `harnessix.worker.claims` | Counter | Worker Claim数 |
| `harnessix.worker.lease_renewal_failures` | Counter | 租约续租失败 |
| `harnessix.lease.recoveries` | Counter | 过期租约恢复数 |
| `harnessix.queue.ready` | Gauge | READY数量 |
| `harnessix.queue.oldest_ready_age` | Gauge | 最老READY年龄 |
| `harnessix.actions.pending_approval` | Gauge | 等待审批数 |
| `harnessix.actions.unknown` | Gauge | UNKNOWN数量 |
| `harnessix.reconciliation` | Counter | 对账结果数 |

当前OpenTelemetry实现把全部Histogram单位固定为`s`，虽然Action时延符合秒语义，但该通用实现对未来非时长Histogram
不安全；新增Metric前必须修复单位合同。Metric标签不得使用Action ID、Tenant ID、路径、Prompt或任意高基数字段。

## 7. Action状态诊断

```bash
curl --fail http://127.0.0.1:8787/v1/actions/<action-id>
curl --fail http://127.0.0.1:8787/v1/actions/<action-id>/events
```

按以下顺序核对：

1. `action_id`、`tenant_id`、Tool名称/版本和Request Fingerprint；
2. Snapshot状态与Event最后序号；
3. Policy、Approval Fingerprint、Lease Owner/Expiry；
4. Result/Receipt/Error和`UNKNOWN`原因；
5. 外部系统中的稳定Idempotency或Receipt身份；
6. Trace ID关联的API、Worker和Executor Span。

HTTP API当前未实现身份认证，诊断接口本身会暴露Action投影；只允许在回环或受保护网络使用。

## 8. 告警建议与当前边界

以下是待0.9容量/Soak校准的信号，不是已承诺SLO：

| 信号 | 应关注的变化 | 响应 |
|---|---|---|
| Readiness | 连续503 | 摘除API，检查Journal和Migration |
| `queue.ready`/oldest age | 持续增长 | 检查Worker数量、错误和Lease |
| `actions.unknown` | 非零持续或突增 | 停止自动重试，启动对账 |
| Lease renewal failures | 突增 | 检查数据库时延、事件循环阻塞和重复Owner |
| Reconcile error/unknown | 持续增长 | 检查外部查询契约和凭据，不重发效果 |
| Provider Attempt/Usage异常 | Usage缺失或尝试激增 | 停止费用扩大，检查流式协议与Fallback |
| Agent interrupted | 重启后集中出现 | 检查宿主稳定性与显式Resume策略 |

没有经过负载基线，本文不提供固定数值阈值。正式阈值必须绑定版本、硬件、后端、并发和任务集。

## 9. 常见错误分诊

| 错误/现象 | 直接含义 | 下一步 |
|---|---|---|
| `journal_unavailable` | Journal Ping失败 | 网络/文件权限/数据库进程/Migration |
| `runtime_busy` | Session已有Runtime Owner | 查找原进程，不删除Lock绕过 |
| `schema_too_new` | 数据由更高版本创建 | 启动正确版本或恢复备份 |
| `migration_changed` | 已应用Session Migration字节变化 | 隔离制品并审计供应链 |
| `database_corrupt` | SQLite quick check失败 | 停写并恢复已验证备份 |
| `product_config_permissions` | POSIX配置文件身份/Mode/硬链接不安全 | 恢复Owner、`0600`和单链接普通文件 |
| `product_config_diagnostic_failed` | 至少一个离线检查失败 | 读取脱敏诊断报告 |
| `product_state_overlap` | 状态目录与Workspace重叠 | 移动状态目录，不用Symlink绕过 |
| `product_tools_platform_unsupported` | 未知平台或原生读取端口不可用 | 使用已验证的POSIX/Windows宿主并检查系统能力 |
| `UNKNOWN` | 外部效果无法证明 | 调用专用Reconcile或人工处理 |
| stdio无输出 | 可能未完成握手、进程退出或stdout污染 | 检查stderr JSON、argv和协议帧，不发送Shell文本 |

### 9.1 Product UI错误自助

`F1`打开的帮助只来自[`error_help.py`](../../src/harnessix/product_ui/error_help.py)静态目录，包含标题、原因、影响、
恢复动作和本文锚点。未知错误码、路径和异常正文不会回显，而是统一映射为`product_internal_failure`。

| 稳定码或分组 | 直接含义 | 命令与恢复结论 |
|---|---|---|
| `approval_stale` | Turn、Call、Approval或Fingerprint已变化 | 决定未发送且不分配Command ID；刷新后重开 |
| `approval_evidence_required` | 完整Diff尚未通过 | Approve禁用，Reject可用；重读证据或拒绝 |
| `question_stale`/`question_answer_invalid` | 问题已变化，或回答为空/超限 | 回答未发送；按当前问题修正 |
| `turn_control_stale`/`steering_invalid` | Turn状态或Steer正文不再有效 | Cancel/Steer未发送；刷新状态 |
| `diff_unavailable` | Artifact能力缺失或批量Diff引用缺失 | 不允许盲批；升级服务、重试或Reject |
| `artifact_reference_changed` | 分页引用与审批绑定不同 | 证据作废；重新读取并检查服务端 |
| `artifact_pagination_stalled` | offset重复、不连续或超过50页 | 读取停止；禁止Approve |
| `artifact_integrity_failed` | 记录、UTF-8字节或SHA-256不符 | Diff不可信；拒绝并检查Artifact链路 |
| `artifact_read_timeout` | 5秒绝对时限内未完整读取 | 未产生领域命令；重试或Reject |
| 连接错误分组 | 当前连接代际不可继续 | 不盲重放；`Ctrl+R`后从Replay确认 |
| `product_internal_failure` | 未命中可公开稳定分类 | 不推断业务结果；重启并采集白名单诊断 |

## 9.1 Workspace Patch错误分诊

| 稳定错误/状态 | 含义 | 处置 |
|---|---|---|
| `platform_not_supported` | 平台缺少受支持的POSIX no-follow写语义 | 保持只读；不得用Shell或普通Path API绕过 |
| `delivery_action_mismatch` / `delivery_request_conflict` | Route、规范资源、Snapshot或既有事务身份不一致 | 不覆盖旧事务；重新读取Workspace并创建新调用 |
| `execution_plan_stale` | 审批前后Workspace来源发生变化 | 无文件效果；废弃原批准并生成新提案 |
| `artifact_conflict` / `artifact_not_found` | Review正文/身份冲突，或当前Call尚未授权引用 | 禁止批准；重新Replay并核对Session事实 |
| `delivery_source_changed` | 成员写入前观察不再等于before | 停止写入并人工审查外部修改 |
| `delivery_partial_effect` | 已形成严格after前缀和before后缀 | 人工审查并决定补齐或回退；系统不会自动续写 |
| Action `unknown` | 取消、失租或崩溃后无法证明效果 | 只允许Reconcile观察，不再次执行 |

诊断记录只能保存Plan/Transaction/Artifact摘要、成员数量、游标、状态和稳定错误码；不得保存Patch正文、完整Diff、Workspace绝对路径或原始异常。e5之前没有启动全局恢复报告，发现旧`running/reconciling` Route时必须保留全部State文件用于对账。

## 10. 证据采集与脱敏

允许保存：代码Revision、依赖版本、平台、时间、稳定错误码、状态枚举、事件类型、摘要、请求次数、Token/费用聚合、
Trace/Span ID和白名单Metric。默认禁止保存：API Key、Authorization Header、数据库URL、用户Prompt、模型正文、
Workspace绝对路径、文件内容、Tool参数/输出、私有Session和供应商原始响应。

诊断包尚未实现。人工采集必须先形成字段白名单，再读取源数据；不能先打包整个状态目录后再尝试删除敏感内容。

## 11. 源码与测试映射

| 诊断能力 | 源码 | 测试 |
|---|---|---|
| Health/Readiness | [`api/app.py`](../../src/harnessix/api/app.py) | [`test_api.py`](../../tests/integration/test_api.py) |
| 日志 | [`observability/logging.py`](../../src/harnessix/observability/logging.py) | [`test_observability_core.py`](../../tests/unit/test_observability_core.py) |
| OTel | [`observability/opentelemetry.py`](../../src/harnessix/observability/opentelemetry.py) | [`test_observability_flow.py`](../../tests/integration/test_observability_flow.py) |
| Worker运行指标 | [`worker.py`](../../src/harnessix/worker.py)的`record_operational_metrics` | [`test_worker.py`](../../tests/integration/test_worker.py) |
| 配置诊断 | [`product_config/runtime.py`](../../src/harnessix/product_config/runtime.py)的`diagnose_configuration` | [`test_server_and_cli.py`](../../tests/product_config/test_server_and_cli.py) |
| 产品Doctor/Preflight | [`product_config/preflight.py`](../../src/harnessix/product_config/preflight.py)、[`product_ui/cli.py`](../../src/harnessix/product_ui/cli.py) | [`test_preflight.py`](../../tests/product_config/test_preflight.py)、[`tests/product_ui/test_cli.py`](../../tests/product_ui/test_cli.py) |
| Agent Telemetry | [`agent/telemetry.py`](../../src/harnessix/agent/telemetry.py) | [`test_telemetry.py`](../../tests/agent/test_telemetry.py) |
| Product UI错误自助 | [`product_ui/error_help.py`](../../src/harnessix/product_ui/error_help.py)、[`product_ui/main_view.py`](../../src/harnessix/product_ui/main_view.py) | [`test_interaction_screens.py`](../../tests/product_ui/test_interaction_screens.py)、[`test_app_interactions.py`](../../tests/product_ui/test_app_interactions.py) |

## 12. 已知限制

统一Agent产品观测装配、支持包Schema、数据保留策略、Dashboard、告警规则、SLO、Metric单位修复、高基数硬限制、
异常正文统一脱敏和Observability失败完全隔离仍未完成。Doctor是离线启动诊断，不等价于运行期Telemetry或支持包。部署时应把这些缺口作为发布阻断项，而不是
通过外部Collector存在来宣称可观测性已经生产完备。
