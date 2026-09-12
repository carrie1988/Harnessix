---
doc_type: governance
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

# Harnessix Code 文档现状全量盘点

## 1. 盘点目的

本盘点为DOC-1整改建立可重复比较的起点，回答“现有资料是什么、各自承担什么角色、与源码和测试能否互相定位、优先整改什么”。它不把章节关键词数量等同于内容质量，也不把存量文档缺少新规范字段解释为实现缺陷。

## 2. 固定范围与方法

- 基线提交：`8e3e3576bbc64bf2b696279c404ba5fcb3fee06e`；
- 文档范围：该提交受Git管理的`docs/**/*.md`，不包含本次新增治理资料；
- 源码范围：`src/harnessix/`下30个顶层生产包；
- 测试范围：`tests/`；
- 行数口径：物理文本行；
- 状态口径：前80行中以“状态：”或“文档状态：”开头的显式文本；
- 章节口径：Markdown标题关键词匹配，只衡量是否有结构入口；
- 源码引用口径：识别现存仓库路径文本，并单独检查是否存在真正的Markdown源码/测试链接。

### 2.1 限制

1. 相对链接只校验目标路径存在，不校验标题锚点；
2. 外部链接未联网验证可用性；
3. 章节标题命中不证明内容完整，未命中也不证明正文完全未涉及该主题；
4. 自然语言中的类名和函数名未做静态语义解析；
5. 机器可读明细以[DOC-1.0起始基线](../baselines/documentation-doc1.0-start.json)为准。

## 3. 结论摘要

1. 文档数量已经形成规模，但主要按里程碑和决策累积，缺少按当前源码模块组织的稳定阅读入口；
2. 76份ADR承担了大量历史决策，但ADR不是现行模块详细设计的替代品；
3. 30个顶层生产包都没有`docs/modules/<package>.md`形式的独立现行模块设计；
4. 现有文档没有Mermaid图，需求背景、数据流、类设计、数据结构、源码映射和伪代码等结构化入口接近空白；
5. 文档中存在路径文本，但没有可点击的当前源码或测试Markdown链接，读者无法稳定从设计跳到实现；
6. 状态文本覆盖率尚可，但61种自由写法无法自动判断文档是否为当前事实；
7. 现有相对路径链接未发现目标缺失，说明可在不破坏导航的前提下渐进迁移。

## 4. 数量与结构基线

| 指标 | 数值 | 解释 |
|---|---:|---|
| Markdown文档 | 133 | 固定提交下的全部`docs/**/*.md` |
| Markdown物理行 | 18,831 | 不含本次治理资料 |
| 顶层设计与指南 | 25 | 位于`docs/`根目录 |
| ADR | 76 | 决策记录 |
| 源码研究 | 27 | 参考实现证据 |
| 验证证据 | 5 | 受控验证结果 |
| 显式状态文档 | 96 | 状态文本尚未标准化 |
| 状态文本变体 | 61 | 目标收敛为6个机器枚举 |
| Mermaid文档/代码块 | 0/0 | 不代表没有ASCII图 |
| 相对/外部链接 | 406/281 | 外部链接未联网验证 |
| 目标缺失的相对路径 | 0 | 锚点不在本阶段检查 |
| 无Markdown链接文档 | 48 | 既无相对链接也无外部链接 |
| 含现存仓库路径文本的文档 | 22 | 每份文档内去重 |
| 现存仓库路径文本 | 127 | 包含源码、测试、脚本和示例 |
| 含当前源码Markdown链接的文档 | 0 | 目标是所有现行模块设计 |
| 含当前测试Markdown链接的文档 | 0 | 目标是所有正式设计 |
| 顶层生产源码包 | 30 | 仅统计目录，不统计根级Python文件 |
| 根级生产Python模块 | 10 | 入口、装配、兼容门面及公共导出 |
| 具有独立模块设计的源码包 | 0 | 目标路径为`docs/modules/<package>.md` |


### 4.1 分类规模

| 分类 | 文档数 | 总行数 | 平均行数 |
|---|---:|---:|---:|
| 顶层设计与指南 | 25 | 9,655 | 386.2 |
| ADR | 76 | 5,542 | 72.9 |
| 源码研究 | 27 | 3,305 | 122.4 |
| 验证证据 | 5 | 329 | 65.8 |


### 4.2 ADR长度分布

| 长度 | 数量 | 风险解释 |
|---|---:|---|
| 不超过50行 | 30 | 适合简洁决策，但不能据此承担完整实现说明 |
| 51～100行 | 27 | 仍应只维护决策、理由与后果 |
| 101～200行 | 18 | 需要在DOC-1.5检查是否混入详细设计 |
| 超过200行 | 1 | 优先检查职责混杂和可拆分内容 |

