---
doc_type: governance-index
status: current
version: 50
code_revision: 82e247a8d083f3f8a7d68ee091a43d59096f298d
owners:
  - core
modules:
  - documentation
  - product_ui
related_adrs:
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
  - docs/adr/0078-product-shell-and-recoverable-client-state.md
  - docs/adr/0079-preflight-and-native-read-port.md
  - docs/adr/0080-capability-proven-product-action-composition.md
related_tests:
  - tests/governance/test_documentation_policy.py
  - tests/product_config/test_action_contracts.py
  - tests/product_config/test_action_catalog.py
  - tests/trusted_actions/test_router.py
  - tests/governance/test_generated_specs.py
  - tests/product_ui/test_state_store.py
  - tests/product_ui/test_projection.py
  - tests/product_ui/test_recoverable_session.py
  - tests/product_ui/test_controller.py
  - tests/product_ui/test_app.py
  - tests/product_ui/test_interactions.py
  - tests/product_ui/test_app_interactions.py
  - tests/product_ui/test_stdio_product.py
supersedes: []
---

# Harnessix Code 文档中心

## 1. 文档用途

本页是Harnessix Code正式资料的统一入口。文档按“当前事实、历史决策、研究证据、验证证据”分层，避免读者通过里程碑历史拼接当前实现。

