---
doc_type: governance
status: current
version: 4
code_revision: 8321ef383f2cbb3ab76191a1cc3db361a52e92ef
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
| 总体架构 | [总体架构](../architecture.md)、[源码阅读地图](../guides/source-reading-map.md) | [研究计划](../research-plan.md)及各主题研究 | 系统级入口和两份黄金样例已完成；剩余模块级事实待DOC-1.3～DOC-1.4迁移 |
| Action Plane | [Action Plane子系统设计](../subsystems/action-plane.md)、[Action Contract](../action-contract.md)、[Action生命周期](../action-lifecycle.md) | [ADR 0001](../adr/0001-python-first-runtime.md)～[ADR 0004](../adr/0004-durable-trace-context.md) | 子系统级现行设计已完成；各包独立设计待DOC-1.3细化 |
| Agent Runtime | [Agent Runtime模块设计](../modules/agent.md) | [Agent Loop研究](../research/agent-loop.md)、[ADR 0006](../adr/0006-thread-turn-item-event-model.md)～[ADR 0013](../adr/0013-kernel-contracts-and-telemetry.md) | 现行模块设计已完成 |
| Model Runtime | [0.4设计](../m04-model-runtime.md)、[Smoke指南](../model-smoke.md) | [ADR 0014](../adr/0014-openai-compatible-provider.md)～[ADR 0022](../adr/0022-bailian-price-validation.md) | Provider、账本、计费和Smoke缺独立模块入口 |
| Coding Tool | [0.5设计](../m05-coding-tools.md) | Tool/Patch/Eval系列研究与ADR | 1,097行聚合文档承担过多模块事实 |
| Context/Session | [0.6设计](../m06-context-and-sessions.md)、Compaction与Thread专题设计 | Context/Session系列研究与ADR | Context、Session、Artifact边界仍需逐包固化 |
| 可信执行/交付 | [0.7设计](../m07-trusted-execution-and-delivery.md)、[威胁模型](../threat-model.md) | [可信执行研究](../research/trusted-execution-and-delivery.md)、ADR 0065～0069 | Process、Sandbox、Workspace、Delivery等缺独立设计 |
| 产品运行时/扩展 | [0.8设计](../m08-product-runtime-and-extensions.md) | Protocol/MCP/Skill研究、ADR 0070～0075 | Protocol、App Server、SDK及扩展需逐包固化 |
| 可维护性 | [0.9.0设计](../m09-code-maintainability.md) | [可读性研究](../research/code-readability-and-structure.md)、[ADR 0076](../adr/0076-code-readability-and-structural-governance.md) | 文档门禁留待DOC-1.6 |
| 测试与Eval | [测试与Eval规范](../testing-and-evals.md) | Eval系列研究与ADR | 1,670行聚合资料需按规范、任务集、执行和证据拆分 |
| 部署与运维 | [部署与运行](../deployment.md) | 平台、许可和产品边界ADR | 1,238行聚合资料需拆分安装、配置、升级、恢复和诊断 |

## 4. 30个生产源码包覆盖矩阵

“当前相关资料”表示可用于迁移或已经完成的现行入口；尚未创建的“目标模块设计”保持代码文本，已创建路径使用链接。

