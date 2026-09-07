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

## 历史 Session v6 / migration 7 升级（0.5.3b2b）

本节记录 b2b 的升级验收；当时启动应用到 `0007_managed_patch.sql`，当前 migration9 的步骤见文末。事件版本与迁移编号不同：Agent v6、Session migration 7；副本账本现为 v3，v1→v2 与 v2→v3 的独立升级步骤见下文。旧 v1–v5 事件不改写；只有新追加或显式 rebuild 的投影升级。最低 reader 标记使旧 wheel 明确返回 schema_too_new，不能删除迁移记录强行降级。

升级前停止旧宿主，并以 SQLite backup 或完整停机备份保存 Session；写会话还必须一起保留受管副本（包括账本、私有镜像和目标文件）。两库不是一个事务，不应只恢复其中一个并假定另一边没有效果。恢复到旧版本应使用一致的升级前备份，不将新事件交给旧 reader。

包外可复现升级探针为 `scripts/session_upgrade_probe.py`：

1. 从 `git archive 45b2b1043b1aed9dd53800c89b69252cb90e2eb8` 构建旧 wheel，并为旧/新 wheel 分别创建基础依赖环境；
2. 在仓库外以旧环境运行 `python -I session_upgrade_probe.py create <空目录>`，生成真实 WAITING_APPROVAL；
3. 同目录以新环境运行 `python -I session_upgrade_probe.py upgrade <目录>`，批准旧只读请求、继续、检查旧事件字节和 Replay；
4. 以旧环境运行 `python -I session_upgrade_probe.py old-reader <目录>`，验证明确拒绝新库且不修改历史。

探针不调用模型 API，不依赖源码测试包；旧环境生成的 v5 完整 transcript 已作为冻结夹具纳入 CI。新写接入运行 `python -I kernel_patch.py`（先复制 examples 中该文件至仓库外），同样只需基础 wheel，无供应商 SDK。

## 副本账本 v2 升级（0.5.3c2a）

本节记录 c2a 的历史升级，当前 v3 见下一节。这是每个私有受管副本的 `ledger.sqlite` 升级，不是 Session migration 8，也不改变 Action Plane 数据库。关闭旧宿主后整体备份副本目录及相应 Session；不要仅复制账本而遗漏 workspace、owner.lock 或仍可能存在的临时文件。

新宿主取得独占 owner.lock 后，先验证副本身份、metadata、baseline 和全部旧单文件计划，再在单个 SQLite 事务中新增组计划/审批表和成员归属外键并推进 user_version。错误数据库、未来版本或校验失败不升级；DDL 中断保持 v1，提交后为完整 v2。升级保留旧事件/镜像原字节和数据库 inode，不应用或重放补丁。

真实旧 wheel 探针为 `scripts/patch_ledger_upgrade_probe.py`，复制至仓库外，分别用两个基础安装环境运行：

1. `git archive 09cb6d665933076e29699974dfeb0d31fd6e6b4e` 导出旧源码并构建 wheel，另构建本片新 wheel，分别安装到独立环境；
2. 旧环境：`python -I patch_ledger_upgrade_probe.py create <新目录>`，生成真正 v1 的 pending/approved/applied 三类计划；
3. 新环境：`python -I patch_ledger_upgrade_probe.py upgrade <同目录>`，验证全部旧状态、事件/镜像字节、文件 inode/mtime/ctime 和源目录不变；
4. 旧环境：`python -I patch_ledger_upgrade_probe.py reject <同目录>`，应明确返回 patch_wrong_database；
5. 再运行新环境 upgrade，确认拒绝旧 reader 后仍可重开且没有重复写。

旧 v1 reader 拒绝 v2 是预期行为。不要手动降低 user_version 或删表降级；回退只能恢复一致的升级前完整备份，并接受该备份之后状态不可用。基础发行版本仍为0.1.0，能力切片编号与包版本/数据库格式分别管理。c2a 当时只提供宿主组审批；当前 c2b 的多文件执行见下一节。不需要真实模型、远程服务器或新中间件。

## 副本账本 v3 升级（0.5.3c2b）

当前完整目标为 v3。上一节保留 c2a 的 v1→v2 历史步骤；当前源码的 `patch_ledger_upgrade_probe.py` 会校验最新账本版本，v1 会先完整升到 v2，再完整升到 v3。原 `ledger_migrations.py` 的 v1→v2 实现未修改，新步骤在 `batch_run_migrations.py`。升级不消费已批准组，不修改任何目标文件或旧事件。

v2→v3 在副本独占锁、metadata/baseline、旧单文件记录、外键和完整组记录校验后，于同一事务创建 batch_run_events 并推进 user_version。中断只保留完整 v2 或完整 v3。旧 v2 wheel 会拒绝 v3，不能改 user_version 强行降级。升级前仍须一致地备份整个副本与关联 Session，不仅备份 SQLite 文件。

`batch_run_upgrade_probe.py` 可在仓库外用实际旧/新基础 wheel 复现：

1. 从 `git archive f0adddcead492e7114ead38e91a4adf00d0142c0` 构建并独立安装旧 v2 wheel，另安装当前新 wheel；
2. 旧环境：`python -I batch_run_upgrade_probe.py create <新目录>`，真实保存 pending/approved/rejected 三类组；
3. 新环境：`python -I batch_run_upgrade_probe.py upgrade <同目录>`，校验旧表原字节、原决定、目标时间/inode 与源目录，确认所有运行记录仍不存在；
4. 旧环境：`python -I batch_run_upgrade_probe.py reject <同目录>`，确认 patch_wrong_database；
5. 新环境：`python -I batch_run_upgrade_probe.py execute <同目录>`，先再次验证升级未改历史，然后显式执行旧 approved 组，检查全部应用与只核对不重写；
6. 旧环境再次 reject，确认执行后也不能让旧 reader 接管。第5步已产生新事实，之后不再用“历史完全未变”的 upgrade 探针检查同目录。

另以真实 `09cb6d6` 的 v1 wheel 与 `patch_ledger_upgrade_probe.py` 验收跨两级升级，保留旧 pending/approved/applied 单文件事件。包版本仍为0.1.0；Agent v6、Session migration7 和供应商依赖未变，不需要真实模型、SSH 或中间件。


## 整组调用桥接安装（0.5.3c3a）

c3a 本片只有新宿主契约/桥接，当时未改变 Agent v6、Session migration7、副本账本v3或依赖。已有单文件与组账本不因安装新 wheel 而迁移、批准或执行；包版本仍为0.1.0，部署应记录具体 Git 提交和 wheel 文件摘要。旧 Schema 和单文件实现保持不变。

