---
doc_type: governance-index
status: current
version: 1
code_revision: 8e3e3576bbc64bf2b696279c404ba5fcb3fee06e
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

DOC-1是0.9.1开始前的横向阻断工作流，不改变0.9.0已完成的代码治理结论，也不替代0.9各产品切片。DOC-1.0只建立基线和规则，不重写既有模块设计、不修改生产代码、不提前引入CI文档门禁。

## 3. DOC-1阶段

| 阶段 | 目标 | 状态 | 产物 |
|---|---|---|---|
| DOC-1.0 | 文档盘点、分类、规范、模板、追踪矩阵和整改待办 | 已完成 | 本目录与[起始基线](../baselines/documentation-doc1.0-start.json) |
| DOC-1.1 | 文档导航、系统架构和源码阅读总入口 | 未开始 | 文档地图、系统上下文、主链阅读路线 |
| DOC-1.2 | Agent Runtime与Action Plane黄金样例 | 未开始 | 两份符合新规范的完整模块设计 |
| DOC-1.3 | Coding Agent主链模块设计 | 未开始 | Model、Context、Tool、Process、Workspace等模块文档 |
| DOC-1.4 | 产品运行时与扩展模块设计 | 未开始 | Protocol、App Server、SDK、MCP、Skill、Hook等模块文档 |
| DOC-1.5 | Eval、历史设计和验证资料治理 | 未开始 | 现行资料与历史证据分层、聚合文档拆分 |
| DOC-1.6 | 自动化门禁 | 未开始 | 元数据、链接、结构、源码映射和陈旧性检查 |

“已完成”表示DOC-1.0要求的治理资料已经建立，不表示133份存量文档已经整改完成。

## 4. 入口

- [文档工程规范](documentation-standard.md)：文档职责、结构、状态、链接和评审规则；
- [现状全量盘点](documentation-inventory.md)：固定提交上的数量基线、结构缺口和完整目录；
- [文档—源码—测试追踪矩阵](documentation-traceability.md)：目标追踪模型及30个源码包的覆盖状态；
- [整改待办](documentation-remediation-backlog.md)：DOC-1.1至DOC-1.6的执行顺序和验收边界；
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
