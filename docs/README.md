---
doc_type: governance-index
status: current
version: 4
code_revision: 6c5f310346afa3fa176f51707722467f46811b35
owners:
  - core
modules:
  - documentation
related_adrs: []
related_tests: []
supersedes: []
---

# Harnessix Code 文档中心

## 1. 文档用途

本页是Harnessix Code正式资料的统一入口。文档按“当前事实、历史决策、研究证据、验证证据”分层，避免读者通过里程碑历史拼接当前实现。

当前产品实现已经完成路线图0.1～0.9.0范围，但仍不是1.0正式商用版本。完整TUI、Windows产品级Coding Tool Runtime、三平台发行物、固定Eval/Soak阈值、安全供应链和Dogfooding仍属于0.9后续工作。当前能力和规划能力以[总体架构](architecture.md)及[路线图](roadmap.md)为准。

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
| 文件修改和执行 | [源码阅读地图：可信执行](guides/source-reading-map.md#8-可信写入进程与交付主链) | `patches/processes → sandbox/workspace → delivery` |
| Action Plane | [Action Plane子系统设计](subsystems/action-plane.md) | `domain → policy → runtime → storage/worker` |
| MCP、Skill与Hook | [0.8产品运行时设计](m08-product-runtime-and-extensions.md) | `mcp/skills/hooks → trusted_actions` |
| Eval与发布证据 | [测试与Eval规范](testing-and-evals.md) | `evals → validation` |

30个生产源码包、10个根级生产模块、当前相关资料和测试入口见[文档—源码—测试追踪矩阵](governance/documentation-traceability.md)。

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
| Action Plane | [Action Plane子系统设计](subsystems/action-plane.md) | Policy、Approval、Journal、Lease、`UNKNOWN`与Reconcile |
| 外部Action契约 | [Action Contract](action-contract.md) | 请求、工具定义、指纹和Trace Context |
| Action状态与恢复 | [Action生命周期](action-lifecycle.md) | 状态转换、租约、`UNKNOWN`和对账 |
| 安全模型 | [威胁模型](threat-model.md) | 资产、信任边界、攻击面和缓解措施 |
| 部署与升级 | [部署与运行](deployment.md) | 当前命令、配置、存储迁移和运行边界 |
| 测试与Eval | [测试与Eval规范](testing-and-evals.md) | 测试分层、故障注入、Eval和通过证据 |
| 现行模块设计 | [追踪矩阵](governance/documentation-traceability.md) | DOC-1迁移期间的模块覆盖状态和目标路径 |

里程碑文档描述某个版本切片的交付增量，不再作为单个模块当前实现的唯一事实源。

## 4. 里程碑设计

| 版本 | 设计资料 | 主要能力 |
|---|---|---|
| 0.1/M1 | [Action Plane Worker与PostgreSQL](m1-worker-postgresql.md)、[可观测性](m1-observability.md) | Action Contract、Policy、Approval、Journal、Worker |
| 0.3 | [Agent Runtime Kernel](m03-runtime-kernel.md) | Thread/Turn/Item/Event、Agent Loop、Session与恢复 |
| 0.4 | [Model Runtime](m04-model-runtime.md) | Provider事件、尝试/用量/成本、Smoke |
| 0.5 | [Coding Tool Runtime](m05-coding-tools.md) | 读取、Artifact、Patch、Process、Git、Eval与交付 |
| 0.6 | [Context与持久会话](m06-context-and-sessions.md) | Context Source、压缩、Thread生命周期和Retry |
| 0.7 | [可信执行与工程交付](m07-trusted-execution-and-delivery.md) | 跨平台端口、Sandbox、Secret、Process、Workspace与Delivery |
| 0.8 | [产品运行时与扩展](m08-product-runtime-and-extensions.md) | Protocol、App Server、SDK、MCP、Skill、Hook和Provider配置 |
| 0.9.0 | [代码可维护性治理](m09-code-maintainability.md) | 代码说明、职责拆分、复杂度与依赖基线 |

专题详细设计包括[Compaction窗口](compaction-runtime-and-windows.md)、[摘要尝试账本](compaction-attempt-ledger.md)、[Thread生命周期](thread-lifecycle.md)和[Turn Retry/Provider切换](turn-retry-and-provider-switch.md)。

### 4.1 根目录聚合文档角色

| 文档 | 角色 | 是否维护当前事实 | 使用限制 |
|---|---|---|---|
| [总体架构](architecture.md) | 系统级现行设计 | 是 | 维护组件、主链、状态、数据、安全和部署边界，不展开每个类的全部细节 |
| [路线图](roadmap.md) | 规划与完成证据索引 | 是 | 只管理范围、依赖、状态和验收，不充当模块设计 |
| [产品章程](product-charter.md) | 产品目标与发布边界 | 是 | 不描述易变化的实现细节 |
| [Action Contract](action-contract.md) | 稳定外部契约 | 是 | Action Plane契约变化必须同步实现和合同测试 |
| [Action生命周期](action-lifecycle.md) | Action状态与恢复契约 | 是 | 不替代Agent Turn生命周期设计 |
| [威胁模型](threat-model.md) | 统一风险登记 | 是 | 模块缓解措施应链接到现行模块设计和测试 |
| [测试与Eval规范](testing-and-evals.md) | 质量规范聚合入口 | 是 | 具体模块测试事实逐步迁移到模块设计 |
| [部署与运行](deployment.md) | 运维聚合入口 | 是 | 安装、升级、恢复和平台资料将在DOC-1.5拆分 |
| `m03`～`m09`里程碑文档 | 历史增量设计 | 否 | 用于解释某阶段交付，不作为当前模块事实的唯一来源 |
| [ADR目录](adr/) | 决策历史 | 否 | 解释为什么选择，不重复当前实现全文 |
| [研究目录](research/) | 参考源码证据 | 否 | 不能直接成为Harnessix的产品契约 |
| [验证目录](validation/) | 特定版本证据 | 否 | 结论不得外推到未记录的平台、Provider或输入 |

## 5. 架构决策和源码研究

- [ADR目录](adr/)：记录长期决策的背景、候选方案、选择和后果；
- [源码研究计划](research-plan.md)：定义参考版本、研究问题和clean-room边界；
- [源码研究目录](research/)：Codex、OpenCode、Claude Code等参考实现的可核验证据；
- [自研与复用边界](build-vs-buy.md)：第三方依赖、许可证和自研边界。

ADR回答“为什么这样选择”，源码研究回答“参考实现有什么证据”，二者都不替代当前模块设计。

## 6. 测试、部署和验证证据

- [测试与Eval规范](testing-and-evals.md)；
- [受控模型Smoke指南](model-smoke.md)；
- [部署与运行](deployment.md)；
- [验证证据目录](validation/)；
- [威胁模型](threat-model.md)。

验证证据绑定特定代码、环境和输入，不应被解释为所有模型、平台或用户任务均已通过。

## 7. 文档治理

- [治理入口](governance/README.md)；
- [文档工程规范](governance/documentation-standard.md)；
- [DOC-1.0全量盘点](governance/documentation-inventory.md)；
- [文档—源码—测试追踪矩阵](governance/documentation-traceability.md)；
- [整改待办](governance/documentation-remediation-backlog.md)；
- [DOC-1.0机器基线](baselines/documentation-doc1.0-start.json)。

DOC-1.1已建立本导航、总体架构和源码阅读主链；DOC-1.2已完成Agent Runtime与Action Plane两份黄金样例。其余独立模块设计按DOC-1.3和DOC-1.4继续迁移。

## 8. 文档状态说明

新增或重写的正式文档使用以下状态：

- `draft`：未完成评审；
- `reviewing`：正在评审；
- `current`：与标注代码版本一致；
- `historical`：只描述历史增量；
- `superseded`：已被明确取代；
- `deprecated`：仍保留兼容背景但不应继续采用。

存量文档正在按DOC-1渐进迁移；未带标准YAML元数据的文档不应仅凭“完成”字样判断为当前事实。
