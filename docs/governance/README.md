---
doc_type: governance-index
status: current
version: 6
code_revision: 7a325f2ef11bb369f396c739992ea170cfcce8ac
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
| DOC-1.3 | Coding Agent主链模块设计 | 进行中（3/19） | [Session](../modules/session.md)、[Context](../modules/context.md)、[Artifact](../modules/artifacts.md)已完成；Model、Tool、Process、Workspace等待迁移 |
| DOC-1.4 | 产品运行时与扩展模块设计 | 未开始 | Protocol、App Server、SDK、MCP、Skill、Hook等模块文档 |
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
