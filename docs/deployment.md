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

## 当前Session v9 / migration10进程投影升级（0.5.4b2b2）

本片新增Agent Event/Thread v9和`0010_agent_process_projection.sql`。migration10只推进最低reader标记，不新增表、索引或列，也不重写旧事件、快照或Effect Journal。新写或显式rebuild的Session投影版本为9；v1–v8 Schema文件保持原字节。升级前仍应停止Session宿主并做一致备份，回退只能恢复备份，不能删除migration10伪装降级。

Runtime重开v9的WAITING_ACTION仍只保留原等待，不会在启动时创建、批准、执行或轮询Process Action。b2c1配置原专用端口后，调用方可显式`resume_turn`单次读取匹配Action并投影；普通Session审批或手写`ToolResult.process`仍不可绕过。Effect Journal、Worker、API的Process ToolDescriptor和Principal必须继续一致，Action Approval仍是唯一许可。

真实跨安装验收使用`scripts/process_session_upgrade_probe.py`，探针不依赖测试包：

1. 从`git archive e0e849813942b21452ba1943d5cca3a5f936e5f6`构建真实v8 wheel，并与当前wheel分别安装到仓库外基础环境；
2. 旧环境执行`python -I process_session_upgrade_probe.py create <空目录>`，创建包含真实只读工具调用的v8完成会话、migration1–9及冻结fixture；
3. 新环境执行`upgrade <同目录>`，确认migration10只追加marker，旧事件、投影、前九个校验和及数据库inode不变；
4. 旧环境执行`old-reader <同目录>`，确认`schema_too_new`且整个可见数据库状态不变；
5. 新环境执行`resume <同目录>`，追加v9完成Turn，确认旧v8事件原字节、Replay和projection version 9；旧环境再次拒绝。

实际旧wheel SHA256为`d0d5ba4322ddaa846565478901932335a5a89f3d26da3804df0155c022601d93`，b2b2a基础wheel为`7a8d189119d978240cd10b5efab7ecb3a13d453a08609fa16eb56a1c753fae04`，本片最终基础wheel为`e7a85fc4af22bea55ebd2d4db963890a774fbfbf3b0526d42899a4e86ef6dd84`。旧wheel直接导出的`tests/agent/fixtures/session-v8.json`纳入回归，SHA256为`f8c5413a0d0af920b6c1fcd4e7e286fb14b000045a5832b29663c26c11f02cc3`。migration10提交前后另以真实`os._exit`验证，重启只看到完整v8或完整v9 migration集合，不重写历史。

b2b2已完成同版本Replay、重启保留等待、冻结Schema、真实旧wheel升级和迁移硬退出验收。b2c1现已提供显式模型进程端口；默认Agent仍不暴露`host.process`，Process Artifact仍未部署。基础wheel无需供应商SDK、远程数据库或新中间件。


## 显式Process Agent运行时部署（0.5.4b2c1）

API/Agent宿主必须显式构造`ProcessAgentBridge(actions, principal)`并以`processes=`注入`AgentRuntime`。传入的ActionService必须设置`auto_execute=False`，且Registry中唯一的`host.process`定义、Principal、cwd/程序表/环境/资源绑定必须与独立Worker完全一致。桥接不拥有ActionService生命周期；宿主先初始化Effect Journal，关闭时在Agent Runtime退出后再关闭ActionService。

推荐部署角色保持分离：

1. Agent/API进程写Session、提交Action和写唯一审批决定；
2. Action Worker从Journal领取READY并执行固定程序；
3. 客户端或上层调度器在收到状态变化后显式调用`resume_turn`一次。b2c1不提供后台轮询器，不能用紧循环调用resume替代队列通知。

审批接口返回WAITING_ACTION不表示命令完成。只有Action终态被再次读取并写入Session结果后，Agent才继续模型循环；UNKNOWN/MANUAL_INTERVENTION会中断Turn。公开模型结果当前只有流计数/摘要和生命周期，不含完整stdout/stderr。运维查看完整正文仍需读取受控Action Result；b2c2交付前不要自行把Base64正文注入模型或伪造Artifact引用。

同一决定重答可修复Action已决定、Session未投影的窗口；不同actor/outcome/reason会冲突。重启后的WAITING_APPROVAL可由`resume_turn`只读同步已有Action决定，WAITING_ACTION可单次观察。若Action由外部入口在Turn超时后形成决定，Session仍按Action真实决定时间补投影，但原Turn预算不会复活。Action创建后而Session审批请求尚未提交的真硬退出、等待取消和完整跨库退出矩阵尚未验收，不应配置自动重试或修改Journal状态绕过。

基础wheel可在仓库外执行`python -I kernel_process.py`（复制自`examples/kernel_process.py`）验证离线闭环。该示例不需要供应商SDK、API Key、远程数据库或新中间件；不是任意Shell、仓库测试执行或OS Sandbox。包版本仍为0.1.0，生产记录必须使用具体提交和wheel摘要。