基础 wheel 无需 OpenAI/Anthropic SDK 即可运行 `examples/batch_patch_bridge.py`；将示例复制到仓库外，用安装环境 `python -I batch_patch_bridge.py` 验收，避免误从源目录导入。新入口是宿主 API，不是模型批量写开关；不能传入旧 Kernel 的 `patches` 参数冒充单文件端口。先关闭/排空桥接，再关闭副本，原 Session 宿主仍负责持久准入。

c3b 已新增 Agent/Session 契约及最低 reader 的实际升级验证，见下节。c3c2 已通过独立端口对接 Diff Artifact；原只读 Artifact 发布器仍不接受写调用。无需新数据库、模型请求或远程部署。

## 历史 Session v7 / migration8 升级（0.5.3c3b）

本节交付时新 wheel 启动应用到 `0008_managed_patch_batch.sql`，只增加最低 reader 标记，不重写既有 v1–v6 事件/投影；新追加或显式 rebuild 的投影为7。受管副本账本保持v3，旧组/单文件工具定义及依赖不变。包版本仍0.1.0，部署须记录具体提交与 wheel 摘要，不凭包版本识别能力。

停机并一致备份 Session 和完整受管副本后升级。旧 reader 必须拒绝 migration8；回退仅使用升级前一致备份，不删迁移记录或修改版本号降级。升级本身不会消费旧 WAITING、批准或写文件；过期按原 Turn 时限处理。

独立可复现探针 `scripts/kernel_batch_upgrade_probe.py`（不依赖测试包）已验收：

1. 从真实 `git archive 6a7cc65` 构建旧 wheel，并为旧/新 wheel 分别建立仅基础依赖环境；复制探针至仓库外；
2. 旧环境：`python -I kernel_batch_upgrade_probe.py create <新目录>`，生成真实 v6 只读与单文件两类 WAITING；
3. 新环境：`python -I kernel_batch_upgrade_probe.py upgrade <同目录>`，验证旧事件、原投影字节、源/副本 inode/mtime/ctime 不变，不答复审批；
4. 旧环境：`python -I kernel_batch_upgrade_probe.py old-reader <同目录>`，明确 schema_too_new 且不改历史；
5. 新环境：`python -I kernel_batch_upgrade_probe.py resume <同目录>`，显式批准并实际完成两类旧审批，新事件为v7，原事件字节和源目录不变，Replay 一致；
6. 旧 reader 再次拒绝新库。`upgrade` 模式检查初始投影原文，须在 `resume` 前使用，不用于已继续会话。

另由旧 wheel 在独立目录完成单文件任务并导出 `tests/agent/fixtures/session-v6.json`；CI 的升级/事务故障夹具基于该真实 transcript 和冻结迁移。两个真实迁移进程退出分别覆盖 marker8 插入未提交与事务提交后，重开只见完整7或8，不混合/重写旧数据；夹具不替代真实 wheel 验收。

新示例 `examples/kernel_batch.py` 可以复制到仓库外用基础 wheel 的 `python -I` 执行，无 OpenAI/Anthropic SDK、API Key 或新中间件。它使用显式专用端口；不要将桥接传给旧单文件 `patches` 或通用写注册表。取消等待或证明缺失时 unknown 是保守恢复结果，不通过重放降低不确定性。

## 差异报告准备安装（0.5.3c3c1）

c3c1 当时仅新增宿主报告 API 与独立 JSONL 契约，没有数据库迁移、供应商依赖或模型工具变更。Agent v7、Session migration8、Provider v3、副本账本v3及旧工具定义不变；包版本仍0.1.0，请继续记录 Git 提交和 wheel 摘要。

基础 wheel 安装后，将 `examples/batch_diff.py` 复制到仓库外，可直接运行 `python -I batch_diff.py`。示例使用真实本地 Session 和受管副本、离线决策，未调用模型 API；无需新数据库、服务器登录或中间件。当时报告通过 `to_jsonl()` 返回 bytes，不提供归档引用；当前示例已按 c3c2 更新为事务归档与读取，见下节。

历史报告需要一致保留完整副本镜像/账本和原调用/批准/效果。缺失事实或快照不匹配时拒绝生成，不对当前文件猜测效果，也不自动 reconcile。取消/超时/关闭须等报告线程排空；报告生成失败不回滚此前真实写入。c3c2 才新增事务发布及其实际 reader 升级步骤。

## 当前 Session v8 / migration9 升级（0.5.3c3c2）

本节取代上文“当前 v7”的版本说明；历史段落保留当时的验收事实。停宿主并一致备份 Session 与完整受管副本后升级。migration9 同事务复制旧 Artifact 行、替换表并增加用途唯一约束；旧行用途为 `tool_result`，正文/manifest/引用与历史事件、投影原字节不改。新引用需要 Agent v8，新写或 rebuild 的投影为8。最低 reader 不允许旧 wheel 接管新库；不通过删 migration 降级。

`SQLiteBatchDiffPublisher` 为显式宿主配置，不自动打开写端口；旧只读发布器的成功只读限制保持。升级本身不生成报告、不消费等待审批、不执行文件写入。旧整组 WAITING 可以显式决定/恢复执行并取得新的效果引用，但不会回填或修改过去的计划审批事件。

独立基础安装环境的实际探针为 `scripts/batch_diff_upgrade_probe.py`：

1. 从 `33e690e33395876b2d5357071d947e08f765c23e` 导出旧源码构建 wheel，另构建当前 wheel，各自安装到仓库外环境；
2. 旧环境运行 `python -I batch_diff_upgrade_probe.py create <新目录>`，实际创建只读/单文件/整组三类 WAITING 和已归档只读结果；
3. 新环境运行 `upgrade <同目录>`：旧事件、投影、Artifact manifest/正文、目标文件身份/时间不变；旧引用仍可读；
4. 旧环境运行 `old-reader <同目录>`，确认 `schema_too_new` 且不改变旧事件；
5. 新环境运行 `resume <同目录>`，显式批准/完成三类旧调用，整组真实效果附带引用，旧事件原字节不变，Replay 一致；旧环境再次 `old-reader` 仍拒绝。

另由旧 wheel 在独立目录执行 create/fixture 导出真实 v7 整组 transcript，以及旧只读 Artifact 的原事件/投影/表行夹具纳入 CI，非手改版本号。migration9 的复制、删除旧表、重命名及提交后四个真实进程退出均验收为完整旧库或完整新库。

