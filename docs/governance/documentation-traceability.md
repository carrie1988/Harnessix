---
doc_type: governance
status: current
version: 24
code_revision: 8cd3358bdf0e8f550d7584ee3d81b5e5f7ae4e3e
owners:
  - core
modules:
  - documentation
related_adrs: []
related_tests: []
supersedes: []
---

# Harnessix Code 文档—源码—测试追踪矩阵

## 1. 目标

追踪矩阵定义正式资料如何从产品目标落到实际实现和验证，并盘点每个生产源码包当前可参考的文档、测试入口和缺失的现行模块设计。矩阵是导航和整改输入，不表示现有聚合文档已经完整覆盖对应模块。

## 2. 目标追踪模型

```mermaid
flowchart LR
    Charter[产品章程] --> Roadmap[路线图/需求切片]
    Roadmap --> Research[源码研究]
    Research --> ADR[架构决策]
    ADR --> Change[重大变更设计]
    Change --> Contract[领域/协议契约]
    Contract --> Module[现行模块设计]
    Module --> Source[源码文件与符号]
    Source --> Test[测试用例/合同]
    Test --> Evidence[验证证据]
    Evidence --> Roadmap
```

### 2.1 图示说明

1. [产品章程](../product-charter.md)定义用户、价值和产品边界；[路线图](../roadmap.md)将目标拆成可发布切片；
2. [源码研究](../research-plan.md)只提供参考事实，不能直接成为Harnessix契约；
3. ADR记录长期取舍，重大变更设计描述一次增量，契约定义稳定边界；
4. 模块设计维护当前实现，必须链接到实际源码文件和稳定符号；
5. 测试证明契约和失败语义，验证证据记录特定环境与结果；
6. 只有证据满足路线图门槛，切片状态才能关闭。

### 2.2 追踪关系要求

| 起点 | 必须追踪到 | 关系要求 |
|---|---|---|
| 路线图切片 | 研究、ADR、变更设计、测试/证据 | 完成状态不得只有提交号 |
| ADR | 背景研究、现行模块设计 | ADR不重复当前实现全文 |
| 重大变更设计 | 受影响模块、源码符号、测试 | 实现后冻结为历史 |
| 现行模块设计 | 上下游模块、源码符号、测试 | 代码变更后同步 |
| 契约 | 实现者、消费者、合同测试 | 版本、错误和兼容规则完整 |
| 验证证据 | 测试规范、代码提交、环境 | 不含Secret和个人环境信息 |

## 3. 当前系统级资料关系

