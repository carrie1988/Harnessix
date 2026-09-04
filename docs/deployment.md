# 部署与运行

## 1. 本地开发模式

默认使用 SQLite 和 `inline` 执行，不需要外部中间件：

```bash
make install
make check
make run
```

该模式适合开发、调试和完整的不确定副作用演示：

```bash
make demo
```

## 2. PostgreSQL 队列模式

生产形态至少运行一个 API 进程和一个 Worker 进程，并共享同一个 PostgreSQL 数据库。

```bash
export HARNESSIX_DATABASE_URL='postgresql://harnessix:***@数据库地址:5432/harnessix'
export HARNESSIX_EXECUTION_MODE=queued
export HARNESSIX_LEASE_SECONDS=30
export HARNESSIX_WORKER_HEARTBEAT_SECONDS=10

# API 进程
uv run harnessix serve --host 127.0.0.1 --port 8787

# Worker 进程
uv run harnessix worker
```

数据库密码只应通过进程环境或 Secret 管理系统注入，不写入仓库、Action 参数、日志和 Journal。

## 3. PostgreSQL 最小权限

建议为 Harnessix 创建独立数据库和非超级用户。该用户只需要拥有 Harnessix 数据库中的 Schema 与表，不需要 `SUPERUSER`、`CREATEDB` 或 `CREATEROLE`。

首次启动时 Journal 会在事务级 Advisory Lock 保护下执行幂等迁移。多个 API/Worker 同时启动不会并发修改 Schema。

## 4. 网络边界

- PostgreSQL 不监听公网地址；
- 只监听回环地址和受控私网/VPN 地址；
- `pg_hba.conf` 只允许明确的应用来源地址；
- 使用 `scram-sha-256` 保存和校验数据库密码；
- API 是否对外开放由反向代理、TLS 和身份认证层决定。

## 5. Worker 参数

| 参数 | 环境变量 | 建议 |
|---|---|---|
| 租约时长 | `HARNESSIX_LEASE_SECONDS` | 大于正常网络抖动和调度延迟 |
| 心跳间隔 | `HARNESSIX_WORKER_HEARTBEAT_SECONDS` | 小于租约时长，通常为其三分之一 |
| 空队列轮询 | `HARNESSIX_WORKER_POLL_SECONDS` | 低延迟场景减小，低负载场景增大 |
| 恢复扫描 | `HARNESSIX_RECOVERY_INTERVAL_SECONDS` | 单节点通常为 5 秒 |

`harnessix worker --once` 最多处理一个 `READY` Action 后退出，适合验收和受外部调度器驱动的执行方式。

## 6. 容器进程

同一个镜像可以分别启动 API 和 Worker：

```bash
docker run --rm harnessix:latest serve
docker run --rm harnessix:latest worker
```

两个容器必须注入相同的 PostgreSQL URL 和 `queued` 执行模式。真实部署还需要由编排平台注入 Secret、持久卷和进程重启策略。

## 7. 健康与验收

API 健康检查：

```bash
curl http://127.0.0.1:8787/healthz
curl http://127.0.0.1:8787/readyz
```

运行 PostgreSQL 集成测试：

```bash
HARNESSIX_TEST_POSTGRES_URL='postgresql://...' \
  uv run pytest tests/integration/test_postgres_journal.py
```

验收时应确认：

1. API 提交返回 `202` 与 `READY`；
2. Worker Claim 后依次出现 `execution_leased`、`execution_started`、`execution_completed`；
3. 两个 Worker 竞争一个 Action 时只有一个获得租约；
4. 长任务跨越原始租约截止时间后仍能通过心跳完成；
5. `RUNNING` 租约过期后进入 `UNKNOWN`，不会自动重放写操作。

## 8. OpenTelemetry Collector 验收

Harnessix 使用 OTLP/HTTP 导出 Trace 和 Metrics。仓库提供只输出到 Collector 自身日志的验收配置：

```bash
docker run --rm \
  -p 4317:4317 \
  -p 4318:4318 \
  -v "$PWD/deploy/otel-collector.debug.yaml:/etc/otelcol/config.yaml" \
  otel/opentelemetry-collector
```

应用进程配置：

```bash
export HARNESSIX_OTEL_ENDPOINT='http://127.0.0.1:4318'
export HARNESSIX_SERVICE_NAME='harnessix'
uv run harnessix serve
```

API 和 Worker 的 `service.name` 分别为 `harnessix.api`、`harnessix.worker`。Collector 日志应同时出现 Trace 与 Metrics 数据。