当前 `examples/batch_diff.py` 已更新为计划/效果双引用归档闭环，仍可用基础 wheel 在仓库外 `python -I` 运行，无供应商 SDK 或模型请求。包版本仍0.1.0，安装时记录 Git 提交和 wheel 摘要；无需远程服务器或新中间件。

## 受信宿主进程层安装（0.5.4a）

基础wheel新增 `harnessix.processes`，不需要额外依赖、模型API、服务器或中间件。仅验证macOS/Linux的本地文件系统及Python3.12/3.13对应CI；Windows明确拒绝，不静默降级。包版本仍0.1.0，记录具体Git提交与wheel摘要。

新示例 `examples/host_process.py` 可复制到仓库外，安装基础wheel后用 `python -I host_process.py` 运行。它只启动脚本内固定的受信Python命令，验证双流捕获/完整排水、超时和直接子进程回收，不运行用户仓库的测试或安装脚本。

宿主必须显式选择cwd、可执行文件表和环境；无隐式shell、stdin或父进程环境透传。关闭事件循环前调用并等待 `aclose()`，或使用异步上下文管理器。进程组不能隔离文件/网络访问，也不能保证脱组后代、宿主硬崩溃或不可中断内核状态的整体清理。不要将本层直接注册为免审批模型工具；持久准入与更强containment后续单独验收。

此片无Agent/Session/副本迁移，旧Schema与既有工具定义保持。无需因安装本片对旧Session进行降级、重建或执行等待审批。

## 持久进程Action部署（0.5.4b1）

`process_action_tool(factory)`仅由宿主显式加入自己的ToolRegistry；默认`build_registry`不注册。工厂必须每次创建绑定一致的全新`HostProcessRuntime`，工具版本会包含其权限摘要。SQLite/PostgreSQL Effect Journal、Policy和Worker配置沿用0.1，无新表、迁移或中间件；API与Worker必须部署相同工具版本和宿主绑定，否则批准后的执行会在启动前失败。

命令请求必须带幂等键并等待审批。argv、程序别名和超时属于持久Action正文，禁止在其中放凭据；本片遇到SecretRef会在不启动进程的前提下失败。退出码非零仍是已观察到的进程结果，运维判断测试成败必须读取ProcessResult。UNKNOWN和MANUAL_INTERVENTION不得投入READY队列或由Worker重试。

宿主硬退出时Journal可以在租约过期后恢复UNKNOWN，但0.5.4b1没有外部进程监督器，子进程仍可能存活。运维只能按部署环境核查，不能对持久PID/PGID直接发信号。示例`python -m examples.process_action`验证持久准入闭环，不验证Sandbox或Agent模型调用。

## Agent/Process稳定身份部署（0.5.4b2b1）

本片增加`AgentProcessCallPlan`和受信准备/快照核对API，没有Session或Effect Journal迁移；该片交付时最低reader仍是Agent v8 / migration9。安装新wheel不会创建Action、批准请求、启动进程或改写旧Session。包版本仍为0.1.0，部署必须记录具体Git提交和wheel摘要。

宿主只能把同一Process Action `ToolDescriptor`同时用于模型ToolCall构造和桥接，并提供稳定、受信的`Principal`。API与Worker宿主绑定不同会产生不同工具版本，旧计划不能继续。Action请求提交后的状态、决定和结果只从原Effect Journal读取；Session接入尚未交付，不得自行把计划或普通Session `ApprovalRecord`传给Executor。

独立Schema`agent-process-call-plan-v1`用于持久兼容检查。b2b2已升级Agent事件和Session最低reader并完成真实旧包验收，见下节；仍不能删除既有migration记录、手写事件或把`host.process`加入默认Agent工具表来提前开放能力。

## Session v9 / migration10–11进程投影与Artifact历史升级（0.5.4b2b2–b2c2）

b2b2由`0010_agent_process_projection.sql`把最低reader推进到Agent Event/Thread v9；migration10不新增表、索引或列。b2c2的`0011_process_output_artifacts.sql`事务复制Artifact表，仅把`process_output`加入purpose白名单。两次迁移都不重写旧事件、快照或Effect Journal；旧Artifact正文、manifest和purpose保持原字节。0.6.1之后的新写或显式rebuild投影为v10，并追加不改表的migration12；v1–v9 Schema文件保持冻结。升级前仍应停止Session宿主并做一致备份，回退只能恢复备份，不能删除migration marker或下调投影版本伪装降级。

Runtime重开v9的WAITING_ACTION仍只保留原等待，不会在启动时创建、批准、执行或轮询Process Action。b2c1配置原专用端口后，调用方可显式`resume_turn`单次读取匹配Action并投影；普通Session审批或手写`ToolResult.process`仍不可绕过。Effect Journal、Worker、API的Process ToolDescriptor和Principal必须继续一致，Action Approval仍是唯一许可。

真实跨安装验收使用`scripts/process_session_upgrade_probe.py`，探针不依赖测试包：

1. 从`git archive e0e849813942b21452ba1943d5cca3a5f936e5f6`构建真实v8 wheel，并与当前wheel分别安装到仓库外基础环境；
2. 旧环境执行`python -I process_session_upgrade_probe.py create <空目录>`，创建包含真实只读工具调用的v8完成会话、migration1–9及冻结fixture；
3. 新环境执行`upgrade <同目录>`，确认migration10/11只追加兼容迁移，旧事件、投影、前九个校验和及数据库inode不变；
4. 旧环境执行`old-reader <同目录>`，确认`schema_too_new`且整个可见数据库状态不变；
5. 新环境执行`resume <同目录>`，追加v9完成Turn，确认旧v8事件原字节、Replay和projection version 9；旧环境再次拒绝。

实际旧wheel SHA256为`d0d5ba4322ddaa846565478901932335a5a89f3d26da3804df0155c022601d93`，b2b2a基础wheel为`7a8d189119d978240cd10b5efab7ecb3a13d453a08609fa16eb56a1c753fae04`，b2b2b基础wheel为`e7a85fc4af22bea55ebd2d4db963890a774fbfbf3b0526d42899a4e86ef6dd84`。旧wheel直接导出的`tests/agent/fixtures/session-v8.json`纳入回归，SHA256为`f8c5413a0d0af920b6c1fcd4e7e286fb14b000045a5832b29663c26c11f02cc3`。migration10提交前后及migration11复制/删表/重命名/提交后均以真实`os._exit`验证，重启只看到完整旧库或完整新库，不重写历史。

b2b2已完成同版本Replay、重启保留等待、冻结Schema、真实旧wheel升级和迁移硬退出验收。b2c1提供显式模型进程端口，b2c2提供Process Artifact和migration11，b2c3补齐跨库恢复、取消、租约UNKNOWN和双SDK闭环。默认Agent仍不暴露`host.process`。基础wheel无需供应商SDK、远程数据库或新中间件。