## 5. 标准章节覆盖

以下数字只表示标题关键词命中，不评价段落深度。新规范不会要求每份ADR包含所有章节，而是要求模块/详细设计按适用性完整覆盖。

| 结构入口 | 文档数 | 占133份比例 |
|---|---:|---:|
| 需求背景 | 0 | 0.0% |
| 设计目标 | 1 | 0.8% |
| 范围/非目标 | 23 | 17.3% |
| 总体架构/系统上下文 | 2 | 1.5% |
| 模块/职责边界 | 6 | 4.5% |
| 核心/主流程 | 2 | 1.5% |
| 时序 | 1 | 0.8% |
| 数据流 | 0 | 0.0% |
| 类设计 | 0 | 0.0% |
| 接口设计 | 0 | 0.0% |
| 数据结构 | 0 | 0.0% |
| 字段说明/约束 | 1 | 0.8% |
| 失败与恢复 | 10 | 7.5% |
| 安全 | 31 | 23.3% |
| 可观测性 | 16 | 12.0% |
| 测试/验收 | 70 | 52.6% |
| 源码映射 | 0 | 0.0% |
| 伪代码 | 0 | 0.0% |


## 6. 主要问题与根因

### 6.1 组织轴与源码阅读轴不一致

现有资料主要按0.3～0.9里程碑和ADR编号组织。随着实现跨多个迭代累积，同一个包的当前行为分散在若干里程碑文档、ADR和研究材料中。读者必须先知道历史演进，才能拼出当前实现。

整改方向：以30个顶层生产包建立现行模块设计，以系统架构和主链阅读指南作为入口；里程碑文档保留交付历史，不再承担唯一的当前实现说明。

### 6.2 决策、设计和证据职责混杂

ADR数量多但平均较短，适合保留决策；聚合文档则持续增长，其中测试与Eval、部署、Coding Tools等文档已成为大型汇总。职责混杂导致更新时难以判断应修改哪一份资料。

整改方向：ADR保留“为什么”，模块设计维护“现在如何工作”，重大变更设计冻结“这次改了什么”，验证文档只保留证据。

### 6.3 图示和结构化契约不足

Mermaid块为0；需求背景、数据流、类设计、数据结构、源码映射和伪代码等标题覆盖极低。ASCII架构图和散落文字可以传递部分信息，但难以系统展示跨模块时序、失败恢复和字段责任。

整改方向：按文档类型强制上下文、时序、状态和数据流图；每张图配文字说明、源码映射和适用性判断。

### 6.4 文档与实现缺少可点击追踪

22份文档含有127个可识别的现存仓库路径文本，但没有一份文档以Markdown链接指向当前源码或测试。路径可能在重构后失效，也不能表达具体符号和验证关系。

整改方向：现行设计使用相对文件链接加稳定符号名；历史设计使用固定提交永久链接；测试映射到文件和测试函数/合同套件。

### 6.5 状态无法机器判定

96份文档具有显式状态，但存在61种文本表达，状态、阶段进度和验收结果混在同一行。

整改方向：YAML状态收敛到`draft`、`reviewing`、`current`、`historical`、`superseded`、`deprecated`；详细进度保留正文。

## 7. 高复杂度聚合文档

| 排名 | 文档 | 行数 | DOC-1处理方向 |
|---:|---|---:|---|
| 1 | [Harnessix Code 测试与 Eval 规范 v1](../testing-and-evals.md) | 1670 | 建立索引并拆出模块事实源 |
| 2 | [部署与运行](../deployment.md) | 1238 | 建立索引并拆出模块事实源 |
| 3 | [0.5 Coding Tool Runtime 详细实施设计](../m05-coding-tools.md) | 1097 | 建立索引并拆出模块事实源 |
| 4 | [Harnessix Code 威胁模型 v2](../threat-model.md) | 648 | 建立索引并拆出模块事实源 |
| 5 | [Harnessix Code 0.8 产品运行时与扩展详细设计](../m08-product-runtime-and-extensions.md) | 647 | 建立索引并拆出模块事实源 |
| 6 | [Harnessix Code 设计与开发路线图](../roadmap.md) | 539 | 建立索引并拆出模块事实源 |
| 7 | [Harnessix Code 总体架构](../architecture.md) | 526 | 建立索引并拆出模块事实源 |
| 8 | [Harnessix Code 0.7 可信执行与工程交付设计](../m07-trusted-execution-and-delivery.md) | 463 | 建立索引并拆出模块事实源 |
| 9 | [0.6 Context Engine 与持久会话详细实施设计](../m06-context-and-sessions.md) | 428 | 建立索引并拆出模块事实源 |
| 10 | [0.3 Agent Runtime Kernel 实施设计](../m03-runtime-kernel.md) | 326 | 建立索引并拆出模块事实源 |
| 11 | [0.4 Model Runtime 实施计划](../m04-model-runtime.md) | 307 | 建立索引并拆出模块事实源 |
| 12 | [0.7 可信执行与工程交付研究](../research/trusted-execution-and-delivery.md) | 269 | 保留证据，补充导航和职责边界 |