| 关注点 | 当前事实入口 | 决策/研究 | 当前缺口 |
|---|---|---|---|
| 产品边界 | [文档中心](../README.md)、[产品章程](../product-charter.md)、[路线图](../roadmap.md) | [ADR 0005](../adr/0005-evolve-to-harnessix-code.md)、[ADR 0062](../adr/0062-local-first-v1-commercial-boundary.md) | 能力证据仍需在DOC-1.5按发布声明统一索引 |
| 总体架构 | [总体架构](../architecture.md)、[源码阅读地图](../guides/source-reading-map.md) | [研究计划](../research-plan.md)及各主题研究 | 系统级入口、黄金样例和DOC-1.3的19个模块已完成；DOC-1.4已完成Protocol与App Server，剩余8个包 |
| Action Plane | [Domain模块设计](../modules/domain.md)、[Policy模块设计](../modules/policy.md)、[Executors模块设计](../modules/executors.md)、[Storage模块设计](../modules/storage.md)、[Action Plane子系统设计](../subsystems/action-plane.md)、[Action Contract](../action-contract.md)、[Action生命周期](../action-lifecycle.md) | [ADR 0001](../adr/0001-python-first-runtime.md)～[ADR 0004](../adr/0004-durable-trace-context.md) | Domain、Policy、Executors、Storage独立设计和子系统主链已完成 |
| Agent Runtime | [Agent Runtime模块设计](../modules/agent.md) | [Agent Loop研究](../research/agent-loop.md)、[ADR 0006](../adr/0006-thread-turn-item-event-model.md)～[ADR 0013](../adr/0013-kernel-contracts-and-telemetry.md) | 现行模块设计已完成 |
| Model Runtime | [Model Runtime模块设计](../modules/models.md)、[Smoke指南](../model-smoke.md) | [ADR 0014](../adr/0014-openai-compatible-provider.md)～[ADR 0022](../adr/0022-bailian-price-validation.md) | Provider、账本和计费现行设计已完成；Smoke独立模块入口待DOC-1.4 |
| Coding Tool | [Coding Tool Runtime模块设计](../modules/tools.md)、[Managed Patch Runtime模块设计](../modules/patches.md)、[Process Runtime模块设计](../modules/processes.md) | [Tool Runtime研究](../research/tool-runtime.md)、[Patch Runtime研究](../research/patch-runtime.md)、[ADR 0023](../adr/0023-workspace-read-tools.md)～[ADR 0053](../adr/0053-tool-concurrency-and-error-taxonomy.md) | Tools、Patch与Process现行模块设计已完成；Eval继续独立迁移 |
| Context/Session | [Context模块设计](../modules/context.md)、[Session模块设计](../modules/session.md)、[Artifact模块设计](../modules/artifacts.md) | [0.6设计](../m06-context-and-sessions.md)、Compaction与Thread专题设计及相关ADR | 三个包的现行设计已完成；里程碑和专题历史资料待DOC-1.5分层 |
| 可信执行/交付 | [Execution Plan模块设计](../modules/execution.md)、[Process Runtime模块设计](../modules/processes.md)、[Sandbox模块设计](../modules/sandbox.md)、[Secrets模块设计](../modules/secrets.md)、[Trusted Actions模块设计](../modules/trusted-actions.md)、[Workspace模块设计](../modules/workspace.md)、[Delivery模块设计](../modules/delivery.md)、[0.7设计](../m07-trusted-execution-and-delivery.md)、[威胁模型](../threat-model.md) | [可信执行研究](../research/trusted-execution-and-delivery.md)、[统一Action研究](../research/unified-action-plane-and-extension-boundaries.md)、ADR 0065～0069 | Execution、Process、Sandbox、Secrets、Trusted Actions、Workspace与Delivery现行设计已完成 |
| 产品运行时/扩展 | [Protocol模块设计](../modules/protocol.md)、[App Server模块设计](../modules/app-server.md)、[0.8设计](../m08-product-runtime-and-extensions.md) | Protocol/MCP/Skill研究、ADR 0070～0075 | Protocol与App Server现行设计已完成；SDK及其余扩展仍需逐包固化 |
| 可观测性 | [Observability模块设计](../modules/observability.md) | [ADR 0004](../adr/0004-durable-trace-context.md)、[ADR 0013](../adr/0013-kernel-contracts-and-telemetry.md) | 现行模块事实已完成；统一产品装配、故障隔离、单位和隐私加固仍是产品任务 |
| 可维护性 | [0.9.0设计](../m09-code-maintainability.md) | [可读性研究](../research/code-readability-and-structure.md)、[ADR 0076](../adr/0076-code-readability-and-structural-governance.md) | 文档门禁留待DOC-1.6 |
| 测试与Eval | [Evals模块设计](../modules/evals.md)、[测试与Eval规范](../testing-and-evals.md) | Eval系列研究与ADR | Evals现行模块事实已完成；1,670行聚合历史仍需在DOC-1.5按规范、任务集、执行和证据分层 |
| 部署与运维 | [部署与运行](../deployment.md) | 平台、许可和产品边界ADR | 1,238行聚合资料需拆分安装、配置、升级、恢复和诊断 |

## 4. 30个生产源码包覆盖矩阵

“当前相关资料”表示可用于迁移或已经完成的现行入口；尚未创建的“目标模块设计”保持代码文本，已创建路径使用链接。