## 显式Process Agent运行时部署（0.5.4b2c1）

API/Agent宿主必须显式构造`ProcessAgentBridge(actions, principal)`并以`processes=`注入`AgentRuntime`。传入的ActionService必须设置`auto_execute=False`，且Registry中唯一的`host.process`定义、Principal、cwd/程序表/环境/资源绑定必须与独立Worker完全一致。桥接不拥有ActionService生命周期；宿主先初始化Effect Journal，关闭时在Agent Runtime退出后再关闭ActionService。

推荐部署角色保持分离：

1. Agent/API进程写Session、提交Action和写唯一审批决定；
2. Action Worker从Journal领取READY并执行固定程序；
3. 客户端或上层调度器在收到状态变化后显式调用`resume_turn`一次。b2c1不提供后台轮询器，不能用紧循环调用resume替代队列通知。

审批接口返回WAITING_ACTION不表示命令完成。只有Action终态被再次读取并写入Session结果后，Agent才继续模型循环；UNKNOWN/MANUAL_INTERVENTION会中断Turn。公开模型结果只有流计数/摘要和生命周期，不直接包含完整stdout/stderr。b2c2配置正确时结果会附带受作用域保护的Artifact引用；运维仍应把Effect Journal中的Action Result视为效果事实，不能用Artifact替代。

同一决定重答可修复Action已决定、Session未投影的窗口；不同actor/outcome/reason会冲突。重启后的WAITING_APPROVAL可由`resume_turn`只读同步已有Action决定，WAITING_ACTION可单次观察。若Action由外部入口在Turn超时后形成决定，Session仍按Action真实决定时间补投影，但原Turn预算不会复活。Action创建后而Session审批请求尚未提交、等待取消和完整跨库退出矩阵现已由b2c3验收；仍不得配置自动重放非幂等命令或手改Journal状态绕过。

基础wheel可在仓库外执行`python -I kernel_process.py`（复制自`examples/kernel_process.py`）验证离线闭环和Process Artifact。该示例不需要供应商SDK、API Key、远程数据库或新中间件；不是任意Shell、仓库测试执行或OS Sandbox。包版本仍为0.1.0，生产记录必须使用具体提交和wheel摘要。

## Process Artifact部署（0.5.4b2c2）

Agent宿主在同一个Session对象上创建`SQLiteArtifactStore`，并把同一个`ProcessAgentBridge`与`CodingToolRuntime.workspace_scope`传给`SQLiteProcessArtifactPublisher`：

```python
artifacts = SQLiteArtifactStore(sessions)
async with CodingToolRuntime(workspace, artifacts=artifacts) as tools:
    process_artifacts = SQLiteProcessArtifactPublisher(
        artifacts,
        processes,
        workspace_scope=tools.workspace_scope,
    )
    async with AgentRuntime(
        sessions,
        provider,
        scoped_tools=tools,
        artifacts=artifacts,
        processes=processes,
        process_artifacts=process_artifacts,
    ):
        ...
```

三个对象必须共享原Session/桥接/scope；不要从模型参数接受数据库路径或scope。未配置发布器时保留b2c1行为，只产生有界摘要。达到Artifact配额、正文编码后超过1 MiB或发布失败时，结果可以没有引用；这不是Action失败。若业务必须长期保留完整日志，应从Effect Journal建立独立受控导出/保留流程，不能放宽Session Artifact上限或TTL后仍宣称上下文有界。

`process-output/v1`正文可能包含源码、测试输出和秘密；SQLite文件、备份与导出按源代码资产保护。现有Artifact不加密，过期清理保留tombstone且不保证立即缩小数据库文件。工作区scope不是访问令牌；网络API暴露`read_artifact`前仍需上层认证授权。迁移11后旧wheel必须拒绝数据库，回滚只能恢复一致备份。

migration11 SHA256为`12295e83c718c367ae0da730ea39395663728752d33cc24b620d3ee5c70104e2`。本片基础wheel SHA256为`2ec6c89e2be650cd01654e8567dd44775d6ef52c0825d42cb481d63189b4a4ee`；实际部署仍必须记录最终Git提交和CI结果，不能只依赖包内仍为0.1.0的版本号。

## Process Saga恢复与取消部署（0.5.4b2c3）

b2c3不需要数据库迁移或新中间件。升级前仍应保证Agent/API和Worker使用相同代码版本、`host.process`工具描述、Principal、工作目录、程序允许表、环境与资源限制。Runtime启动遇到“Process ToolCall已提交但Session审批Item缺失”时，只有配置匹配的`ProcessAgentBridge`才会按稳定身份补Action/审批请求；未配置时保持原事实，交由正确宿主接管。

WAITING审批或Action的Session取消不是队列撤销。客户端应显示“已停止等待，外部效果未知”，不能显示“命令已终止”。PENDING、READY或RUNNING Action仍由Effect Journal和Worker管理；若业务要求撤销READY，当前版本没有该协议，不能删除行、修改状态或只取消Session。正常关闭Runtime也会保留等待供重开，不会隐式决定审批。

Worker的RUNNING/RECONCILING租约过期会写入`UNKNOWN`结果和`lease_expired`错误。监控至少告警`unknown_count`和`lease_recovered -> UNKNOWN`；不得把此状态重投READY。该结果只说明执行所有权丢失，不说明OS进程已经退出。生产部署应通过容器、systemd/launchd或独立监督器约束Worker及其进程组；Harnessix当前不会依据持久PID清理孤儿。

多API实例可以竞争同一Action审批，数据库事务保证只有一个权威决定。调用方收到`approval_conflict`后应读取并展示已存在决定，不得改actor/reason后自动重试。SQLite适合单机多进程；多主机部署使用PostgreSQL，并在发布门禁运行实库租约UNKNOWN与并发测试。

OpenAI/Anthropic SDK闭环测试使用离线Mock传输，不需要生产Key。线上Secret仍只能通过Provider配置的环境引用提供；Process argv不支持SecretRef解析，禁止把API Key、密码或令牌作为命令参数持久化。Process Artifact只受Session/工作区作用域保护，备份、导出和API读取仍需上层认证授权。

## Git与测试Profile部署（0.5.4c）

本片不增加数据库迁移、第三方Python依赖或远程中间件。它复用当前Agent v9、Session migration11、Action/Process/Artifact v1和副本账本v3；升级wheel本身不会启用Git、注册测试命令、创建Action或执行仓库代码。包版本仍为0.1.0，部署必须记录精确Git提交和wheel SHA-256。