## 8. 全量文档目录

说明：状态列只表示是否检测到存量自由文本，不将其强行映射为新状态；“代码链接”统计指向当前`src/harnessix/`或`tests/`的Markdown链接。

### 8.1 顶层设计与指南（25份）

| 文档 | 角色 | 行数 | 显式状态 | Mermaid | 代码链接 |
|---|---|---:|---|---:|---:|
| [Action Contract v1](../action-contract.md) | 契约 | 85 | 缺失 | 0 | 0 |
| [Action 生命周期](../action-lifecycle.md) | 详细设计 | 71 | 缺失 | 0 | 0 |
| [Harnessix Code 总体架构](../architecture.md) | 系统架构 | 526 | 缺失 | 0 | 0 |
| [自研与复用边界](../build-vs-buy.md) | 决策分析 | 55 | 缺失 | 0 | 0 |
| [0.6.3 摘要尝试账本详细设计](../compaction-attempt-ledger.md) | 详细设计 | 102 | 检测到（待标准化） | 0 | 0 |
| [0.6.3 自动Compaction运行时与活动窗口详细设计](../compaction-runtime-and-windows.md) | 详细设计 | 156 | 检测到（待标准化） | 0 | 0 |
| [0.6.3 压缩窗口规划与候选校验详细设计](../compaction-window-planning.md) | 详细设计 | 161 | 检测到（待标准化） | 0 | 0 |
| [部署与运行](../deployment.md) | 部署设计 | 1238 | 缺失 | 0 | 0 |
| [0.3 Agent Runtime Kernel 实施设计](../m03-runtime-kernel.md) | 里程碑聚合设计 | 326 | 检测到（待标准化） | 0 | 0 |
| [0.4 Model Runtime 实施计划](../m04-model-runtime.md) | 里程碑聚合设计 | 307 | 检测到（待标准化） | 0 | 0 |
| [0.5 Coding Tool Runtime 详细实施设计](../m05-coding-tools.md) | 里程碑聚合设计 | 1097 | 检测到（待标准化） | 0 | 0 |
| [0.6 Context Engine 与持久会话详细实施设计](../m06-context-and-sessions.md) | 里程碑聚合设计 | 428 | 检测到（待标准化） | 0 | 0 |
| [Harnessix Code 0.7 可信执行与工程交付设计](../m07-trusted-execution-and-delivery.md) | 里程碑聚合设计 | 463 | 检测到（待标准化） | 0 | 0 |
| [Harnessix Code 0.8 产品运行时与扩展详细设计](../m08-product-runtime-and-extensions.md) | 里程碑聚合设计 | 647 | 缺失 | 0 | 0 |
| [0.9.0代码可读性、可维护性与结构治理详细设计](../m09-code-maintainability.md) | 里程碑聚合设计 | 226 | 检测到（待标准化） | 0 | 0 |
| [M1.2 可观测性与运行保障设计](../m1-observability.md) | 里程碑聚合设计 | 194 | 缺失 | 0 | 0 |
| [M1 独立 Worker 与 PostgreSQL 设计](../m1-worker-postgresql.md) | 里程碑聚合设计 | 126 | 缺失 | 0 | 0 |
| [受控模型 Smoke 使用说明](../model-smoke.md) | 验证指南 | 93 | 缺失 | 0 | 0 |
| [Harnessix Code 产品章程](../product-charter.md) | 产品章程 | 112 | 缺失 | 0 | 0 |
| [主流 Coding Agent 源码研究计划](../research-plan.md) | 研究计划 | 113 | 缺失 | 0 | 0 |
| [Harnessix Code 设计与开发路线图](../roadmap.md) | 路线图 | 539 | 检测到（待标准化） | 0 | 0 |
| [Harnessix Code 测试与 Eval 规范 v1](../testing-and-evals.md) | 测试与评测设计 | 1670 | 检测到（待标准化） | 0 | 0 |
| [Thread Resume、Fork 与 Archive 详细设计](../thread-lifecycle.md) | 详细设计 | 130 | 检测到（待标准化） | 0 | 0 |
| [Harnessix Code 威胁模型 v2](../threat-model.md) | 威胁模型 | 648 | 检测到（待标准化） | 0 | 0 |
| [Turn Retry、Interrupted Recovery 与 Provider 切换详细设计](../turn-retry-and-provider-switch.md) | 详细设计 | 142 | 检测到（待标准化） | 0 | 0 |

