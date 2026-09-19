---
doc_type: deployment-design
status: current
version: 9
code_revision: e5b7a8a4072dcb0ed4992ea94e2e0a8420f24a58
owners:
  - core
modules:
  - deployment
  - product_ui
  - product_config
  - app_server
  - sdk
  - trusted_actions
related_adrs:
  - docs/adr/0062-local-first-v1-commercial-boundary.md
  - docs/adr/0063-windows-v1-platform-support.md
  - docs/adr/0071-headless-app-server-and-sdk-lifecycle.md
  - docs/adr/0079-preflight-and-native-read-port.md
  - docs/adr/0080-capability-proven-product-action-composition.md
  - docs/adr/0081-single-coding-agent-product-boundary.md
related_tests:
  - tests/governance/test_product_runtime_convergence.py
  - tests/product_config/test_server_and_cli.py
  - tests/product_config/test_action_config_runtime.py
  - tests/product_config/test_action_runtime.py
  - tests/product_config/test_preflight.py
  - tests/product_ui/test_cli.py
  - tests/product_ui/test_stdio_product.py
  - tests/tools/test_windows_native_runtime.py
supersedes: []
---

# Harnessix Code部署与运维

## 1. 文档定位

本文是当前安装、配置、启动、升级、恢复、诊断和平台资料的统一入口。Harnessix Code 1.0只有一套本地优先
Coding Agent产品拓扑；历史`harnessix serve`、`harnessix worker`和Action HTTP API不再是产品部署方式。
0.1～0.8时期的旧命令和队列运维证据冻结在[部署里程碑历史](deployment-milestone-history.md)，不得作为当前手册。

具体操作分别由以下资料维护：

| 主题 | 当前事实源 |
|---|---|
| 安装与制品 | [安装与制品](operations/installation.md) |
| Product Config、Profile与Secret | [配置参考](operations/configuration.md) |
| 升级、备份与回退 | [升级与回退](operations/upgrade-and-rollback.md) |
| Session、Action、Process与Delivery恢复 | [故障恢复](operations/recovery.md) |
| Doctor、日志、Trace与Metric | [诊断与可观测性](operations/diagnostics.md) |
| macOS、Linux、Windows与Container | [平台与运行环境](operations/platforms.md) |

## 2. 当前能力与非目标

| 能力 | 当前状态 | 生产解释 |
|---|---|---|
| 源码开发安装 | 可用 | Python 3.12+，使用锁定`uv.lock`安装 |
| `harnessix code` | 已实现候选 | TUI、配置向导、Doctor、Client State和stdio子进程监督 |
| `harnessix agent` | 已实现 | Agent Protocol薄CLI，适合自动化和无TUI使用 |
| `harnessix agent-server` | 已实现 | 本地Headless App Server；stdout仅传输Agent Protocol JSONL |
| Agent Python SDK | 已实现 | `AgentClient`及进程内/子进程Transport，不包含Action HTTP Client |
| 默认Workspace读取 | macOS/Linux/Windows已实现 | 启动前按平台能力证明，失败时不开放协议 |
| 默认Workspace Patch | POSIX已实现 | 经Trusted Action、Review Artifact、审批和Delivery事务执行 |
| 固定Container Process | e4执行链与e5配置/恢复均已验收 | 只有显式Action Config且镜像、Sandbox、Owner、Secret和恢复能力全部证明后才广告 |
| Wheel与三平台安装器 | 未完成 | 0.9.5形成正式发行物、签名、SBOM与升级证据 |
| 远程多租户服务 | 非1.0范围 | 不开放网络Agent Server、远程Worker池或集中控制面 |

独立Action HTTP/Worker已经退出产品面。旧`ActionService/ActionWorker`只在迁移调用方内保留，不接受新部署；
迁移顺序和归档要求见[ADR 0081](adr/0081-single-coding-agent-product-boundary.md)。

## 3. 当前部署拓扑

```mermaid
flowchart LR
    User[用户] --> UI[harnessix code 或 agent]
    UI <-->|Agent Protocol v1<br/>stdio JSONL| Server[harnessix agent-server]
    Server --> Config[Product Config + Action Config]
    Server --> State[(私有状态目录)]
    Server --> Workspace[(用户Workspace)]
    Server --> Provider[外部模型Provider]
    Server --> Router[Trusted Action Runtime]
    Router --> Workspace
    Router --> Container[受管Container / Process Owner]
    Router --> External[显式批准的外部目标]
```

### 3.1 进程与生命周期

- `harnessix code`持有TUI、客户端状态和子进程Transport；
- `harnessix agent-server`持有双配置Store、Session Store、Artifact Store、Coding Tool、Agent Runtime和
  `ProductActionRuntimeOwner`；
- stdio EOF、协议错误或父进程退出触发Server关闭，组件按组合根逆序释放；
- Container进程由Process Owner负责启动、输出、超时和进程树清理，它是执行后端，不是第二个产品服务；
- Provider是外部网络边界，Workspace和状态目录是两个必须互不包含的本地信任域。