### Git能力

Git工具是opt-in配置。宿主应从受控安装中解析一次绝对可执行文件并传入：

```python
from pathlib import Path
from harnessix.tools.runtime import CodingToolRuntime

tools = CodingToolRuntime(workspace, git_executable=Path("/usr/bin/git"))
```

路径必须是当前平台实际存在且可执行的普通文件；运行时会绑定文件身份，替换或升级可执行文件后应重建Runtime并视为新工具版本。不要接受模型、仓库配置或请求参数提供该路径。工作区必须是精确Git顶层目录，不能把父仓库的任意子目录当作独立授权范围。

当前固定环境会关闭全局/系统Git配置、交互、分页器和可选锁，固定参数会关闭Hook、fsmonitor、external diff和textconv。部署不应通过包装脚本恢复这些能力；如需公司级配置，应作为新策略版本经过威胁建模、测试和审批。Git读取使用5秒时限、有限进程捕获、状态最多200项和Diff 48 KiB公开前缀，不适合把巨型生成目录当作无限查询接口。

### 测试Profile

测试能力必须在同一规范工作区中同时绑定Process Action和受限前端：

```python
import sys

from harnessix.domain.models import Principal
from harnessix.domain.registry import ToolRegistry
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.processes.test_contracts import TestProfile
from harnessix.processes.test_profiles import RunTestsAgentBridge

registry = ToolRegistry()
registry.register(
    process_action_tool(lambda: HostProcessRuntime(workspace, {"python": sys.executable}))
)
# service必须auto_execute=False，并由独立ActionWorker消费。
tests = RunTestsAgentBridge(
    service,
    Principal(tenant_id="tenant", subject_id="agent", framework="harnessix-agent"),
    workspace,
    (
        TestProfile(
            name="unit",
            description="项目单元测试",
            program="python",
            arguments=("-I", "-m", "pytest", "-q", "tests/unit"),
            timeout_seconds=300,
        ),
    ),
)
```

生产配置要求：

1. Profile名称、说明、程序、argv和超时进入代码评审与配置变更审计；
2. 程序别名必须存在于`HostProcessRuntime`固定表，Profile时限不能超过进程上限；
3. 不在argv中放置Token、密码或其他凭据，完整argv会持久化到Effect Journal；
4. `ActionService(auto_execute=False)`与独立Worker保持，审批接口不能兼任命令执行；
5. 若需要完整日志，显式绑定`SQLiteProcessArtifactPublisher`并按既有Artifact配额、TTL和访问控制部署；
6. 工作区、程序文件、Profile或Process资源策略改变后创建新Runtime，不能尝试让旧调用沿用新配置；
7. 不可信仓库测试应在后续容器/网络隔离后再进入生产；当前宿主进程边界不限制文件和网络权限。

测试断言失败表现为`ActionStatus.SUCCEEDED`且Tool Result `passed=false`，因为执行生命周期已确定；运维告警不能把它和启动失败、超时、清理失败或UNKNOWN混为一类。Session等待取消只停止Agent观察，不撤销已经批准的Action，沿用ADR 0042处置流程。

离线安装验收：

```bash
python -I coding_feedback.py
```

将`examples/coding_feedback.py`复制到仓库外，以只安装基础wheel的Python运行；需要系统Git，不需要OpenAI/Anthropic SDK、API Key、SSH或数据库中间件。示例在临时目录运行固定Python测试并修改私有受管副本，源目录保持不变。生产上线前仍须在目标OS、实际Git版本、隔离后端和组织审批策略上单独验收。

## Coding Eval评分与报告部署（0.5.5a）

本片不增加数据库迁移、第三方依赖、远程服务或中间件。安装新wheel只提供`harnessix.evals`契约、评分器、Git证据采集和报告读写函数，不会自行发现仓库、运行测试、调用模型、批准Action或修改文件。

宿主接入顺序必须是：加载并校验固定任务→由0.5.5b编排器物化私有缺陷基线→运行基线检查→驱动同一个`AgentRuntime`/Action Worker→运行最终隐藏检查→`collect_git_evidence`→`grade_coding_eval`→`write_eval_report`。不得从模型参数接受任务文件、隐藏检查、Git路径、报告路径、Provider标识或基线摘要。

报告目录由宿主预先创建并置于被评工作区之外。写入使用同目录0600临时文件、文件和目录`fsync`及原子替换；父目录缺失等I/O异常统一返回`eval_report_write_failed`，目标符号链接返回`eval_report_path_denied`。读取拒绝符号链接、非普通文件、超过1 MiB或Schema损坏的内容。报告仍包含仓库内相对路径、模型名和平台摘要，应按内部质量记录控制访问、备份和保留期限。它不包含Prompt、Diff、测试输出、Session正文或模型summary，但不提供通用DLP或加密。

0.5.5a没有隐藏检查执行器。调用方自行构造`EvalTestObservation`只能用于测试或可信内嵌编排，不能作为远程第三方提交的证明。后续运行器必须把检查命令和输出留在宿主边界，只将退出码、耗时和SHA-256交给评分器；检查输出若可能包含Secret，应按Process Artifact同等级保护，不写入Eval报告。

`invalid`表示任务树、缺陷基线或最终检查集合不可用于统计，不能计入模型失败率；`failed`才表示任务可运行但Agent未满足要求。任何报告写入失败都应让本次发布记录失败，不能仅根据内存中的`passed`结论更新基线。当前未实现报告聚合、签名、远端上传、多次试验统计或源目录交付。

## 历史Eval物化与隐藏检查部署（0.5.5b1）

本片不增加数据库迁移、远程服务或中间件。运行前必须具备：包含固定revision的Harnessix完整Git历史、受控Git绝对路径、预先创建且仅服务账户可访问的运行根，以及安装任务测试依赖的Python词法绝对路径。仅安装基础wheel足以导入Catalog和物化器，但本任务隐藏检查会复用历史树中的OpenAI测试夹具，推荐使用：

```bash
uv sync --locked --all-extras --dev
git cat-file -e 9f24961840fa704e7c7a344c648164d8afe793b7^{commit}
```

浅克隆缺少固定对象时应让运行失败，不允许从可变分支或网络临时下载内容补齐。CI的Python和macOS Eval作业使用`fetch-depth: 0`；PostgreSQL作业不运行历史Eval，无需完整历史。

受信宿主按以下顺序准备工作区：