### 8.2 ADR（76份）

| 文档 | 角色 | 行数 | 显式状态 | Mermaid | 代码链接 |
|---|---|---:|---|---:|---:|
| [ADR 0001：采用 Python-first Runtime](../adr/0001-python-first-runtime.md) | 架构决策 | 19 | 检测到（待标准化） | 0 | 0 |
| [ADR 0002：将 `UNKNOWN` 作为一等结果](../adr/0002-unknown-first-class.md) | 架构决策 | 19 | 检测到（待标准化） | 0 | 0 |
| [ADR 0003：使用 Journal 状态实现持久化 Worker Queue](../adr/0003-database-backed-worker-queue.md) | 架构决策 | 20 | 检测到（待标准化） | 0 | 0 |
| [ADR-0004：在 Action 快照中持久化 W3C Trace Context](../adr/0004-durable-trace-context.md) | 架构决策 | 53 | 检测到（待标准化） | 0 | 0 |
| [ADR 0005：将 Harnessix 演进为 Harnessix Code](../adr/0005-evolve-to-harnessix-code.md) | 架构决策 | 54 | 检测到（待标准化） | 0 | 0 |
| [ADR 0006：采用 Thread、Turn、Item 与 AgentEvent 领域模型](../adr/0006-thread-turn-item-event-model.md) | 架构决策 | 128 | 检测到（待标准化） | 0 | 0 |
| [ADR 0007：采用持久边界驱动的 Agent Loop 与分层取消](../adr/0007-agent-loop-and-cancellation.md) | 架构决策 | 156 | 检测到（待标准化） | 0 | 0 |
| [ADR 0008：采用模型无关的流式 Provider Event](../adr/0008-provider-event-model.md) | 架构决策 | 141 | 检测到（待标准化） | 0 | 0 |
| [ADR 0009：App Server v1 使用标准 JSON-RPC 2.0 与 stdio JSONL](../adr/0009-app-server-protocol.md) | 架构决策 | 146 | 检测到（待标准化） | 0 | 0 |
| [ADR 0010：SQLite Event Log、事务投影与保守恢复](../adr/0010-session-store-and-recovery.md) | 架构决策 | 147 | 检测到（待标准化） | 0 | 0 |
| [ADR 0011：0.3 Kernel 使用进程内宿主与初始聚合投影](../adr/0011-kernel-host-and-initial-projection.md) | 架构决策 | 60 | 检测到（待标准化） | 0 | 0 |
| [ADR 0012：持久审批检查点与显式继续](../adr/0012-durable-approval-checkpoint.md) | 架构决策 | 99 | 检测到（待标准化） | 0 | 0 |
| [ADR 0013：Kernel 语义契约、诊断与存储验收](../adr/0013-kernel-contracts-and-telemetry.md) | 架构决策 | 65 | 检测到（待标准化） | 0 | 0 |
| [ADR 0014：首个 OpenAI-compatible Provider](../adr/0014-openai-compatible-provider.md) | 架构决策 | 52 | 检测到（待标准化） | 0 | 0 |
| [ADR 0015：第二类 Provider 与用量演进边界](../adr/0015-anthropic-provider.md) | 架构决策 | 57 | 检测到（待标准化） | 0 | 0 |
| [ADR 0016：模型尝试账本与累计用量事实](../adr/0016-model-attempt-ledger.md) | 架构决策 | 72 | 检测到（待标准化） | 0 | 0 |
| [ADR 0017：实际 SDK 的尝试与用量映射](../adr/0017-provider-attempt-usage.md) | 架构决策 | 60 | 检测到（待标准化） | 0 | 0 |
| [ADR 0018：版本化 Token 价格与可重算成本报告](../adr/0018-versioned-token-cost.md) | 架构决策 | 64 | 检测到（待标准化） | 0 | 0 |
| [ADR 0019：受控模型 Smoke 与白名单诊断](../adr/0019-controlled-model-smoke.md) | 架构决策 | 35 | 检测到（待标准化） | 0 | 0 |
| [ADR 0020：响应计费元数据与尝试绑定](../adr/0020-observed-billing-context.md) | 架构决策 | 41 | 检测到（待标准化） | 0 | 0 |
| [ADR 0021：并发 Session 初始化的 WAL 切换边界](../adr/0021-session-wal-initialization.md) | 架构决策 | 32 | 检测到（待标准化） | 0 | 0 |
| [ADR 0022：百炼单次计价验收的证据边界](../adr/0022-bailian-price-validation.md) | 架构决策 | 46 | 检测到（待标准化） | 0 | 0 |
| [ADR 0023：工作区绑定与首批只读编码工具](../adr/0023-workspace-read-tools.md) | 架构决策 | 43 | 检测到（待标准化） | 0 | 0 |
| [ADR 0024：有界搜索与 Artifact 归属拆片](../adr/0024-bounded-search-and-artifact-scope.md) | 架构决策 | 48 | 检测到（待标准化） | 0 | 0 |
| [ADR 0025：可信工具执行作用域与旧端口兼容](../adr/0025-trusted-tool-execution-scope.md) | 架构决策 | 50 | 检测到（待标准化） | 0 | 0 |
| [ADR 0026：同一 Session 事务发布有界 Artifact](../adr/0026-transactional-artifacts.md) | 架构决策 | 36 | 检测到（待标准化） | 0 | 0 |
| [ADR 0027：先准备精确 Patch，再开放受控写入](../adr/0027-prepared-patch-and-write-admission.md) | 架构决策 | 48 | 检测到（待标准化） | 0 | 0 |
| [ADR 0028：受管副本内的持久单文件 Patch 执行](../adr/0028-managed-patch-execution.md) | 架构决策 | 47 | 检测到（待标准化） | 0 | 0 |
| [ADR 0029：受管 Patch 的调用绑定与 Agent 接入顺序](../adr/0029-managed-patch-agent-bridge.md) | 架构决策 | 60 | 检测到（待标准化） | 0 | 0 |
| [ADR 0030：Kernel 写准入、持久审批与双账本恢复](../adr/0030-kernel-managed-patch-admission.md) | 架构决策 | 134 | 检测到（待标准化） | 0 | 0 |
| [ADR 0031：多文件计划、部分效果与有界结构化 Diff](../adr/0031-patch-batches-and-structured-diff.md) | 架构决策 | 59 | 检测到（待标准化） | 0 | 0 |
| [ADR 0032：整组事务预留与持久审批](../adr/0032-durable-batch-reservation-and-approval.md) | 架构决策 | 40 | 检测到（待标准化） | 0 | 0 |
| [ADR 0033：整组一次性消费、部分效果与只核对恢复](../adr/0033-batch-consumption-and-effect-recovery.md) | 架构决策 | 41 | 检测到（待标准化） | 0 | 0 |
| [ADR 0034：整组调用桥接与 Kernel 接入边界](../adr/0034-batch-call-bridge-and-kernel-integration.md) | 架构决策 | 56 | 检测到（待标准化） | 0 | 0 |
| [ADR 0035：Kernel 整组审批、效果与跨账本恢复](../adr/0035-kernel-batch-approval-and-recovery.md) | 架构决策 | 41 | 检测到（待标准化） | 0 | 0 |
| [ADR 0036：真实整组差异报告与归档准入](../adr/0036-batch-diff-documents-and-artifact-admission.md) | 架构决策 | 42 | 检测到（待标准化） | 0 | 0 |
| [ADR 0037：整组差异报告的事务发布](../adr/0037-batch-diff-transaction-publication.md) | 架构决策 | 25 | 检测到（待标准化） | 0 | 0 |
| [ADR 0038：受信宿主进程生命周期与有界捕获](../adr/0038-host-process-lifecycle.md) | 架构决策 | 44 | 检测到（待标准化） | 0 | 0 |
| [ADR 0039：复用Action Plane的持久命令准入](../adr/0039-process-action-plane-admission.md) | 架构决策 | 40 | 检测到（待标准化） | 0 | 0 |
| [ADR 0040：Agent进程调用的单一审批权威与恢复Saga](../adr/0040-agent-process-action-saga.md) | 架构决策 | 93 | 检测到（待标准化） | 0 | 0 |
| [ADR 0041：Process输出Artifact契约与事务发布](../adr/0041-process-output-artifact.md) | 架构决策 | 84 | 检测到（待标准化） | 0 | 0 |
| [ADR 0042：Process跨库恢复、等待取消与SDK闭环](../adr/0042-process-saga-recovery-and-cancellation.md) | 架构决策 | 119 | 检测到（待标准化） | 0 | 0 |
| [ADR 0043：Git只读反馈与宿主测试Profile](../adr/0043-git-and-controlled-test-feedback.md) | 架构决策 | 207 | 检测到（待标准化） | 0 | 0 |
| [ADR 0044：Coding Eval任务、证据与确定性评分基线](../adr/0044-coding-eval-contract-and-grader.md) | 架构决策 | 130 | 检测到（待标准化） | 0 | 0 |
| [ADR 0045：历史缺陷任务物化与宿主隐藏检查](../adr/0045-historical-eval-materialization-and-checks.md) | 架构决策 | 134 | 检测到（待标准化） | 0 | 0 |
| [ADR 0046：历史任务的Agent Runtime、审批、Worker与评分编排](../adr/0046-historical-eval-runtime-orchestration.md) | 架构决策 | 141 | 检测到（待标准化） | 0 | 0 |
| [ADR 0047：Coding Eval多试验计划与可重算证据](../adr/0047-coding-eval-campaign-evidence.md) | 架构决策 | 66 | 检测到（待标准化） | 0 | 0 |
| [ADR 0048：受控真实Coding Eval Campaign执行](../adr/0048-controlled-real-eval-campaign-execution.md) | 架构决策 | 84 | 检测到（待标准化） | 0 | 0 |
| [ADR 0049：版本化Coding Eval Token预算](../adr/0049-versioned-eval-token-budget.md) | 架构决策 | 78 | 检测到（待标准化） | 0 | 0 |
| [ADR 0050：模型可纠正的工具参数校验反馈](../adr/0050-model-correctable-tool-validation.md) | 架构决策 | 104 | 检测到（待标准化） | 0 | 0 |
| [ADR 0051：版本化公开Coding Eval最终回答契约](../adr/0051-versioned-eval-final-answer-contract.md) | 架构决策 | 43 | 检测到（待标准化） | 0 | 0 |
| [ADR 0052：Coding Eval受控变更包与显式合入](../adr/0052-controlled-eval-change-delivery.md) | 架构决策 | 68 | 检测到（待标准化） | 0 | 0 |
| [ADR 0053：Tool有界并发与统一错误分类](../adr/0053-tool-concurrency-and-error-taxonomy.md) | 架构决策 | 87 | 检测到（待标准化） | 0 | 0 |
| [ADR-0054：版本化 Context 规划、指令优先级与无正文检查记录](../adr/0054-context-planning-and-inspection.md) | 架构决策 | 109 | 检测到（待标准化） | 0 | 0 |
| [ADR 0055：受控项目指令 Source 与持久 freshness](../adr/0055-project-instruction-source-and-freshness.md) | 架构决策 | 113 | 检测到（待标准化） | 0 | 0 |
| [ADR 0056：Workspace、Git、环境Source与乐观一致性](../adr/0056-workspace-git-environment-sources-and-consistency.md) | 架构决策 | 105 | 检测到（待标准化） | 0 | 0 |
| [ADR 0057：Tool Result稳定模型视图与完整Artifact绑定](../adr/0057-tool-result-model-view-and-artifact-binding.md) | 架构决策 | 157 | 检测到（待标准化） | 0 | 0 |
| [ADR 0058：可审计压缩窗口与独立摘要尝试](../adr/0058-compaction-windows-and-accounted-summary-attempts.md) | 架构决策 | 115 | 检测到（待标准化） | 0 | 0 |
| [ADR 0059：独立摘要账本与分用途成本报告](../adr/0059-compaction-attempt-ledger-and-purpose-costs.md) | 架构决策 | 32 | 检测到（待标准化） | 0 | 0 |
| [ADR 0060：Thread生命周期与无授权Fork](../adr/0060-thread-lifecycle-and-authority-free-forks.md) | 架构决策 | 55 | 检测到（待标准化） | 0 | 0 |
| [ADR 0061：终态Turn重试与Provider中立历史](../adr/0061-terminal-turn-retry-and-provider-neutral-history.md) | 架构决策 | 60 | 检测到（待标准化） | 0 | 0 |
| [ADR 0062：Harnessix Code 1.0本地优先商用边界](../adr/0062-local-first-v1-commercial-boundary.md) | 架构决策 | 50 | 检测到（待标准化） | 0 | 0 |
| [ADR 0063：Windows纳入Harnessix Code 1.0支持范围](../adr/0063-windows-v1-platform-support.md) | 架构决策 | 50 | 检测到（待标准化） | 0 | 0 |
| [ADR 0064：AGPL社区版与商业许可证双许可](../adr/0064-agpl-and-commercial-dual-licensing.md) | 架构决策 | 52 | 检测到（待标准化） | 0 | 0 |
| [ADR 0065：平台能力端口与不可变执行计划](../adr/0065-platform-capability-ports-and-execution-plan.md) | 架构决策 | 40 | 检测到（待标准化） | 0 | 0 |
| [ADR 0066：Sandbox、网络与 Secret 失败关闭边界](../adr/0066-sandbox-network-and-secret-boundaries.md) | 架构决策 | 34 | 检测到（待标准化） | 0 | 0 |
| [ADR 0067：跨平台进程所有权与终端生命周期](../adr/0067-process-ownership-and-terminal-lifecycle.md) | 架构决策 | 31 | 检测到（待标准化） | 0 | 0 |
| [ADR 0068：事务性 Workspace 与 Git 交付](../adr/0068-transactional-workspace-and-git-delivery.md) | 架构决策 | 34 | 检测到（待标准化） | 0 | 0 |
| [ADR 0069：统一 Coding Action 风险路由](../adr/0069-unified-coding-action-risk-route.md) | 架构决策 | 47 | 检测到（待标准化） | 0 | 0 |
| [ADR 0070：Agent Protocol v1公共投影、游标与兼容边界](../adr/0070-agent-protocol-v1-boundaries.md) | 架构决策 | 56 | 检测到（待标准化） | 0 | 0 |
| [ADR 0071：Headless App Server与Agent SDK生命周期](../adr/0071-headless-app-server-and-sdk-lifecycle.md) | 架构决策 | 50 | 检测到（待标准化） | 0 | 0 |
| [ADR 0072：持久交互与Pull-Live事件流](../adr/0072-durable-interaction-and-pull-live-stream.md) | 架构决策 | 56 | 检测到（待标准化） | 0 | 0 |
| [ADR 0073：MCP目录绑定与Sandbox执行边界](../adr/0073-mcp-catalog-binding-and-sandbox.md) | 架构决策 | 58 | 检测到（待标准化） | 0 | 0 |
| [ADR 0074：Skill快照与Hook Action安全边界](../adr/0074-skill-snapshot-and-hook-action-boundary.md) | 架构决策 | 66 | 检测到（待标准化） | 0 | 0 |
| [ADR 0075：Provider Profile、Secret 引用与安全 Fallback](../adr/0075-provider-profile-secret-and-safe-fallback.md) | 架构决策 | 155 | 检测到（待标准化） | 0 | 0 |
| [ADR 0076：代码可读性、可维护性与结构治理](../adr/0076-code-readability-and-structural-governance.md) | 架构决策 | 159 | 检测到（待标准化） | 0 | 0 |