当前产品实现已经完成路线图0.1～0.9.0范围，但仍不是1.0正式商用版本。0.9.1a～0.9.1d已通过对应全矩阵CI并关闭。0.9.1e的[源码研究](research/default-trusted-action-product-composition.md)、[ADR 0080](adr/0080-capability-proven-product-action-composition.md)和[详细设计](changes/m09-1e-default-trusted-action-composition.md)已经建立；其中e1已完成Action合同、同源目录、Router幂等规划和默认Artifact组合，并由[CI 34739842959](https://github.com/carrie1988/Harnessix/actions/runs/34739842959)完成全矩阵验收，e2～e5尚未实施。三平台发行物、固定Eval/Soak阈值、安全供应链和Dogfooding仍属于0.9后续工作。当前能力和规划能力以[总体架构](architecture.md)及[路线图](roadmap.md)为准。

## 2. 推荐阅读路径

### 2.1 首次了解项目

1. [产品章程](product-charter.md)：目标用户、产品价值、1.0边界与非目标；
2. [总体架构](architecture.md)：当前组件、数据与控制流、持久化、安全及失败恢复；
3. [设计与开发路线图](roadmap.md)：已完成能力、未完成切片和发布门槛；
4. [源码阅读地图](guides/source-reading-map.md)：从CLI入口追踪到Agent、Model、Context、Tool和持久化；
5. [测试与Eval规范](testing-and-evals.md)：行为如何被确定性测试、故障注入和真实任务证明。

### 2.2 阅读当前源码

| 阅读目标 | 起点 | 下一步 |
|---|---|---|
| 产品启动与协议 | [源码阅读地图：产品启动](guides/source-reading-map.md#4-产品启动与装配主链) | `cli → product_config → app_server → protocol` |
| Agent Loop与状态 | [Agent Runtime模块设计](modules/agent.md) | `agent → session → models/context/tools` |
| 文件修改和执行 | [Managed Patch Runtime模块设计](modules/patches.md) | `patches → agent/artifacts`，再阅读`sandbox/workspace → delivery` |
| 执行授权与审批 | [Execution Plan模块设计](modules/execution.md) | `execution → trusted_actions/processes/sandbox/delivery` |
| 进程与终端监督 | [Process Runtime模块设计](modules/processes.md) | `processes → execution/workspace/secrets/sandbox`，区分兼容Saga与跨平台Supervisor |
| Sandbox隔离与网络 | [Sandbox模块设计](modules/sandbox.md) | `sandbox/contracts → capabilities/network → container/process_runtime`，区分合同库、显式装配与默认产品能力 |
| Secret解析与输出防泄漏 | [Secrets模块设计](modules/secrets.md) | `domain/product_config/execution → secrets/provider → process/sandbox/models → redaction/guard`，区分引用、明文作用域和发布边界 |
| 统一Trusted Action路由 | [Trusted Actions模块设计](modules/trusted-actions.md) | `binding/invocation → resource/policy → execution/approval → audit/executor/reconcile`，再追踪MCP、Skill、Hook与Git Push适配 |
| Workspace路径、快照与租约 | [Workspace模块设计](modules/workspace.md) | `paths/contracts → POSIX/Windows observation → snapshot/verify → lease`，再追踪Execution、Trusted Action、Delivery、Sandbox和Skill消费者 |
| Workspace与Git交付 | [Delivery模块设计](modules/delivery.md) | `desired files → transaction/snapshot/blob/diff → POSIX publish`或`managed worktree → checkpoint → deterministic commit → separately approved push` |
| Action Plane | [Domain模块设计](modules/domain.md)、[Policy模块设计](modules/policy.md)、[Executors模块设计](modules/executors.md)、[Storage模块设计](modules/storage.md)与[Action Plane子系统设计](subsystems/action-plane.md) | `domain → policy → executors/runtime → storage/worker`，先读领域、决策、效果和持久化边界，再读跨包主链 |
| Action HTTP API | [API模块设计](modules/api.md) | `create_app → lifespan/middleware → routes → ActionService`；重点区分POST 202、GET 200、身份缺口、首次持久化与输入安全边界 |
| Framework Adapter | [Adapter模块设计](modules/adapters.md) | `StructuredTool → build_request → Sync/Async Client → ActionSnapshot`；重点区分LangChain Tool兼容意图、真实LangGraph未验证、Tool Call身份与Action状态 |
| MCP扩展 | [MCP模块设计](modules/mcp.md) | `target → connection/catalog → trusted policy/definition → gateway → trusted_actions`；重点区分目录事实、权限事实、Pre-send/After-send、UNKNOWN和默认产品未装配 |
| Skill扩展 | [Skill模块设计](modules/skills.md) | `source → catalog → progressive load/resource → action gateway`；重点区分内容包、Root/Manifest绑定、早期审计缺口、Secret发布边界和默认产品未装配 |
| Hook扩展 | [Hook模块设计](modules/hooks.md) | `definition/grant → registry → dispatch/matcher → hook run → trusted action → 双账本`；重点区分捕获时授权、Action执行Timeout、恢复和默认产品未装配 |
| 受控Provider验证 | [Smoke模块设计](modules/smoke.md) | `network gate → strict config → fixed scenario → Agent/SQLite/Replay → whitelist report`；重点区分Token边界、金额未知、配置对象安全与端点—凭据未绑定 |
| Eval与发布证据 | [Evals模块设计](modules/evals.md) | `task/catalog → materialization → agent run → grader → campaign`；方法读[测试与Eval规范](testing-and-evals.md)，历史数字读[里程碑测试记录](testing-and-evals-milestone-history.md)，真实Provider结果读[验证证据索引](validation/README.md) |
| Trace、Metric与日志 | [Observability模块设计](modules/observability.md) | `core port → no-op/OTel adapter → API/Action/Worker`；Agent链再读`agent/telemetry.py`的故障隔离 |
| Agent Protocol与恢复 | [Protocol模块设计](modules/protocol.md) | `contracts → codec → projection → request ledger`；再读App Server的握手、路由与命令顺序 |
| App Server连接与应用编排 | [App Server模块设计](modules/app-server.md) | `stdio → server → service → runtime/session`；重点区分连接、命令账本、领域事实、Live Delta与关闭生命周期 |
| Python SDK边界 | [SDK模块设计](modules/sdk.md) | 区分Agent Protocol与Action HTTP两套客户端；再读Transport并发、取消和错误边界 |
| 可恢复终端产品 | [Product UI终端产品模块设计](modules/product-ui.md) | `contracts → state_store → projection → session → controller → rendering → app/cli`；重点区分持久事实、类型化Intent、Textual View和stdio组合根 |
| Product Config与安全Fallback | [Product Config模块设计](modules/product-config.md) | `contracts → codec → runtime/store/migration → server/cli`；重点区分源/语义摘要、零暴露切换、CAS与启动事务 |

31个生产源码包、10个根级生产模块、当前相关资料和测试入口见[文档—源码—测试追踪矩阵](governance/documentation-traceability.md)。

### 2.3 参与重大变更

1. 阅读[文档工程规范](governance/documentation-standard.md)；
2. 使用[重大变更设计模板](governance/templates/change-design-template.md)形成评审材料；
3. 长期取舍使用[ADR模板](governance/templates/adr-template.md)；
4. 实现完成后使用[模块设计模板](governance/templates/module-design-template.md)同步当前事实；
5. 将源码符号、测试函数、失败恢复和验证证据加入追踪关系；
6. 门禁通过后才更新路线图完成状态。

## 3. 当前事实源

| 主题 | 当前事实源 | 责任边界 |
|---|---|---|
| 产品定位 | [产品章程](product-charter.md) | 用户、价值、1.0范围与非目标 |
| 交付顺序 | [路线图](roadmap.md) | 切片、依赖、状态和验收门槛 |
| 系统当前结构 | [总体架构](architecture.md) | 系统上下文、模块边界、主流程和当前限制 |
| Agent Runtime | [Agent Runtime模块设计](modules/agent.md) | Thread/Turn/Item/Event、Agent Loop、交互、取消、Retry与恢复 |
| Session | [Session模块设计](modules/session.md) | Event Log、Snapshot、CAS、Fork、迁移、重建和Runtime Owner |
| Context | [Context模块设计](modules/context.md) | Source优先级、预算、模型历史视图、Compaction账本和活动窗口 |
| Model Runtime | [Model Runtime模块设计](modules/models.md) | Provider端口、流状态机、Attempt、Usage、Billing和Cost |
| Artifact | [Artifact模块设计](modules/artifacts.md) | 有界正文、原子发布、分页、完整性验证、TTL和回收 |
| Coding Tool Runtime | [Coding Tool Runtime模块设计](modules/tools.md) | Workspace只读文件/搜索/Git、可信Scope、并发、取消和Artifact捕获 |
| Managed Patch Runtime | [Managed Patch Runtime模块设计](modules/patches.md) | 精确计划、受管副本、单文件/批次审批、持久执行、恢复和Diff Artifact |
| Execution Plan | [Execution Plan模块设计](modules/execution.md) | v1/v2不可变计划、环境/Secret摘要、能力与Sandbox绑定、Policy/Approval和SQLite检查点 |
| Process Runtime | [Process Runtime模块设计](modules/processes.md) | POSIX/Windows进程树Owner、pipe/PTY、Lease/CAS、输出脱敏、取消及重启恢复；兼容Action Saga单独说明 |
| Domain | [Domain模块设计](modules/domain.md) | Action v1模型、状态、Tool Registry、Policy/Approval、Outcome、错误和依赖倒置端口 |
| Policy | [Policy模块设计](modules/policy.md) | 默认Action Policy三分支、完整Effect/Risk矩阵、Service事务边界及Trusted Action策略分界 |
| Executors | [Executors模块设计](modules/executors.md) | Echo与Issue样例、外部效果、双库事务、Outcome/Receipt、UNKNOWN对账和版本漂移边界 |
| Storage | [Storage模块设计](modules/storage.md) | SQLite/PostgreSQL Journal、Schema/Migration、事务、队列、Lease、恢复、后端差异和数据保护边界 |
| Sandbox | [Sandbox模块设计](modules/sandbox.md) | 严格合同、能力探测、固定Container argv、DNS快照、受管出口、Process监督、Profile持久化及真实平台证据边界 |
| Secrets | [Secrets模块设计](modules/secrets.md) | 三套引用合同、环境Provider、短生命周期作用域、常见编码Pattern、流式脱敏、结构化Guard和跨模块装配缺口 |
| Trusted Actions | [Trusted Actions模块设计](modules/trusted-actions.md) | 宿主Binding、规范资源、默认风险Policy、Execution/Approval、Route Hash链、UNKNOWN/Reconcile和扩展能力端口 |
| Workspace | [Workspace模块设计](modules/workspace.md) | 跨平台逻辑路径、选择资源Snapshot、POSIX/Windows对象安全观察、Secure Reader、执行前校验和SQLite Fencing Lease |
| Delivery | [Delivery模块设计](modules/delivery.md) | Workspace Transaction、私有Blob、完整Diff、POSIX可恢复发布、Git Worktree/Checkpoint/Commit和单独批准Push |
| Evals | [Evals模块设计](modules/evals.md) | 历史任务、私有物化、Agent运行、固定评分、Campaign、成本、Compaction语义评测和专用单文件交付 |
| Observability | [Observability模块设计](modules/observability.md) | 内部端口、No-op/OTel适配、W3C持久传播、信号目录、日志、Agent安全包装、故障与隐私边界 |
| Agent Protocol | [Protocol模块设计](modules/protocol.md) | JSON-RPC v1、严格解码、公共投影、Replay/Delta、命令幂等账本、兼容和Schema边界 |
| App Server | [App Server模块设计](modules/app-server.md) | 单连接握手、方法分派、应用服务、后台Turn、持久Replay、Live Delta、Scoped Artifact与stdio并发关闭 |
| SDK | [SDK模块设计](modules/sdk.md) | Agent双Transport、严格Response/Result、协商方法及消息/Replay上限，以及Action Plane HTTP同步/异步客户端 |
| Product UI终端产品 | [Product UI终端产品模块设计](modules/product-ui.md) | Client State、发送前Command分配、纯投影、连接代际、单Actor Controller、Plan/Tool、Approval/Question、Diff证据、Usage/Cost未知、Cancel/Steer、错误自助和stdio冷恢复；0.9.1c三平台CI通过并关闭 |
| Product Config | [Product Config模块设计](modules/product-config.md) | 严格配置、Profile选择、Secret引用、离线诊断、安全Fallback、迁移、CAS和产品启动事务 |
| Action HTTP API | [API模块设计](modules/api.md) | FastAPI Lifespan、Action资源、200/202/404/409/422/500、Trace、身份、输入预算和部署边界 |
| Framework Adapter | [Adapter模块设计](modules/adapters.md) | LangChain StructuredTool工厂、固定Context、Action映射、状态投影、Tool Call幂等恢复及真实LangGraph证据边界 |
| MCP | [MCP模块设计](modules/mcp.md) | 受管Target、不可变目录、Schema边界、SQLite状态、调用前漂移、Trusted Action、UNKNOWN/Reconcile和只读stdio Server |
| Skill | [Skill模块设计](modules/skills.md) | 本地来源、Frontmatter、目录摘要、渐进加载、安全Reader、无正文访问账本、Action Gateway、Secret与提示注入边界 |
| Hook | [Hook模块设计](modules/hooks.md) | Definition/Grant/Registry、精确Matcher、Blocking/Advisory、确定Run、双账本、Timeout/取消、Interrupted恢复和产品接线边界 |
| Smoke | [Smoke模块设计](modules/smoke.md) | 显式网络门禁、严格配置、固定场景、请求/Token边界、审批重开、Replay、白名单报告和Provider认证边界 |
| Action Plane | [Action Plane子系统设计](subsystems/action-plane.md) | Policy、Approval、Journal、Lease、`UNKNOWN`与Reconcile |
| 外部Action契约 | [Action Contract](action-contract.md) | 请求、工具定义、指纹和Trace Context |
| Action状态与恢复 | [Action生命周期](action-lifecycle.md) | 状态转换、租约、`UNKNOWN`和对账 |
| 安全模型 | [威胁模型](threat-model.md) | 资产、信任边界、攻击面和缓解措施 |
| 部署与运维 | [部署总入口](deployment.md) | 当前部署面、拓扑、持久状态、安全约束和六类运维资料入口 |
| 测试与Eval | [测试与Eval规范](testing-and-evals.md) | 当前测试分层、故障注入、Eval指标和发布判定；历史运行数字不在本文维护 |
| 验证证据 | [验证证据索引](validation/README.md) | 固定Revision、环境、预算、结果、脱敏边界和证据谱系 |
| 现行模块设计 | [追踪矩阵](governance/documentation-traceability.md) | DOC-1迁移期间的模块覆盖状态和目标路径 |

里程碑文档描述某个版本切片的交付增量，不再作为单个模块当前实现的唯一事实源。

## 4. 里程碑设计

| 版本 | 设计资料 | 主要能力 |
|---|---|---|
| 0.1/M1 | [Action Plane Worker与PostgreSQL](m1-worker-postgresql.md)、[可观测性](m1-observability.md) | 历史增量；Action Contract、Policy、Approval、Journal、Worker |
| 0.3 | [Agent Runtime Kernel](m03-runtime-kernel.md) | 历史增量；Thread/Turn/Item/Event、Agent Loop、Session与恢复 |
| 0.4 | [Model Runtime](m04-model-runtime.md) | 历史增量；Provider事件、尝试/用量/成本、Smoke |
| 0.5 | [Coding Tool Runtime历史索引](m05-coding-tools.md)与[完整历史](m05-coding-tools-milestone-history.md) | 读取、Artifact、Patch、Process、Git、Eval与交付 |
| 0.6 | [Context与持久会话](m06-context-and-sessions.md) | 历史增量；Context Source、压缩、Thread生命周期和Retry |
| 0.7 | [可信执行与工程交付](m07-trusted-execution-and-delivery.md) | 历史增量；跨平台端口、Sandbox、Secret、Process、Workspace与Delivery |
| 0.8 | [产品运行时与扩展历史索引](m08-product-runtime-and-extensions.md)与[完整历史](m08-product-runtime-and-extensions-milestone-history.md) | Protocol、App Server、SDK、MCP、Skill、Hook和Provider配置 |
| 0.9.0 | [代码可维护性治理](m09-code-maintainability.md) | 历史增量；代码说明、职责拆分、复杂度与依赖基线 |
| 0.9.1 | [CLI/TUI产品体验详细设计](changes/m09-1-cli-tui-product-experience.md)；[0.9.1c完整领域交互详细设计](changes/m09-1c-domain-interactions.md)；[0.9.1d配置与Windows只读链详细设计](changes/m09-1d-configuration-preflight-windows-read.md)；[0.9.1e默认Trusted Action组合详细设计](changes/m09-1e-default-trusted-action-composition.md) | 进行中；0.9.1a～d已关闭；e1由[CI 34739842959](https://github.com/carrie1988/Harnessix/actions/runs/34739842959)验收关闭，e2～e5待实施 |

0.6专题历史设计包括[窗口规划](compaction-window-planning.md)、[Compaction运行时与活动窗口](compaction-runtime-and-windows.md)、[摘要尝试账本](compaction-attempt-ledger.md)、[Thread生命周期](thread-lifecycle.md)和[Turn Retry/Provider切换](turn-retry-and-provider-switch.md)。这些资料解释对应切片的形成过程；当前行为统一由Context、Agent、Session、Models和Artifacts模块设计维护。

### 4.1 根目录聚合文档角色

| 文档 | 角色 | 是否维护当前事实 | 使用限制 |
|---|---|---|---|
| [总体架构](architecture.md) | 系统级现行设计 | 是 | 维护组件、主链、状态、数据、安全和部署边界，不展开每个类的全部细节 |
| [路线图](roadmap.md) | 规划与完成证据索引 | 是 | 只管理范围、依赖、状态和验收，不充当模块设计 |
| [产品章程](product-charter.md) | 产品目标与发布边界 | 是 | 不描述易变化的实现细节 |
| [Action Contract](action-contract.md) | 稳定外部契约 | 是 | Action Plane契约变化必须同步实现和合同测试 |
| [Action生命周期](action-lifecycle.md) | Action状态与恢复契约 | 是 | 不替代Agent Turn生命周期设计 |
| [威胁模型](threat-model.md) | 统一风险登记 | 是 | 模块缓解措施应链接到现行模块设计和测试 |
| [测试与Eval规范](testing-and-evals.md) | 质量规范聚合入口 | 是 | 只定义跨模块测试策略、Eval指标、证据生命周期和发布判定 |
| [测试里程碑历史](testing-and-evals-milestone-history.md) | 冻结的历史验收记录 | 否 | 保留阶段数字与失败演进，不作为当前测试策略或发布状态 |
| [部署与运维](deployment.md) | 运维聚合入口 | 是 | 只维护拓扑、跨组件硬约束和安装/配置/升级/恢复/诊断/平台入口 |
| [部署里程碑历史](deployment-milestone-history.md) | 冻结的旧版本部署记录 | 否 | 保留0.3～0.8当时命令和升级证据，不作为当前手册 |
| `m03`～`m09`里程碑文档 | 历史增量设计 | 否 | 用于解释某阶段交付，不作为当前模块事实的唯一来源 |
| [ADR索引](adr/README.md) | 决策历史 | 否 | 解释为什么选择，不重复当前实现全文 |
| [源码研究索引](research/README.md) | 参考源码证据 | 否 | 不能直接成为Harnessix的产品契约 |
| [验证目录](validation/) | 特定版本证据 | 否 | 结论不得外推到未记录的平台、Provider或输入 |

## 5. 架构决策和源码研究

- [ADR索引](adr/README.md)：记录80份长期决策的状态、背景、候选方案、选择和后果；
- [源码研究计划](research-plan.md)：定义参考版本、研究问题和clean-room边界；
- [源码研究索引](research/README.md)：Codex、OpenCode、Claude Code等30份冻结参考实现证据及访问日期；
- [自研与复用边界](build-vs-buy.md)：第三方依赖、许可证和自研边界。

ADR回答“为什么这样选择”，源码研究回答“参考实现有什么证据”，二者都不替代当前模块设计。

## 6. 测试、部署和验证证据

- [测试与Eval规范](testing-and-evals.md)：当前测试策略与发布门槛；
- [测试里程碑历史](testing-and-evals-milestone-history.md)：DOC-1.5前冻结的阶段验收记录；
- [受控模型Smoke指南](model-smoke.md)；
- [部署与运维](deployment.md)：当前部署拓扑和统一入口；
- [安装与制品](operations/installation.md)、[配置参考](operations/configuration.md)、[升级与回退](operations/upgrade-and-rollback.md)；
- [故障恢复](operations/recovery.md)、[诊断与可观测性](operations/diagnostics.md)、[平台与运行环境](operations/platforms.md)；
- [验证证据索引](validation/README.md)：真实Provider Smoke与Coding Eval证据谱系；
- [威胁模型](threat-model.md)。

验证证据绑定特定代码、环境和输入，不应被解释为所有模型、平台或用户任务均已通过。

## 7. 文档治理

- [治理入口](governance/README.md)；
- [文档工程规范](governance/documentation-standard.md)；
- [DOC-1.0全量盘点](governance/documentation-inventory.md)；
- [文档—源码—测试追踪矩阵](governance/documentation-traceability.md)；
- [整改待办](governance/documentation-remediation-backlog.md)；
- [DOC-1.0机器基线](baselines/documentation-doc1.0-start.json)。
- [文档策略v1](../governance/documentation-policy-v1.json)与[文档检查器](../scripts/documentation_check.py)：自动门禁合同和实现入口。

DOC-1.1已建立本导航、总体架构和源码阅读主链；DOC-1.2已完成Agent Runtime与Action Plane两份黄金样例。DOC-1.3的19个Coding Agent主链模块已全部完成；DOC-1.4已完成Protocol、App Server、SDK、Product Config、API、Adapter、MCP、Skill、Hook与Smoke。31/31个生产源码包均已有独立现行模块设计。DOC-1.5已完成测试/验证证据、六类运维资料、里程碑/0.6专题设计、76份既有ADR和27份源码研究的职责、状态及入口治理。0.9.1d新增的源码研究、ADR和详细设计继续受同一文档合同约束。DOC-1.6已按照[架构决策](adr/0077-versioned-documentation-contract-and-gates.md)和[详细设计](changes/doc-1.6-automated-documentation-gates.md)交付版本化策略、全库检查器、源码差异同步、公共合同漂移检查、31项治理回归和三平台CI门禁；Linux文档任务同时执行变化Mermaid真实渲染。

## 8. 文档状态说明

新增或重写的正式文档使用以下状态：

- `draft`：未完成评审；
- `reviewing`：正在评审；
- `current`：与标注代码版本一致；
- `historical`：只描述历史增量；
- `superseded`：已被明确取代；
- `deprecated`：仍保留兼容背景但不应继续采用。

仓库内202份Markdown均受DOC-1.6严格门禁约束。文档是否可作为现行依据仍必须同时核对YAML状态、`code_revision`、当前模块设计和验证证据，不能仅凭正文中的“完成”字样判断。