| 源码包 | 核心职责 | 当前相关资料 | 主要测试入口 | 目标模块设计 | 当前结论 |
|---|---|---|---|---|---|
| [adapters](../../src/harnessix/adapters/) | 外部框架适配 | [Action Contract](../action-contract.md)、[总体架构](../architecture.md) | [unit](../../tests/unit/) | `docs/modules/adapters.md` | 缺失 |
| [agent](../../src/harnessix/agent/) | Agent Loop、Reducer、Turn运行 | [Agent Runtime模块设计](../modules/agent.md) | [agent](../../tests/agent/) | [docs/modules/agent.md](../modules/agent.md) | 完整，DOC-1.2黄金样例 |
| [api](../../src/harnessix/api/) | Action HTTP API | [Action Contract](../action-contract.md)、[部署](../deployment.md) | [integration](../../tests/integration/) | `docs/modules/api.md` | 缺失 |
| [app_server](../../src/harnessix/app_server/) | Headless App Server生命周期 | [0.8](../m08-product-runtime-and-extensions.md) | [app_server](../../tests/app_server/) | `docs/modules/app-server.md` | 缺失 |
| [artifacts](../../src/harnessix/artifacts/) | 大对象、Diff与模型历史Artifact | [0.5](../m05-coding-tools.md)、[0.6](../m06-context-and-sessions.md) | [artifacts](../../tests/artifacts/) | `docs/modules/artifacts.md` | 缺失 |
| [context](../../src/harnessix/context/) | Context Source、预算、压缩 | [0.6](../m06-context-and-sessions.md)、[Compaction运行时](../compaction-runtime-and-windows.md) | [context](../../tests/context/) | `docs/modules/context.md` | 缺失 |
| [delivery](../../src/harnessix/delivery/) | 事务性交付与Git发布 | [0.7](../m07-trusted-execution-and-delivery.md) | [delivery](../../tests/delivery/) | `docs/modules/delivery.md` | 缺失 |
| [domain](../../src/harnessix/domain/) | Action领域契约 | [Action Plane子系统设计](../subsystems/action-plane.md)、[Action Contract](../action-contract.md) | [unit](../../tests/unit/) | `docs/modules/domain.md` | 子系统级完整；独立模块待DOC-1.3 |
| [evals](../../src/harnessix/evals/) | Coding Eval合同、执行与分级 | [测试与Eval](../testing-and-evals.md)、[0.5](../m05-coding-tools.md) | [evals](../../tests/evals/) | `docs/modules/evals.md` | 缺失 |
| [execution](../../src/harnessix/execution/) | Execution Plan与持久计划 | [0.7](../m07-trusted-execution-and-delivery.md) | [execution](../../tests/execution/) | `docs/modules/execution.md` | 缺失 |
| [executors](../../src/harnessix/executors/) | Action Executor实现 | [Action Plane子系统设计](../subsystems/action-plane.md) | [unit](../../tests/unit/) | `docs/modules/executors.md` | 子系统级完整；独立模块待DOC-1.3 |
| [hooks](../../src/harnessix/hooks/) | 声明式Hook注册与执行 | [0.8](../m08-product-runtime-and-extensions.md) | [hooks](../../tests/hooks/) | `docs/modules/hooks.md` | 缺失 |
| [mcp](../../src/harnessix/mcp/) | MCP目录、客户端、Server与统一Action | [0.8](../m08-product-runtime-and-extensions.md)、[MCP研究](../research/mcp-runtime-and-security.md) | [mcp](../../tests/mcp/) | `docs/modules/mcp.md` | 缺失 |
| [models](../../src/harnessix/models/) | Provider适配、用量与成本 | [0.4](../m04-model-runtime.md)、[Smoke](../model-smoke.md) | [models](../../tests/models/) | `docs/modules/models.md` | 缺失 |
| [observability](../../src/harnessix/observability/) | Trace、Metric与结构化日志 | [M1可观测性](../m1-observability.md) | [integration](../../tests/integration/)、[unit](../../tests/unit/) | `docs/modules/observability.md` | 缺失 |
| [patches](../../src/harnessix/patches/) | Patch计划、批次、应用与恢复 | [0.5](../m05-coding-tools.md)、[Patch研究](../research/patch-runtime.md) | [patches](../../tests/patches/) | `docs/modules/patches.md` | 缺失 |
| [policy](../../src/harnessix/policy/) | Action Policy决策 | [Action Plane子系统设计](../subsystems/action-plane.md) | [unit](../../tests/unit/) | `docs/modules/policy.md` | 子系统级完整；独立模块待DOC-1.3 |
| [processes](../../src/harnessix/processes/) | 宿主/容器进程生命周期 | [0.5](../m05-coding-tools.md)、[0.7](../m07-trusted-execution-and-delivery.md) | [processes](../../tests/processes/) | `docs/modules/processes.md` | 缺失 |
| [product_config](../../src/harnessix/product_config/) | 产品配置、迁移与活动Profile | [0.8](../m08-product-runtime-and-extensions.md)、[部署](../deployment.md) | [product_config](../../tests/product_config/) | `docs/modules/product-config.md` | 缺失 |
| [protocol](../../src/harnessix/protocol/) | Agent Protocol Schema、编解码和投影 | [0.8](../m08-product-runtime-and-extensions.md)、[Protocol研究](../research/protocol.md) | [protocol](../../tests/protocol/) | `docs/modules/protocol.md` | 缺失 |
| [sandbox](../../src/harnessix/sandbox/) | 隔离、网络和能力探测 | [0.7](../m07-trusted-execution-and-delivery.md)、[威胁模型](../threat-model.md) | [sandbox](../../tests/sandbox/) | `docs/modules/sandbox.md` | 缺失 |
| [sdk](../../src/harnessix/sdk/) | 进程内/子进程Python SDK | [0.8](../m08-product-runtime-and-extensions.md) | [app_server](../../tests/app_server/)、[unit](../../tests/unit/) | `docs/modules/sdk.md` | 缺失 |
| [secrets](../../src/harnessix/secrets/) | Secret引用、解析、守卫和脱敏 | [0.7](../m07-trusted-execution-and-delivery.md)、[威胁模型](../threat-model.md) | [secrets](../../tests/secrets/) | `docs/modules/secrets.md` | 缺失 |
| [session](../../src/harnessix/session/) | Session Store、迁移与恢复 | [Session模块设计](../modules/session.md) | [agent](../../tests/agent/)、[contracts](../../tests/contracts/) | [docs/modules/session.md](../modules/session.md) | 完整，DOC-1.3 Wave A |
| [skills](../../src/harnessix/skills/) | Skill快照、发现和渐进加载 | [0.8](../m08-product-runtime-and-extensions.md) | [skills](../../tests/skills/) | `docs/modules/skills.md` | 缺失 |
| [smoke](../../src/harnessix/smoke/) | 受控真实Provider Smoke | [0.4](../m04-model-runtime.md)、[Smoke指南](../model-smoke.md) | [smoke](../../tests/smoke/) | `docs/modules/smoke.md` | 缺失 |
| [storage](../../src/harnessix/storage/) | SQLite/PostgreSQL Action Journal | [Action Plane子系统设计](../subsystems/action-plane.md) | [integration](../../tests/integration/) | `docs/modules/storage.md` | 子系统级完整；独立模块待DOC-1.3 |
| [tools](../../src/harnessix/tools/) | 只读、Git和受控工具运行 | [0.5](../m05-coding-tools.md) | [tools](../../tests/tools/) | `docs/modules/tools.md` | 缺失 |
| [trusted_actions](../../src/harnessix/trusted_actions/) | 高风险Coding Action统一路由 | [0.7](../m07-trusted-execution-and-delivery.md) | [trusted_actions](../../tests/trusted_actions/) | `docs/modules/trusted-actions.md` | 缺失 |
| [workspace](../../src/harnessix/workspace/) | 路径、Snapshot和租约 | [0.5](../m05-coding-tools.md)、[0.7](../m07-trusted-execution-and-delivery.md) | [workspace](../../tests/workspace/) | `docs/modules/workspace.md` | 缺失 |

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

当前已完成2/30个独立包级现行模块设计，并完成1份覆盖Action Plane多个包和根级模块的现行
子系统设计；子系统覆盖不替代DOC-1.3要求的独立包设计。整改阶段和责任分组见
[文档整改待办](documentation-remediation-backlog.md)。
