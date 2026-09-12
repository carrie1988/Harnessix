---
doc_type: governance-index
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

# Harnessix Code 文档治理

## 1. 目标

DOC-1用于建立与生产级Coding Agent相匹配的文档工程能力，使设计资料能够直接支持架构评审、源码阅读、测试设计、故障定位和后续维护。文档不是提交完成后的摘要，而是需求、决策、实现和验证之间的可追溯接口。

## 2. 边界

DOC-1是0.9.1开始前的横向阻断工作流，不改变0.9.0已完成的代码治理结论，也不替代0.9各产品切片。DOC-1.0建立基线和规则；DOC-1.1建立统一导航、当前系统架构和源码阅读主链；模块级现行设计从DOC-1.2开始迁移。

## 3. DOC-1阶段

| 阶段 | 目标 | 状态 | 产物 |
|---|---|---|---|
| DOC-1.0 | 文档盘点、分类、规范、模板、追踪矩阵和整改待办 | 已完成 | 本目录与[起始基线](../baselines/documentation-doc1.0-start.json) |
| DOC-1.1 | 文档导航、系统架构和源码阅读总入口 | 已完成 | [文档中心](../README.md)、[总体架构](../architecture.md)、[源码阅读地图](../guides/source-reading-map.md) |
| DOC-1.2 | Agent Runtime与Action Plane黄金样例 | 已完成 | [Agent Runtime](../modules/agent.md)、[Action Plane](../subsystems/action-plane.md) |
| DOC-1.3 | Coding Agent主链模块设计 | 已完成（19/19） | Wave A～D全部完成；最后一批为[Workspace](../modules/workspace.md)、[Delivery](../modules/delivery.md)、[Evals](../modules/evals.md)和[Observability](../modules/observability.md) |
| DOC-1.4 | 产品运行时与扩展模块设计 | 进行中（2/10） | [Protocol](../modules/protocol.md)与[App Server](../modules/app-server.md)已完成；下一项为SDK，之后为配置、API、Adapter、MCP、Skill、Hook和Smoke |
| DOC-1.5 | Eval、历史设计和验证资料治理 | 未开始 | 现行资料与历史证据分层、聚合文档拆分 |
| DOC-1.6 | 自动化门禁 | 未开始 | 元数据、链接、结构、源码映射和陈旧性检查 |

“已完成”只表示对应切片的验收边界已经满足。DOC-1.0/1.1完成不表示30个包的模块设计或133份存量文档已经全部整改完成。

## 4. 入口