### 8.3 源码研究（27份）

| 文档 | 角色 | 行数 | 显式状态 | Mermaid | 代码链接 |
|---|---|---:|---|---:|---:|
| [Agent Loop 研究与 Harnessix 状态机](../research/agent-loop.md) | 源码研究 | 149 | 缺失 | 0 | 0 |
| [Agent Protocol 与产品运行时源码研究](../research/agent-protocol-product-runtime.md) | 源码研究 | 119 | 缺失 | 0 | 0 |
| [0.2 源码研究基线](../research/baselines.md) | 源码研究 | 62 | 检测到（待标准化） | 0 | 0 |
| [Coding Agent代码可读性、可维护性与结构治理研究](../research/code-readability-and-structure.md) | 源码研究 | 174 | 检测到（待标准化） | 0 | 0 |
| [Compaction与长会话窗口源码研究](../research/compaction-and-context-windows.md) | 源码研究 | 79 | 检测到（待标准化） | 0 | 0 |
| [Context Engine 研究与预算模型](../research/context-engine.md) | 源码研究 | 185 | 缺失 | 0 | 0 |
| [Context 规划、指令与预算源码研究](../research/context-planning-and-instructions.md) | 源码研究 | 78 | 缺失 | 0 | 0 |
| [Context Source、项目指令与 Tool Result 模型视图源码研究](../research/context-sources-and-tool-results.md) | 源码研究 | 189 | 缺失 | 0 | 0 |
| [Coding Eval真实Campaign执行与费用边界研究](../research/eval-campaign-execution.md) | 源码研究 | 67 | 缺失 | 0 | 0 |
| [Coding Agent多试验质量与成本证据研究](../research/eval-campaign.md) | 源码研究 | 41 | 缺失 | 0 | 0 |
| [Coding Eval变更交付源码求证与适用性研究](../research/eval-change-delivery.md) | 源码研究 | 97 | 缺失 | 0 | 0 |
| [Coding Eval最终回答契约可见性研究](../research/eval-final-answer-contract-applicability.md) | 源码研究 | 63 | 缺失 | 0 | 0 |
| [Coding Eval Token预算适用性源码研究](../research/eval-token-budget-applicability.md) | 源码研究 | 92 | 缺失 | 0 | 0 |
| [MCP运行时与安全边界源码研究](../research/mcp-runtime-and-security.md) | 源码研究 | 109 | 缺失 | 0 | 0 |
| [Patch：精确计划、写效果和恢复边界](../research/patch-runtime.md) | 源码研究 | 36 | 检测到（待标准化） | 0 | 0 |
| [App Server Protocol 与 Provider Event 研究](../research/protocol.md) | 源码研究 | 204 | 缺失 | 0 | 0 |
| [Provider、Profile、配置与安全 Fallback 源码研究](../research/provider-profile-config-and-safe-fallback.md) | 源码研究 | 123 | 缺失 | 0 | 0 |
| [Permission、Approval 与 Sandbox 研究](../research/security.md) | 源码研究 | 164 | 缺失 | 0 | 0 |
| [Session、Thread、Turn、Item 与事件模型研究](../research/session-model.md) | 源码研究 | 192 | 缺失 | 0 | 0 |
| [Skills、Hooks与供应链边界源码研究](../research/skills-hooks-and-supply-chain.md) | 源码研究 | 83 | 缺失 | 0 | 0 |
| [Thread Resume、Fork 与 Archive 源码研究](../research/thread-lifecycle-and-fork.md) | 源码研究 | 64 | 缺失 | 0 | 0 |
| [Tool Runtime 研究与执行契约](../research/tool-runtime.md) | 源码研究 | 192 | 缺失 | 0 | 0 |
| [Tool 调度与错误分类专项研究](../research/tool-scheduling-and-errors.md) | 源码研究 | 111 | 缺失 | 0 | 0 |
| [Coding Tool参数校验反馈适用性源码研究](../research/tool-validation-feedback-applicability.md) | 源码研究 | 105 | 缺失 | 0 | 0 |
| [0.7 可信执行与工程交付研究](../research/trusted-execution-and-delivery.md) | 源码研究 | 269 | 检测到（待标准化） | 0 | 0 |
| [Turn Retry、Interrupted Recovery 与 Provider 切换源码研究](../research/turn-retry-and-provider-switch.md) | 源码研究 | 73 | 缺失 | 0 | 0 |
| [0.7.5 统一 Action Plane 与扩展边界源码研究](../research/unified-action-plane-and-extension-boundaries.md) | 源码研究 | 185 | 检测到（待标准化） | 0 | 0 |