`deploy/otel-collector.debug.yaml` 只用于连通性验收，不保存历史数据。正式环境应把 `debug` exporter 替换为团队已有的 Trace/Metrics 后端，并在 Collector 前配置私网访问控制、TLS 或 mTLS；不把后端 Token 写入仓库。

## 9. 远程中间件落地顺序

远程服务器已有 PostgreSQL 时，建议按以下顺序扩展，避免一次引入过多组件：

1. 先部署 OpenTelemetry Collector，并仅绑定受控私网地址；
2. 用本仓库 debug 配置验证 API、Worker 两种服务数据都能到达；
3. 再选择现有公司的 Grafana/Tempo/Prometheus 或云观测后端；
4. 最后固化 Dashboard、SLO 和告警阈值。

当前代码不依赖特定可视化后端，因此更换后端只修改 Collector，不修改 Action 领域逻辑。

## Agent Kernel Session 升级（0.3.3）

以上 API/Worker 部署属于 0.1 Action Plane，不是 Agent App Server。当前 Kernel 仍通过进程内 AgentRuntime 和离线示例运行，不需要安装远程中间件。

0.3.3 的 Session 数据库升级与 Effect Journal 分开：

1. 停止该 Session 数据库的旧 Runtime 宿主；
2. 使用 SQLite Backup API 制作一致备份，或确认所有连接已关闭后备份数据库及其相关文件，不要只复制运行中 WAL 数据库的主文件；
3. 由新 AgentRuntime 取得唯一宿主锁，然后自动应用 Migration 0002/0003；
4. 原 v1/v2 事件保持不变；新事件和更新后的投影为 v3；
5. 升级后旧程序会因 Migration 版本超前而拒绝启动；回退需恢复升级前备份，不能删除迁移记录绕过检查。

数据库只支持本地单宿主，不在 NFS、硬链接别名或不同锁路径之间共享。不要在另一个活动 Runtime 之外直接调用 SessionStore.initialize 做迁移；迁移应由受宿主锁保护的启动路径执行。

验收命令：

~~~bash
uv run pytest tests/agent/test_session_upgrade.py
uv run python examples/kernel_approval.py
~~~

Migration 0003 只推进最低读者版本，不改表形状，目的是让旧程序在扫描新 Item 前拒绝启动。

完整语义见 [持久审批 ADR](adr/0012-durable-approval-checkpoint.md) 与 [Kernel 契约/诊断 ADR](adr/0013-kernel-contracts-and-telemetry.md)。

Kernel 可通过 AgentRuntime 的 observability 参数注入现有导出器，默认 NoOp；导出器由宿主关闭。离线可观测性验收不需要 Collector：

~~~bash
uv run --extra observability python -m examples.kernel_observability
~~~

## 当前 Session v6 / migration 7 升级（0.5.3b2b）

0.3.3 的步骤是历史记录；当前启动会依次应用到 `0007_managed_patch.sql`。事件版本与迁移编号不同：Agent v6、Session migration 7、副本账本 v1。旧 v1–v5 事件不改写；只有新追加或显式 rebuild 的投影升级。最低 reader 标记使旧 wheel 明确返回 schema_too_new，不能删除迁移记录强行降级。

升级前停止旧宿主，并以 SQLite backup 或完整停机备份保存 Session；写会话还必须一起保留受管副本（包括账本、私有镜像和目标文件）。两库不是一个事务，不应只恢复其中一个并假定另一边没有效果。恢复到旧版本应使用一致的升级前备份，不将新事件交给旧 reader。

包外可复现升级探针为 `scripts/session_upgrade_probe.py`：

1. 从 `git archive 45b2b1043b1aed9dd53800c89b69252cb90e2eb8` 构建旧 wheel，并为旧/新 wheel 分别创建基础依赖环境；
2. 在仓库外以旧环境运行 `python -I session_upgrade_probe.py create <空目录>`，生成真实 WAITING_APPROVAL；
3. 同目录以新环境运行 `python -I session_upgrade_probe.py upgrade <目录>`，批准旧只读请求、继续、检查旧事件字节和 Replay；
4. 以旧环境运行 `python -I session_upgrade_probe.py old-reader <目录>`，验证明确拒绝新库且不修改历史。

探针不调用模型 API，不依赖源码测试包；旧环境生成的 v5 完整 transcript 已作为冻结夹具纳入 CI。新写接入运行 `python -I kernel_patch.py`（先复制 examples 中该文件至仓库外），同样只需基础 wheel，无供应商 SDK。