- [文档工程规范](documentation-standard.md)：文档职责、结构、状态、链接和评审规则；
- [现状全量盘点](documentation-inventory.md)：固定提交上的数量基线、结构缺口和完整目录；
- [文档—源码—测试追踪矩阵](documentation-traceability.md)：目标追踪模型及30个源码包的覆盖状态；
- [整改待办](documentation-remediation-backlog.md)：DOC-1.1至DOC-1.6的执行顺序和验收边界；
- [文档中心](../README.md)：面向读者的统一入口和资料角色；
- [当前总体架构](../architecture.md)：系统边界、模块依赖、数据流、五条系统时序和源码测试映射；
- [源码阅读地图](../guides/source-reading-map.md)：产品启动、Agent Loop、可信执行和Action Plane逐文件路线；
- [Agent Runtime模块设计](../modules/agent.md)：DOC-1后模块设计黄金样例；
- [Action Plane子系统设计](../subsystems/action-plane.md)：DOC-1后跨包子系统设计黄金样例；
- [Session模块设计](../modules/session.md)：Event Log、CAS、Fork、迁移和重建的当前事实；
- [Context模块设计](../modules/context.md)：Source、预算、模型历史视图和Compaction的当前事实；
- [Artifact模块设计](../modules/artifacts.md)：有界正文、原子发布、分页、验证和回收的当前事实；
- [Model Runtime模块设计](../modules/models.md)：Provider、流状态机、Attempt、Usage和Cost的当前事实；
- [Coding Tool Runtime模块设计](../modules/tools.md)：Workspace只读文件/搜索/Git、Scope、并发、取消和Artifact捕获的当前事实；
- [Managed Patch Runtime模块设计](../modules/patches.md)：精确计划、受管副本、单文件/批次审批执行、崩溃恢复和Diff Artifact的当前事实；
- [Execution Plan模块设计](../modules/execution.md)：不可变执行计划、环境/Secret摘要、能力/Sandbox、Policy/Approval和持久检查点的当前事实；
- [Process Runtime模块设计](../modules/processes.md)：兼容Action Saga与跨平台Owner、Lease/CAS、pipe/PTY、输出脱敏、取消和恢复的当前事实；
- [Domain模块设计](../modules/domain.md)：Action v1模型、状态、Tool Registry、Policy/Approval、Outcome、错误和依赖倒置端口的当前事实；
- [Policy模块设计](../modules/policy.md)：默认三分支、完整Effect/Risk矩阵、Action Service边界及Trusted Action策略分界的当前事实；
- [Executors模块设计](../modules/executors.md)：内置效果样例、双库事务、幂等、Outcome/Receipt、UNKNOWN对账、部署和版本边界的当前事实；
- [Storage模块设计](../modules/storage.md)：双后端Schema/Migration、事务、队列、Lease、恢复、一致性、安全和运维边界的当前事实；
- [Sandbox模块设计](../modules/sandbox.md)：严格合同、能力探测、固定Container执行、网络/Egress、Process监督、Profile持久化和平台证据边界的当前事实；
- [Secrets模块设计](../modules/secrets.md)：三套引用合同、环境Provider、短生命周期作用域、流式脱敏、结构化Guard和跨模块消费边界的当前事实；
- [Trusted Actions模块设计](../modules/trusted-actions.md)：宿主Binding、规范资源、风险Policy、Execution/Approval、Route Hash链、UNKNOWN/Reconcile和扩展能力边界的当前事实；
- [Workspace模块设计](../modules/workspace.md)：逻辑路径、选择资源Snapshot、POSIX/Windows对象安全观察、Secure Reader、校验和SQLite Fencing Lease的当前事实；
- [Delivery模块设计](../modules/delivery.md)：Workspace Transaction、私有Blob、完整Diff、POSIX发布与恢复、Git Worktree/Checkpoint/Commit和单独批准Push的当前事实；
- [Evals模块设计](../modules/evals.md)：历史任务、物化、正式Agent运行、固定评分、Campaign、成本、Compaction语义评测和专用单文件交付的当前事实；
- [Observability模块设计](../modules/observability.md)：内部端口、OpenTelemetry、日志、持久Trace、完整信号目录、Agent安全包装和故障隔离边界的当前事实；
- [Protocol模块设计](../modules/protocol.md)：公共JSON-RPC合同、严格帧、投影、Replay/Delta、请求账本、兼容及Schema边界的当前事实；
- [App Server模块设计](../modules/app-server.md)：单连接握手、应用服务、后台Turn、stdio并发关闭、Scoped Artifact与默认装配边界的当前事实；
- [详细设计模板](templates/detailed-design-template.md)；
- [模块设计模板](templates/module-design-template.md)；
- [重大变更设计模板](templates/change-design-template.md)；
- [ADR模板](templates/adr-template.md)；
- [源码研究模板](templates/source-reading-template.md)。

## 5. 当前基线摘要

基线固定在提交`8e3e3576bbc64bf2b696279c404ba5fcb3fee06e`：

- `docs/`下共有133份Markdown、18,831行；
- 76份ADR、27份源码研究、25份顶层设计/指南、5份验证证据；
- 30个顶层生产源码包均没有独立的现行模块设计文档；
- 0份文档包含Mermaid图；
- 0份文档通过Markdown链接指向当前`src/harnessix/`源码或`tests/`测试；
- 96份文档可识别到显式状态文本，但形成61种不同写法；
- 现有406个相对链接未发现目标路径缺失；锚点和281个外部链接未在本阶段验证。

以上数字的口径、限制和逐文档记录以[机器可读基线](../baselines/documentation-doc1.0-start.json)为准。
