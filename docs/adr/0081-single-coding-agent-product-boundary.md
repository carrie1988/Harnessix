---
doc_type: adr
status: current
version: 1
code_revision: 809ed2b1a10f5cb462989a12dddf44f83a9d01ab
owners:
  - core
modules:
  - architecture
  - cli
  - sdk
  - api
  - worker
  - trusted_actions
  - product_config
related_adrs:
  - docs/adr/0005-evolve-to-harnessix-code.md
  - docs/adr/0062-local-first-v1-commercial-boundary.md
  - docs/adr/0069-unified-coding-action-risk-route.md
  - docs/adr/0071-headless-app-server-and-sdk-lifecycle.md
  - docs/adr/0080-capability-proven-product-action-composition.md
related_tests:
  - tests/governance/test_product_runtime_convergence.py
  - tests/smoke/test_cli.py
supersedes: []
---

# ADR 0081：收敛为单一Coding Agent产品边界

## 状态

接受，按0.9.1f分阶段实施。独立Action HTTP/Worker停止作为产品入口；兼容内核只有在既有调用方完成迁移后才能物理删除。

## 背景

Harnessix最初以framework-agnostic Action Plane起步，形成了`ActionService`、HTTP API、数据库队列、
`ActionWorker`、Python HTTP Client和LangChain Tool Adapter。项目演进为Harnessix Code后，又建立了
Agent Protocol、`AgentRuntime`、`TrustedActionGateway`和`TrustedActionRouter`。当前默认产品已经通过
后者执行Workspace Patch，并正在接入固定Container Process。

两条链同时出现在顶层CLI、系统上下文和部署资料中，造成两个入口、两套审批/恢复解释和两种产品定位。
独立HTTP API还缺少最终用户认证、租户授权、限流、网络安全和完整取消语义，而1.0已由
[ADR 0062](0062-local-first-v1-commercial-boundary.md)限定为大量独立本地实例，不包含多租户控制面、
分布式Worker或远程执行池。

## 决策驱动因素

1. 1.0必须能够用一条主链解释用户请求、Agent决策、审批、执行、恢复和最终交付；
2. 高风险执行只能有一个计划和批准权威，不能由Agent Session与独立Action服务分别决定；
3. 本地优先产品不应承担未进入1.0范围的HTTP认证、多租户和分布式队列运维成本；
4. Policy、Approval、持久效果、`UNKNOWN`和Reconcile是产品差异化能力，不能随部署入口一起删除；
5. 已发布但未进入默认产品的旧接口需要明确迁移边界，不能以批量硬删破坏Process、Git Push和Eval；
6. 未来远程执行必须复用同一Trusted Action合同，而不是重新开放绕过Agent Session的公共入口。

## 候选方案

| 方案 | 满足的驱动因素 | 不满足的驱动因素 | 成本/风险 |
|---|---|---|---|
| A. 继续并列维护Coding Agent和Action服务 | 保留全部历史接口 | 产品定位、授权权威和1.0范围不收敛 | 双倍协议、部署、安全和测试成本继续增长 |
| B. 只从架构图隐藏，代码与命令不变 | 文档表面简化 | 用户仍能进入第二条产品链，事实与文档不一致 | 形成误导性架构资料 |
| C. 撤销独立产品面，保留兼容内核并分步迁移 | 立即形成单一产品入口，同时保护既有恢复语义 | 需要一段受控兼容窗口 | 迁移期仍需维护精确白名单和旧测试 |
| D. 立即删除全部Action领域与Journal | 文件数量下降最快 | 删除可信副作用治理差异化，并破坏既有调用方 | 恢复、安全和数据兼容风险不可接受 |

## 决策

选择方案C，并遵守以下不变量：

1. Harnessix Code 1.0唯一产品入口是`harnessix code`、薄`harnessix agent`和内部
   `harnessix agent-server`；客户端统一使用Agent Protocol；
2. 顶层CLI不再提供`harnessix serve`和`harnessix worker`，根包和`harnessix.sdk`不再导出Action HTTP Client；
3. 高风险能力统一经`TrustedActionGateway → TrustedActionRouter`完成计划、Policy、审批、执行和对账；
4. `ActionService`、`ActionWorker`、旧Effect Journal和专用Process Bridge在迁移期属于兼容内核，不是产品能力，
   不允许新增生产调用方；
5. 先完成固定Container Process、Git Push和历史Eval迁移，再删除HTTP API、HTTP Client、LangChain Adapter、
   PostgreSQL Worker Queue、旧Bootstrap和兼容内核；