| 源码包 | 核心职责 | 当前相关资料 | 主要测试入口 | 目标模块设计 | 当前结论 |
|---|---|---|---|---|---|
| [adapters](../../src/harnessix/adapters/) | 外部框架适配 | [Action Contract](../action-contract.md)、[总体架构](../architecture.md) | [unit](../../tests/unit/) | `docs/modules/adapters.md` | 缺失 |
| [agent](../../src/harnessix/agent/) | Agent Loop、Reducer、Turn运行 | [Agent Runtime模块设计](../modules/agent.md) | [agent](../../tests/agent/) | [docs/modules/agent.md](../modules/agent.md) | 完整，DOC-1.2黄金样例 |
| [api](../../src/harnessix/api/) | Action HTTP API | [Action Contract](../action-contract.md)、[部署](../deployment.md) | [integration](../../tests/integration/) | `docs/modules/api.md` | 缺失 |
| [app_server](../../src/harnessix/app_server/) | Headless App Server生命周期 | [App Server模块设计](../modules/app-server.md)、[0.8](../m08-product-runtime-and-extensions.md) | [app_server](../../tests/app_server/) | [docs/modules/app-server.md](../modules/app-server.md) | 完整，DOC-1.4产品协议 |
| [artifacts](../../src/harnessix/artifacts/) | 大对象、Diff与模型历史Artifact | [Artifact模块设计](../modules/artifacts.md) | [artifacts](../../tests/artifacts/) | [docs/modules/artifacts.md](../modules/artifacts.md) | 完整，DOC-1.3 Wave A |
| [context](../../src/harnessix/context/) | Context Source、预算、压缩 | [Context模块设计](../modules/context.md) | [context](../../tests/context/) | [docs/modules/context.md](../modules/context.md) | 完整，DOC-1.3 Wave A |
| [delivery](../../src/harnessix/delivery/) | 事务性交付与Git发布 | [Delivery模块设计](../modules/delivery.md)、[0.7](../m07-trusted-execution-and-delivery.md) | [delivery](../../tests/delivery/)、[Push Schema](../../tests/trusted_actions/test_schemas.py) | [docs/modules/delivery.md](../modules/delivery.md) | 完整，DOC-1.3 Wave D |
| [domain](../../src/harnessix/domain/) | Action领域契约 | [Domain模块设计](../modules/domain.md) | [unit](../../tests/unit/)、[Action Service](../../tests/integration/test_action_service.py) | [docs/modules/domain.md](../modules/domain.md) | 完整，DOC-1.3 Wave C |
| [evals](../../src/harnessix/evals/) | Coding Eval合同、执行与分级 | [Evals模块设计](../modules/evals.md)、[测试与Eval](../testing-and-evals.md)、[0.5](../m05-coding-tools.md) | [evals](../../tests/evals/) | [docs/modules/evals.md](../modules/evals.md) | 完整，DOC-1.3 Wave D |
| [execution](../../src/harnessix/execution/) | Execution Plan与持久计划 | [Execution Plan模块设计](../modules/execution.md) | [execution](../../tests/execution/) | [docs/modules/execution.md](../modules/execution.md) | 完整，DOC-1.3 Wave B |
| [executors](../../src/harnessix/executors/) | Action Executor实现 | [Executors模块设计](../modules/executors.md) | [Action Service](../../tests/integration/test_action_service.py)、[Worker](../../tests/integration/test_worker.py) | [docs/modules/executors.md](../modules/executors.md) | 完整，DOC-1.3 Wave C |
| [hooks](../../src/harnessix/hooks/) | 声明式Hook注册与执行 | [0.8](../m08-product-runtime-and-extensions.md) | [hooks](../../tests/hooks/) | `docs/modules/hooks.md` | 缺失 |
| [mcp](../../src/harnessix/mcp/) | MCP目录、客户端、Server与统一Action | [0.8](../m08-product-runtime-and-extensions.md)、[MCP研究](../research/mcp-runtime-and-security.md) | [mcp](../../tests/mcp/) | `docs/modules/mcp.md` | 缺失 |
| [models](../../src/harnessix/models/) | Provider适配、用量与成本 | [Model Runtime模块设计](../modules/models.md) | [models](../../tests/models/) | [docs/modules/models.md](../modules/models.md) | 完整，DOC-1.3 Wave A |
| [observability](../../src/harnessix/observability/) | Trace、Metric与结构化日志 | [Observability模块设计](../modules/observability.md)、[M1可观测性](../m1-observability.md) | [Agent遥测](../../tests/agent/test_telemetry.py)、[观测集成](../../tests/integration/test_observability_flow.py)、[OTLP](../../tests/integration/test_otlp_export.py)、[unit](../../tests/unit/test_observability_core.py) | [docs/modules/observability.md](../modules/observability.md) | 完整，DOC-1.3 Wave D |
| [patches](../../src/harnessix/patches/) | Patch计划、批次、应用与恢复 | [Managed Patch Runtime模块设计](../modules/patches.md) | [patches](../../tests/patches/) | [docs/modules/patches.md](../modules/patches.md) | 完整，DOC-1.3 Wave B |
| [policy](../../src/harnessix/policy/) | Action Policy决策 | [Policy模块设计](../modules/policy.md) | [Action Service](../../tests/integration/test_action_service.py)、[Process](../../tests/processes/test_action_executor.py) | [docs/modules/policy.md](../modules/policy.md) | 完整，DOC-1.3 Wave C |
| [processes](../../src/harnessix/processes/) | 宿主/容器进程生命周期 | [Process Runtime模块设计](../modules/processes.md) | [processes](../../tests/processes/) | [docs/modules/processes.md](../modules/processes.md) | 完整，DOC-1.3 Wave B |
| [product_config](../../src/harnessix/product_config/) | 产品配置、迁移与活动Profile | [0.8](../m08-product-runtime-and-extensions.md)、[部署](../deployment.md) | [product_config](../../tests/product_config/) | `docs/modules/product-config.md` | 缺失 |
| [protocol](../../src/harnessix/protocol/) | Agent Protocol Schema、编解码和投影 | [Protocol模块设计](../modules/protocol.md)、[0.8](../m08-product-runtime-and-extensions.md)、[Protocol研究](../research/protocol.md) | [protocol](../../tests/protocol/)、[app_server](../../tests/app_server/) | [docs/modules/protocol.md](../modules/protocol.md) | 完整，DOC-1.4产品协议 |
| [sandbox](../../src/harnessix/sandbox/) | 隔离、网络和能力探测 | [Sandbox模块设计](../modules/sandbox.md)、[威胁模型](../threat-model.md) | [sandbox](../../tests/sandbox/)、[真实Container](../../tests/integration/test_container_sandbox.py) | [docs/modules/sandbox.md](../modules/sandbox.md) | 完整，DOC-1.3 Wave C |
| [sdk](../../src/harnessix/sdk/) | 进程内/子进程Python SDK | [0.8](../m08-product-runtime-and-extensions.md) | [app_server](../../tests/app_server/)、[unit](../../tests/unit/) | `docs/modules/sdk.md` | 缺失 |
| [secrets](../../src/harnessix/secrets/) | Secret引用、解析、守卫和脱敏 | [Secrets模块设计](../modules/secrets.md)、[威胁模型](../threat-model.md) | [secrets](../../tests/secrets/)、[Process](../../tests/processes/test_supervisor.py)、[Product Config](../../tests/product_config/test_provider_credentials.py) | [docs/modules/secrets.md](../modules/secrets.md) | 完整，DOC-1.3 Wave C |
| [session](../../src/harnessix/session/) | Session Store、迁移与恢复 | [Session模块设计](../modules/session.md) | [agent](../../tests/agent/)、[contracts](../../tests/contracts/) | [docs/modules/session.md](../modules/session.md) | 完整，DOC-1.3 Wave A |
| [skills](../../src/harnessix/skills/) | Skill快照、发现和渐进加载 | [0.8](../m08-product-runtime-and-extensions.md) | [skills](../../tests/skills/) | `docs/modules/skills.md` | 缺失 |
| [smoke](../../src/harnessix/smoke/) | 受控真实Provider Smoke | [0.4](../m04-model-runtime.md)、[Smoke指南](../model-smoke.md) | [smoke](../../tests/smoke/) | `docs/modules/smoke.md` | 缺失 |
| [storage](../../src/harnessix/storage/) | SQLite/PostgreSQL Action Journal | [Storage模块设计](../modules/storage.md)、[Action Plane子系统设计](../subsystems/action-plane.md) | [Action Service](../../tests/integration/test_action_service.py)、[Worker](../../tests/integration/test_worker.py)、[PostgreSQL](../../tests/integration/test_postgres_journal.py) | [docs/modules/storage.md](../modules/storage.md) | 完整，DOC-1.3 Wave C |
| [tools](../../src/harnessix/tools/) | 只读、Git和受控工具运行 | [Coding Tool Runtime模块设计](../modules/tools.md) | [tools](../../tests/tools/) | [docs/modules/tools.md](../modules/tools.md) | 完整，DOC-1.3 Wave B |
| [trusted_actions](../../src/harnessix/trusted_actions/) | 高风险Coding Action统一路由 | [Trusted Actions模块设计](../modules/trusted-actions.md)、[0.7](../m07-trusted-execution-and-delivery.md) | [trusted_actions](../../tests/trusted_actions/)、[Git Push](../../tests/delivery/test_git_push.py) | [docs/modules/trusted-actions.md](../modules/trusted-actions.md) | 完整，DOC-1.3 Wave C |
| [workspace](../../src/harnessix/workspace/) | 路径、Snapshot和租约 | [Workspace模块设计](../modules/workspace.md)、[0.7](../m07-trusted-execution-and-delivery.md) | [workspace](../../tests/workspace/)、[delivery](../../tests/delivery/) | [docs/modules/workspace.md](../modules/workspace.md) | 完整，DOC-1.3 Wave D |