### 3.2 单一入口约束

产品用户和上层应用不得直接访问Session数据库、Action Audit或Executor。所有命令经Agent Protocol进入Application
Service，再由Agent Runtime和Trusted Action Gateway执行。不存在用户到Action HTTP API的旁路，也不存在从数据库队列
直接构造高风险调用的运维入口。

## 4. 安装与开发验收

```bash
uv sync --locked --all-extras --dev
uv run python scripts/generate_specs.py --check
uv run python scripts/documentation_check.py
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
```

`Dockerfile`仅用于构造包含`harnessix`命令的开发镜像，默认执行`--help`，不监听端口、不声明Action API、
不构成正式发行物。1.0容器或安装器必须在0.9.5重新设计并通过平台门禁。

## 5. 产品配置与启动

### 5.1 生成不含Secret正文的配置

```bash
uv run harnessix code configure \
  --config ./private/product-config.json \
  --provider-kind openai_chat \
  --base-url https://example.invalid/v1 \
  --model example-model \
  --api-key-env MODEL_API_KEY \
  --non-interactive
```

配置只保存Secret环境变量名称与版本。API Key正文必须由启动环境提供，不得写入配置、Workspace、Session或日志。

### 5.2 离线Doctor

```bash
uv run harnessix code doctor ./workspace \
  --config ./private/product-config.json \
  --action-config ./private/product-actions.json \
  --state-directory ./private/state
```

Doctor检查配置、Profile、Secret引用、Workspace、状态目录、平台原生读取端口、可选Git、TUI和Action能力。
Doctor成功不是网络Provider调用证明；真实Provider验证由受控Smoke负责。

### 5.3 启动TUI

```bash
export MODEL_API_KEY='由安全环境注入'
uv run harnessix code ./workspace \
  --config ./private/product-config.json \
  --action-config ./private/product-actions.json \
  --state-directory ./private/state
```

### 5.4 Headless Server

```bash
uv run harnessix agent-server \
  --config ./private/product-config.json \
  --action-config ./private/product-actions.json \
  --workspace ./workspace \
  --state-directory ./private/state
```

`agent-server`的stdin/stdout是协议通道。普通日志、Shell提示、进度条或异常Traceback不得写入stdout；调用方负责
子进程监督、连接代际和客户端实例身份。

## 6. 私有状态布局

状态目录必须由当前用户独占，不能是符号链接或Junction，不能与Workspace互相包含。当前或逐步接入的状态包括：

| 状态 | Owner | 事实用途 |
|---|---|---|
| `product-config.db` | Product Runtime Config Store | Product/Action配置快照、两条审计链、恢复报告和双活动指针原子CAS |
| `sessions.db` | Session/Protocol/Artifact Store | Thread、Turn、Item、请求幂等、Artifact元数据与内容 |
| `execution-plans.db` | Execution Plan Store | 不可变计划与批准检查点 |
| `action-audit.db` | Trusted Action Audit Store | Route投影和摘要化Hash链事件 |
| `workspace-leases.db` | Workspace Lease Store | Workspace执行所有权和Fencing |
| `workspace-transactions/` | Delivery Store | 多文件计划、Blob、游标和效果恢复 |
| Process状态文件 | Process Supervisor/Owner | Process Lease、输出观察、停止原因和恢复证据 |

旧Action Plane的SQLite/PostgreSQL Journal不属于新状态布局。已有旧数据库由原版本或后续归档工具只读处理，
新产品不会自动导入、执行或删除其中的READY/RUNNING记录。

## 7. 启动顺序与失败关闭

```text
resolve product/action config, workspace and state paths
reject overlap, links, invalid ownership or permissions
run shared offline preflight
strictly reload Product/Action Config and compare preflight digests
select profile and resolve Secret references without persisting material
build and enter Provider bundle
initialize Session, Protocol Request and Artifact stores
load previous active Action snapshot
build exact recovery router and reconcile unknown routes once; never execute
build candidate catalog from currently verified executors
enter Agent Runtime
atomically CAS activate Product + Action snapshots
open stdio protocol
on any failure: close entered components in reverse order; never open partial protocol
```

启动过程中不得为了保持工具目录稳定而降级到字符串Path、不受管Shell、Host Process或未固定镜像。能力探测失败应形成
稳定Doctor省略原因，并从模型目录和Router注册表同时移除。

## 8. 安全硬约束