```python
import shutil
import sys
from pathlib import Path
from uuid import uuid4

from harnessix.evals import (
    historical_coding_eval,
    materialize_historical_coding_eval,
    run_historical_checks,
)

task = historical_coding_eval("harnessix-openai-empty-incremental-call-id")
materialized = materialize_historical_coding_eval(
    Path("/srv/harnessix/source"),
    Path("/var/lib/harnessix/evals"),
    Path(shutil.which("git") or ""),
    task,
    uuid4(),
)
baseline = await run_historical_checks(task, materialized, Path(sys.executable), "baseline")
```

不要对`sys.executable`调用`resolve()`：venv入口的词法路径用于保留虚拟环境包发现语义。物化器会验证其最终目标是可执行普通文件，并在运行目录的`host/`内发布固定0700入口；模型不能提供该路径。

运行根不得位于被评工作区内。每个UUID目录权限为0700，`materialization.json`权限为0600且最后发布；只有存在有效`ready`清单的目录可以重开。相同运行ID重开会保留未提交修改并核对HEAD/基线树，不会重新物化。没有清单、清单损坏、符号链接或身份不匹配时，应隔离目录供诊断后由运维显式清理，不能自动覆盖或改用新任务身份。

隐藏检查固定为独立进程、60秒时限和有界双流。退出码1是确定的行为不通过，不应触发基础设施告警；其他退出码、超时、清理失败或不完整输出证据应记录`eval_check_infrastructure_failed`并停止评分；取消传播为Turn取消。日志和指标至少记录任务ID/版本/指纹、运行ID、物化错误码、来源与基线摘要、检查ID/阶段/退出码/耗时/输出摘要，不记录隐藏检查正文、原始测试输出或工作区文件。

当前检查在宿主用户权限下执行，没有容器、网络或文件系统隔离，只允许Catalog中经过评审的Harnessix历史任务。动态仓库、第三方PR和不可信测试必须等待0.7 Sandbox/网络策略后接入。0.5.5b1只负责物化和检查；0.5.5b2的正式编排部署要求见下一节。

## 历史Eval Runtime编排部署（0.5.5b2）

`run_historical_coding_eval`要求调用方提供完整来源仓库、位于来源仓库之外的0700运行根、固定Git/Python绝对路径、内置任务、UUID运行ID、已打开的Provider和不含凭据的环境标识。Provider生命周期及真实费用由调用方控制；运行器不读取环境Key、不打开网络连接或自动重试。

每个运行ID目录同时保存物化清单、0600运行状态/报告、只读物化工作区、受管执行副本及Patch账本、Session SQLite、Effect Journal和宿主启动器。整个目录必须只允许同一服务账户访问，不应放入Web静态目录、通用日志采集或共享卷。备份时必须保持文件权限、SQLite WAL/SHM一致性和目录原子快照；不得只复制`run-state.json`后删除账本。

恢复时使用相同任务版本、Provider/Model环境标识、Git/Python绑定和运行ID再次调用。运行器会核对任务指纹、基线、执行副本、Session索引和报告摘要；不匹配时拒绝接管。状态缺失但`managed/`已经存在表示副本构建未形成发布点，当前不自动删除或覆盖，应隔离该运行目录后由新运行ID重试。

内置自动审批只覆盖任务声明的唯一测试Profile和允许路径单文件Patch。Process批准仍由Effect Journal持久化且只由外部`ActionWorker`执行；运行服务账户必须能写运行目录并执行固定Python，但不需要数据库服务器或远程中间件。现阶段不得将该宿主模式开放给任意上传仓库或第三方测试。

运行状态为`completed`只表示评分报告已原子发布，报告本身可能是`passed`、`failed`或`invalid`。监控应分别统计报告结论、失败分类、Provider/模型标识、耗时、步骤、Token、工具调用和审批数，不得把基础设施`invalid`计入模型失败率。真实Provider多次基线及费用告警在0.5.5c定义。

## Coding Eval Campaign证据部署（0.5.5c1）

Campaign计划必须存放在独立0700目录，并在任何真实请求前调用`write_eval_campaign_plan`发布为0600文件。计划固定任务指纹、Harnessix revision、Provider/精确模型、有序run ID、价格快照和计费上下文；不得在计划旁保存API Key值、Authorization Header或供应商响应正文。

每个run ID仍指向0.5.5b2的独立运行目录。聚合进程只读取completed运行状态、Eval报告、Session中的原Turn及由固定价格绑定生成的CostReport，再调用`build_coding_eval_campaign_report`。缺失任何试验、交叉目录、报告摘要不匹配或成本无法重算时必须停止，不得手工拼接部分报告。最终`write_eval_campaign_report`以0600原子文件发布聚合结果。

Campaign报告包含内部run/turn身份、模型名、失败分类、Token、时延和成本小计，应按内部质量记录限制访问与保留。`partial/unknown`表示存在无法计价尝试，不能解释为零费用；价格快照是估算依据，不是供应商账单。P50/P95在2—20个小样本上只用于版本回归，不构成统计显著性声明。

0.5.5c1自身没有网络入口和费用停止策略，不应使用临时脚本直接循环真实Provider；后续c2a入口及部署要求见下一节。

## 受控Coding Eval Campaign执行部署（0.5.5c2a）

本入口只用于受信运维宿主运行内置历史任务，不是面向终端用户的任意仓库执行服务。安装对应源码revision的Harnessix及所选Provider可选依赖，使用同一专用服务账户，并确保Git、Python和源码仓库路径固定。无需数据库服务器、SSH或远程中间件。

Campaign配置必须位于受限目录内、权限为0600且不是符号链接。配置内只写`api_key_env`名称；API Key值由进程环境或正式Secret Provider注入，禁止出现在JSON、Shell参数、仓库、服务日志和进程标题中。运行根必须位于源码根之外，预先创建时权限为0700；执行器也会核对Campaign根、`runs/`和锁文件权限。

默认禁网检查：

```bash
uv run harnessix coding-eval-campaign --config /private/campaign.json
```

该命令应以JSON返回`network_not_enabled`，且不读取配置。只有完成模型、地域、价格有效期、运行ID、源码revision、最大输出、请求/响应预算和费用授权复核后，才显式执行：

```bash
uv run harnessix coding-eval-campaign \
  --config /private/campaign.json \
  --allow-network
```

同一Campaign同一时刻只能有一个宿主；锁冲突必须停止，不能复制配置到第二目录并并发运行相同run ID。中断后使用完全相同配置重启，执行器会核对计划、配置指纹、完成前缀、单次报告、Session成本和源码HEAD，再决定只读返回、继续固定run或保持停止。不要编辑`campaign-state.json`金额、删除单次账本或用新run ID替换失败试验。