### 8.4 验证证据（5份）

| 文档 | 角色 | 行数 | 显式状态 | Mermaid | 代码链接 |
|---|---|---:|---|---:|---:|
| [百炼北京受控验证记录（2026-09-03）](../validation/bailian-2026-09-03.md) | 验证证据 | 55 | 缺失 | 0 | 0 |
| [百炼北京Coding Eval任务v2分页纠正后真实基线](../validation/bailian-2026-09-06-coding-eval-v2-corrected/README.md) | 验证证据 | 74 | 缺失 | 0 | 0 |
| [百炼北京Coding Eval任务v2三次真实基线](../validation/bailian-2026-09-06-coding-eval-v2/README.md) | 验证证据 | 81 | 缺失 | 0 | 0 |
| [百炼北京Coding Eval任务v3真实质量基线](../validation/bailian-2026-09-06-coding-eval-v3/README.md) | 验证证据 | 67 | 缺失 | 0 | 0 |
| [百炼北京三次Coding Eval基线验证](../validation/bailian-2026-09-06-coding-eval/README.md) | 验证证据 | 52 | 缺失 | 0 | 0 |


## 9. DOC-1.0验收结论

- 已建立固定提交、逐文档和逐源码包的机器可读起始基线；
- 已明确存量资料的角色、规模、结构和追踪缺口；
- 已确认现有相对链接目标路径无缺失，但外部链接和锚点仍需后续门禁；
- 已将“30个模块设计缺失”“0个源码/测试Markdown链接”“状态61种写法”作为后续量化整改指标；
- 本盘点不宣称存量文档已符合新规范，整改顺序见[文档整改待办](documentation-remediation-backlog.md)。