1. 配置文件、状态目录和Workspace身份在启动期间必须稳定；
2. Secret正文只在Provider或Executor最小使用窗口存在，不进入argv、数据库、日志或Artifact；
3. 只读工具使用原生安全文件端口并复核漂移；写操作必须经Trusted Action和完整Review；
4. Process只接受宿主固定Profile，不接受模型提供程序、Shell、任意环境或Secret；
5. Container不可用时高风险Process不广告，不回退Host执行；
6. Agent Protocol是本地边界，不等于公网认证协议；
7. 不确定外部效果进入`unknown`并只对账，不自动重放；
8. 备份或恢复不得复制活动锁后让两个Runtime同时操作同一Workspace。
9. 旧Action Route必须用上一活动配置的精确Binding恢复；候选配置不得继承不相等的旧批准。

## 9. 升级、备份与回滚

升级前至少备份Product Config、Session数据库、Artifact内容、Execution Plan、Action Audit、Delivery、Workspace Lease和
Process状态。升级先在副本运行Schema/Doctor检查，再停止旧Server并启动新版本；不得在两个版本之间共享可写状态目录。

回滚只能由能够读取当前数据版本的旧版本执行。新事件或迁移已写入后，旧Reader若不认识必须失败关闭，不能跳过字段继续。
旧Action数据库不参与新产品启动；其归档是0.9.1f3的独立门禁。

## 10. 诊断与可观测性

当前产品没有HTTP `/healthz`或`/readyz`。就绪事实来自：

- `harnessix code doctor`的离线报告；
- Agent Protocol初始化握手及协商能力；
- Session、Action Audit、Delivery和Process的持久状态；
- 结构化错误码、Thread/Turn/Plan身份和低基数Telemetry。

诊断包不得包含Prompt全文、代码正文、Patch正文、argv、绝对路径、环境值、Secret、Provider响应正文或未脱敏stderr。
旧`harnessix.api.*`和`harnessix.worker.*`信号只属于迁移兼容测试，不能作为当前产品SLO。

## 11. 平台边界

- macOS/Linux使用POSIX安全文件语义；Workspace Patch只有在no-follow能力成立时广告；
- Windows只读链使用原生Handle，写入在抗Reparse事务端口完成前失败关闭；
- Container Process依赖Docker或Podman能力、固定镜像Digest、网络策略、资源限制和Process Owner；
- WSL2可作为强隔离后端候选，但不能替代Windows原生Workspace、Git、Process和CLI支持声明；
- PostgreSQL不再是1.0产品运行依赖，保留的旧Journal测试只用于迁移期行为证明。

## 12. 源码与测试映射

| 设计元素 | 源码 | 关键符号 | 测试 |
|---|---|---|---|
| 顶层产品命令 | [`cli.py`](../src/harnessix/cli.py) | `_parser`、`main` | [`test_cli.py`](../tests/smoke/test_cli.py) |
| TUI与子进程启动 | [`product_ui/cli.py`](../src/harnessix/product_ui/cli.py) | `code_main`、`_server_command` | [`product_ui测试`](../tests/product_ui/) |
| 产品组合根 | [`product_config/server.py`](../src/harnessix/product_config/server.py) | `run_product_stdio`、`_serve_product_stdio` | [`test_server_and_cli.py`](../tests/product_config/test_server_and_cli.py) |
| Action配置与双CAS | [`product_config/action_codec.py`](../src/harnessix/product_config/action_codec.py)、[`product_config/action_store.py`](../src/harnessix/product_config/action_store.py) | `load_product_action_config`、`SQLiteProductRuntimeConfigStore` | [`test_action_config_runtime.py`](../tests/product_config/test_action_config_runtime.py) |
| Agent协议服务 | [`app_server/stdio.py`](../src/harnessix/app_server/stdio.py) | `run_stdio` | [`test_server_sdk.py`](../tests/app_server/test_server_sdk.py) |
| Agent SDK Transport | [`sdk/agent_client.py`](../src/harnessix/sdk/agent_client.py) | `SubprocessAgentTransport` | [`app_server测试`](../tests/app_server/) |
| Trusted Action产品组合 | [`product_config/action_runtime.py`](../src/harnessix/product_config/action_runtime.py) | `ProductActionRuntimeOwner`、`open_default_product_action_runtime` | [`test_action_runtime.py`](../tests/product_config/test_action_runtime.py)、[真实Profile测试](../tests/integration/test_product_process_profile.py) |
| 单一产品面门禁 | 生产源码树 | 旧内核Import集合 | [`test_product_runtime_convergence.py`](../tests/governance/test_product_runtime_convergence.py) |

## 13. 当前限制与后续工作

- 0.9.1e4固定Container Process产品链与e5外部Action Config、Doctor、双配置CAS及统一启动恢复Owner已分别通过七任务CI；
- 0.9.1f旧Process、Git Push和Eval调用方尚未全部迁移，兼容内核仍存在源码与测试；
- 0.9.3尚未完成长会话Soak、容量和故障降级基线；
- 0.9.4尚未完成完整供应链、安全攻击和远端MCP边界；
- 0.9.5尚未形成签名发行物、升级/卸载和Beta证据；
- 1.0不提供网络Agent Server、远程Worker池、多租户身份、计费或服务SLO。