退出结果仅包含固定原因和计数：`completed`表示全部试验及聚合报告发布；`fee_limit_reached`或`cost_unknown`表示持久停止；`runtime_failed`需要离线检查受限运行目录中的正式证据。供应商错误正文不会打印到CLI。`campaign-report.json`和各run目录包含内部身份、代码、Session及账本，应纳入敏感工程数据访问控制。

费用停止线仅在完整试验之间核对。已开始试验可能超过阈值，供应商仍可能收费；生产运维还应独立配置云账户预算告警并对账。当前没有请求级硬费用中止或OS Sandbox，不得把本入口暴露为接收不受信仓库的多租户服务。

首轮百炼北京三次运行的配置边界、脱敏报告、费用和预算失败根因见[验证记录](validation/bailian-2026-09-06-coding-eval/README.md)。该Campaign已经完成，禁止通过编辑状态或追加run改写基线；预算修复必须提升任务版本并创建新Campaign。

## Coding Eval任务预算版本升级（0.5.5c3a）

安装本片wheel不修改数据库或已有运行目录。内置任务Catalog同时包含v1和v2：省略版本的受信调用方将选择最新v2；任何恢复、审计或报告重算都必须从已持久计划/状态读取精确`task_version`和`task_fingerprint`，不得再次调用“最新版本”替代原身份。

旧v1 Campaign保持只读：

```python
from harnessix.evals import historical_coding_eval

definition = historical_coding_eval(
    "harnessix-openai-empty-incremental-call-id",
    1,
)
assert definition.task.budget.max_tokens == 20_000
assert definition.task.fingerprint == (
    "ea75be4219574ff398cc252d3b5a8f870cea2f7cbb20cbe51e12e7997e921297"
)
```

新Campaign显式选择v2并在首个请求前固定其新指纹：

```python
definition = historical_coding_eval(
    "harnessix-openai-empty-incremental-call-id",
    2,
)
assert definition.task.budget.max_tokens == 100_000
assert definition.task.fingerprint == (
    "011268310f3aac1b3025d643aaf8361b807bd9d9b55a19436caa05507d122bfb"
)
```

生产变更检查应同时记录任务ID、版本、指纹、步骤/累计Token/时间/输出限制、Provider单步输出上限和费用停止线。100000是Turn累计报告Token上限，不得写入模型上下文窗口配置，也不得据此提高自动重试、工具并发或工作区权限。

升级验收至少执行一次v1和v2离线Campaign并重开completed状态，确认没有再次创建Provider。未知版本或计划指纹漂移必须在Provider创建前失败。真实v2 Campaign必须使用独立授权、新Campaign/run ID和私有0600配置；不得复制旧状态、复用旧run或把新结果追加到首轮报告。

## 任务v2真实Campaign归档（0.5.5c3b）

任务v2 Campaign `ee2ccba3-20da-46c0-9e99-8b8597a461c3`已经完成并发布报告。该运行目录和0600配置只能用于审计与只读恢复；不得编辑状态、删除单次证据、替换run ID或继续发送请求。仓库仅保存脱敏计划、聚合报告和正式分析，见[任务v2验证记录](validation/bailian-2026-09-06-coding-eval-v2/README.md)。

本次三个run均在分页工具校验错误连续重试后达到100000累计Token。运维侧不得通过复制配置并提高任务预算绕过该失败：费用停止位于完整试验之间，无法控制单个失控Turn；重复运行只会扩大输入历史和费用。后续部署顺序固定为先安装有界工具校验反馈实现并完成离线回归，再创建全新Campaign配置、Campaign ID和run ID。新的真实运行仍须单独复核模型、地域、价格有效期、请求次数和费用停止线。

## 分页工具可纠正校验反馈升级（0.5.5c3c）

本片是兼容行为修正，不执行数据库迁移，也不改变工具输入/输出Schema或定义指纹。安装新wheel后：

- 尚未执行且满足专用条件的分页调用会得到`tool_expected_revision_required`；
- 已持久化的旧`tool_invalid_arguments`结果保持原字节，不回写或重新分类；
- Session重开继续从既有结果恢复，不重放失败工具；
- OpenAI-compatible和Anthropic配置、Provider尝试策略、工作区权限与审批策略无需变更。

部署验收应在禁网环境运行直接工具、SQLite重开/Replay和两个SDK离线HTTP纠正闭环，并确认Schema生成无差异。不要在网关或客户端把`retryable=false`改写为基础设施重试；模型若修正参数，必须产生新的工具调用身份。真实c3d Campaign应从安装并验收该wheel的新进程启动，使用全新Campaign/run ID和独立费用授权。

## 分页纠正后任务v2归档（0.5.5c3d）

Campaign `0a0ee9f3-8d4d-46cb-8e38-b1956a7068d8`已完成并发布报告，只允许审计和只读恢复。三个run均采用专用分页错误并成功纠正；不得继续追加run、替换最终回答或把两个正确Patch的私有工作区直接视作源目录合入授权。脱敏计划与报告见[验证记录](validation/bailian-2026-09-06-coding-eval-v2-corrected/README.md)。

## 最终回答契约任务v3升级（0.5.5c3e1）

安装本片wheel不迁移数据库。Catalog包含v1、v2和v3；省略版本时选择最新v3，所有恢复和报告重算仍必须从持久计划读取精确版本与指纹。v3只改变任务Prompt，运行边界与v2一致。

部署方不得在Provider网关剥离Markdown围栏或把自然语言转换为JSON，这会让线上请求与评测证据分叉。新Campaign必须在请求前固定v3指纹`6e7408ce04696ecdf7e72a6ea035504732e4cbdae03b663475a69b5cddd56724`，使用新Campaign/run ID和有效价格快照。已有v1/v2 Campaign继续使用原版本，不得升级原地恢复。

## 任务v3真实Campaign归档（0.5.5c3e2）

Campaign `b98a76ad-a586-4b98-aa95-fd62276380f6`已经完成3/3严格通过并发布报告。运行目录、0600配置、Session、受管副本和Provider响应只用于私有审计；不得追加run、替换回答或从私有工作区直接覆盖源目录。仓库只保存脱敏计划、聚合报告和正式分析，见[任务v3验证记录](validation/bailian-2026-09-06-coding-eval-v3/README.md)。

该Campaign完整已知估算成本为¥0.828428；与分页纠正后任务v2诊断Campaign合计¥2.083924。价格快照估算不替代供应商账单对账，部署监控仍须区分实际账单、未知Usage和试验间停止线。

## 受控Eval变更交付安装（0.5.5d）

本片新增纯库API与三份JSON Schema，不新增数据库迁移、守护进程、网络入口、模型请求或中间件：