## 5. 根级生产模块归属

根级模块不单独制造十份文档；它们应归属系统架构、产品入口或Action Plane设计，并在对应源码映射中覆盖。

| 源码 | 主要职责 | 目标事实源 |
|---|---|---|
| [__init__.py](../../src/harnessix/__init__.py) | 公共导出面 | 系统架构与公共API索引 |
| [__main__.py](../../src/harnessix/__main__.py) | `python -m harnessix`入口 | CLI/TUI模块设计 |
| [agent_cli.py](../../src/harnessix/agent_cli.py) | Coding Agent薄CLI | 0.9.1 CLI/TUI设计 |
| [bootstrap.py](../../src/harnessix/bootstrap.py) | 产品Runtime装配 | 系统架构与产品运行时设计 |
| [cli.py](../../src/harnessix/cli.py) | Action Plane CLI | [Action Plane子系统设计](../subsystems/action-plane.md) |
| [file_lock.py](../../src/harnessix/file_lock.py) | 跨平台文件锁 | 调用它的存储/配置模块设计 |
| [licensing.py](../../src/harnessix/licensing.py) | 许可信息 | 发布与许可设计 |
| [runtime.py](../../src/harnessix/runtime.py) | Action Runtime门面 | [Action Plane子系统设计](../subsystems/action-plane.md) |
| [settings.py](../../src/harnessix/settings.py) | 基础设置 | 部署与产品配置设计 |
| [worker.py](../../src/harnessix/worker.py) | Action Worker入口 | [Action Plane子系统设计](../subsystems/action-plane.md) |

## 6. 追踪完整度定义

每个现行模块设计只有同时满足以下条件，才可从“缺失”改为“完整”：

1. 元数据状态为`current`，代码版本可定位；
2. 上下游边界、正常时序、失败/恢复时序和适用的数据/状态图完整；
3. 重点类、接口、字段、不变量和核心伪代码完整；
4. 每个关键设计元素链接到实际源码文件和稳定符号；
5. 每项契约、失败语义和安全边界链接到测试文件及测试函数/合同；
6. 对应ADR、研究和历史变更关系清楚；
7. 相对链接与文档结构检查通过；
8. 未实现能力和已知限制明确，不把路线图目标写成当前事实。

当前已完成22/30个独立包级现行模块设计，并完成1份覆盖Action Plane多个包和根级模块的现行
子系统设计；DOC-1.4已完成Protocol与App Server，下一项为SDK。整改阶段和责任分组见
[文档整改待办](documentation-remediation-backlog.md)。