6. 旧数据库不得由新版本自动删除。物理移除前必须提供只读检查、导出或明确的归档说明；
7. 1.x若需要远程执行，只能在`TrustedActionExecutor`后增加经过身份认证的Remote Executor Adapter，
   不能恢复独立于Thread/Turn的公共Action入口。

## 理由

Trusted Action已经是默认产品实际使用的安全边界，并由ADR 0069和ADR 0080规定为统一风险路由和唯一执行批准权威。
独立HTTP/Worker的价值主要是通用框架接入和多进程队列，这两项不属于本地优先1.0范围。把治理能力内聚到Agent，
既保留Action Plane最有价值的失败语义，又消除双入口、双SDK和双部署拓扑。

兼容内核分阶段删除优于一次硬删：当前历史Eval仍通过`ActionWorker`驱动受控测试，Git Push仍把统一Route投影到旧
Effect Journal，旧Process桥也承担历史Session恢复。先建立替代路径和回归证据，才能保证收敛不是功能倒退。

## 后果

### 正面后果

- 产品架构、CLI、SDK和部署资料围绕同一Coding Agent主链；
- Agent Session和Trusted Action Router的职责可以清晰解释；
- HTTP认证、通用Action OpenAPI和分布式Worker不再阻塞1.0；
- Policy、Approval、Audit、`UNKNOWN`和Reconcile继续作为内部可信执行能力；
- 后续删除FastAPI、Uvicorn、AsyncPG和LangChain Core具备可验证前置条件。

### 负面后果与债务

- 旧Action HTTP API、Python Client和LangChain Adapter不再作为1.0兼容承诺；
- 迁移窗口内仓库仍存在旧Action合同和Worker实现，必须通过治理测试防止扩散；
- 历史文档需要标记为历史或兼容资料，当前运维文档必须删除旧启动方式；
- Process、Git Push和Eval迁移会触及事件兼容、恢复和故障注入测试，不能只做机械替换。

## 兼容、安全与运维影响

- 本项目尚未发布1.0，允许在0.9阶段撤销早期实验性命令和根包导出；
- 已存在的Agent Protocol v1、Session事件和产品配置保持兼容；
- 移除公共HTTP入口减少未认证Principal、开放OpenAPI和网络监听攻击面；
- 兼容内核不得从默认产品组合根、TUI、Agent SDK或公开运维命令到达；
- 回滚只恢复旧版本二进制，不修改旧数据库；新版本不会自动消费或删除旧Action Queue；
- 远程、多租户和分布式执行继续由1.x单独立项，并需要身份、配额、Secret和服务SLO设计。

## 验证方式

1. CLI帮助和解析测试证明`serve/worker`不再存在，`code/agent/agent-server`保持可发现；
2. 公共导出测试证明根包和`harnessix.sdk`只暴露Agent SDK，不暴露Action HTTP Client；
3. 依赖治理测试冻结旧`ActionService/ActionWorker`生产调用方白名单，任何新增引用均失败；
4. 默认产品装配测试证明`run_product_stdio`只注入`TrustedActionGateway`；
5. 后续迁移逐项覆盖正常、拒绝、取消、超时、崩溃、`UNKNOWN`和Reconcile；
6. 文档门禁证明总体架构、产品章程、路线图、部署和配置资料不再声明独立服务为当前产品。

## 关联资料

| 类型 | 路径/链接 | 关系 |
|---|---|---|
| 重大变更设计 | [0.9.1f单一产品运行时收敛](../changes/m09-1f-single-product-runtime-convergence.md) | 分阶段代码、数据和文档迁移 |
| 现行系统架构 | [总体架构](../architecture.md) | 单一产品主链事实源 |
| 现行可信执行设计 | [Trusted Actions模块设计](../modules/trusted-actions.md) | 保留的统一执行边界 |
| 产品边界 | [ADR 0062](0062-local-first-v1-commercial-boundary.md) | 本地优先1.0范围 |
| 产品组合 | [ADR 0080](0080-capability-proven-product-action-composition.md) | Router唯一权威与默认装配 |
| 治理测试 | [产品运行时收敛测试](../../tests/governance/test_product_runtime_convergence.py) | 防止双入口和旧依赖扩散 |

## 被取代关系

本ADR不否定ADR 0002的`UNKNOWN`语义，也不删除ADR 0003记录的历史Worker Queue设计。ADR 0005中“Action Plane作为
执行治理子系统”的方向保持有效，但“子系统”此后表示Coding Agent内部Trusted Action Runtime，不表示独立HTTP/Worker产品。