```python
from harnessix.domain.models import ApprovalOutcome, ApprovalRecord
from harnessix.evals import CodingEvalDeliveryStore, build_coding_eval_change_package

package = await build_coding_eval_change_package(runs_root, git, definition, run_id)
deliveries = CodingEvalDeliveryStore(private_state_root, git)
pending = deliveries.prepare(package, target_repository_root)

# 宿主展示并审核pending.plan后，保存与计划指纹绑定的决定。
deliveries.decide(
    pending.delivery_id,
    ApprovalRecord(
        outcome=ApprovalOutcome.APPROVED,
        actor="release-owner",
        reason="已核对来源、路径和前后镜像",
        request_fingerprint=pending.plan.approval_fingerprint,
    ),
)
result = deliveries.execute(pending.delivery_id, target_repository_root)
```

`private_state_root`必须是当前用户0700目录，并位于目标仓库之外；其下每个交付目录为0700，`owner.lock`、`package.json`和`state.json`为0600。服务升级或进程退出后，以相同状态根、Git绑定、交付ID和目标根创建Store并先调用`reconcile`。不得删除临时文件、手改state或仅凭目标内容相同把unknown改成applied。

目标仓库必须停在变更包的精确source commit，配置相同origin，且index、工作树和untracked全部为空。`prepare`通过后到`execute`之间的任何来源、状态、路径、权限、inode或前镜像变化都会拒绝写入。若业务允许用户同时编辑同一仓库，宿主必须先获取仓库级独占任务租约；本地`flock`只串行同一交付ID，不是跨主机分布式锁。

`applied`只表示工作树后镜像已原子写入并完成inode归因。部署方仍需在外层执行代码评审、完整测试、commit、签名、push和发布；不得把这些动作拼进本片批准。多文件、创建/删除、rename、二进制、三方合并和自动回滚必须等待新版本契约。

## Tool并发契约升级（0.5.6）

本片不新增数据库迁移、后台服务、API Key、网络或中间件。公开OpenAPI的`ToolDescriptor`新增非必填`supports_parallel_calls`，旧载荷缺失时读取为`false`。该字段属于受信工具定义且进入完整工具指纹，不能由客户端或模型调用参数设置。

### 滚动升级顺序

1. 停止向待升级实例分配新Turn；
2. 等待在途Turn完成、取消或到达持久审批/Action边界；
3. 关闭Agent/Coding Tool Runtime，确认Workspace目录FD与后台任务已回收；
4. 部署新wheel并重新生成/发布OpenAPI；
5. 使用默认并发上限启动，完成离线双读取、串行屏障、取消和关闭检查后再恢复流量。

终态Session无需迁移。升级前尚未完成的Tool Call保存的是旧Descriptor指纹，新Runtime会以`tool_contract_changed`失败关闭，不会在新并发语义下重放；部署方不得编辑Session指纹绕过门禁。若业务必须保留等待审批的旧Turn，应先由旧实例完成或取消，再升级。

### 宿主配置

Kernel和只读工具层分别有独立上限：

```python
tools = CodingToolRuntime(workspace, max_concurrent_reads=4)
runtime = AgentRuntime(
    session_store,
    provider,
    scoped_tools=tools,
    max_parallel_tools=4,
)
```

两项都必须是严格整数且位于1—16。Kernel限制同一Provider批次，Coding Tool限制单实例实际读取资源；有效并发不会超过较小值。生产首发保持默认4，只在文件描述符、线程池和存储延迟指标证明有余量后调整，不能把上限改成无限。

`host.process`、Patch及审批调用不会因该配置并行。非交互命令继续要求宿主显式装配预绑定程序、Process Agent Bridge、Action Worker和唯一审批；默认Bootstrap不广告Process。跨进程Workspace锁、容器隔离、网络/Secret策略仍须等待0.7，不能把单进程信号量作为多租户安全边界。

### 监控与回退

监控至少区分Turn状态、`FailureCategory.TOOL`、稳定错误码、工具时延、取消和Runtime关闭时长。0.5.6会把此前误归为`internal`的`patch_*`、`process_*`、`artifact_*`、`test_*`、`git_*`和`workspace_*`错误计入`tool`；告警仪表盘应同步调整，但历史事件不回写。

出现资源压力时先把两个上限降为1，即恢复等价串行调度，不需要数据库回滚。回退旧wheel前也必须排空在途Turn；旧代码读取带新字段的严格Descriptor可能失败，因此回退不承诺跨版本未完成调用继续执行，终态历史仍作为审计事实保留。

## Context规划切片安装（0.6.1）

0.6.1新增Agent Event/Thread v10与Session Migration 0012。迁移只写最低reader版本标记，不改写旧Event、Thread JSON或Artifact。滚动升级仍应先排空活跃Turn；旧wheel不能读取v10投影，回退前必须确认数据库尚未由新版本追加事件，禁止手工下调`projection_version`。

Context Engine必须使用对应模型和端点的显式窗口配置，不能把`Budget.max_tokens`累计消费上限当成上下文窗口：

```python
from harnessix.context import ContextEngine, ContextFragment, ContextFragmentKind, ContextLimits

context = ContextEngine(
    ContextLimits(
        context_window_tokens=131_072,
        reserved_output_tokens=8_192,
        provider_overhead_tokens=1_024,
        safety_margin_tokens=2_048,
    ),
    (
        ContextFragment(
            kind=ContextFragmentKind.RUNTIME_INSTRUCTION,
            source="runtime-policy/v1",
            content="这里应由受信部署配置提供运行时基础指令",
        ),
    ),
)
runtime = AgentRuntime(session_store, provider, context=context)
```

窗口值必须来自已核对的模型能力资料或受控平台配置。`utf8-bytes/v1`按UTF-8字节保守估算，不是Provider账单Token；生产仪表盘应比较Context估算、真实input usage和`context_budget_exceeded`比例，再决定是否引入新版本Tokenizer，不得原地修改v1算法。

当前`ContextEngine`只接受宿主显式提供的静态Fragment。不得直接遍历不受信仓库并把文件内容标成`runtime_instruction`；项目说明文件发现、路径作用域、读取缺口、revision和更新语义属于0.6.2。Fragment正文会发送给Provider，但不会复制进`ContextPrepared`检查记录；`source`会进入Session诊断，禁止写入凭据、Token或用户隐私值。

部署后至少验证：两个Provider离线system映射、超预算发网前失败、Event Replay、旧Session迁移、`inspect_context`以及Context指标无正文。0.6.1不需要真实模型API、远程服务器、数据库服务或新增中间件。
