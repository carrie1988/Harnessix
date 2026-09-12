---
doc_type: governance-index
status: current
version: 1
code_revision: 3f75747f21dae9bb5c52d62d52a7d10815122f17
owners:
  - core
modules:
  - documentation
  - architecture
related_adrs: []
related_tests: []
supersedes: []
---

# Harnessix Code 架构决策记录索引

## 1. 文档定位

本目录保存长期架构决策的背景、候选方案、选择和后果。ADR回答“为什么这样选择”，不替代当前模块设计、
外部契约或运维手册。判断当前源码行为时，应从本页“当前事实源”进入对应模块设计，再用ADR追溯决策原因。

截至标注代码版本，共有77份编号ADR。DOC-1.5迁移保持前76份决策正文不变，只增加标准元数据、可解析状态、
迁移前最近维护的代码版本和源码测试责任域；ADR 0077在DOC-1.6设计阶段新增。

## 2. 状态语义

| YAML状态 | ADR语义 | 使用规则 |
|---|---|---|
| `current` | 决策已接受且没有被后续ADR整体取代 | 可作为设计理由；当前行为仍以模块设计和契约为准 |
| `reviewing` | 提议仍在评审 | 不得作为已实现能力依据 |
| `superseded` | 后续ADR明确整体取代本决策 | 保留历史并双向登记取代关系 |
| `deprecated` | 决策保留兼容背景但禁止新实现采用 | 新增实现不得继续依赖 |

当前77份ADR均属于已接受决策，没有发现被后续ADR**整体**取代的记录。ADR 0005的macOS/Linux平台条款由
ADR 0063扩展到Windows，但其本地优先产品方向仍有效，因此两者均保持`current`；后续若发生整体取代，必须
同时更新旧ADR的YAML状态、新ADR的`supersedes`和本索引。

## 3. 决策到实现的阅读路径

```mermaid
flowchart LR
    Need[需求与约束] --> Research[冻结源码研究]
    Research --> ADR[ADR决策理由]
    ADR --> Change[历史变更设计]
    Change --> Module[现行模块设计]
    Module --> Source[源码与合同]
    Source --> Test[测试与验证证据]
```

ADR中的版本号、阶段状态和测试数字只对应当时决策背景。现行结构、失败语义、未实现范围和测试入口以
[文档中心](../README.md)、[总体架构](../architecture.md)与[追踪矩阵](../governance/documentation-traceability.md)为准。

## 4. 决策目录

### 4.1 基础架构与产品方向

当前事实源：[产品章程](../product-charter.md)、[总体架构](../architecture.md)、[自研与复用边界](../build-vs-buy.md)。

| 编号 | 决策 | 状态说明 |
|---|---|---|
| 0001 | [采用 Python-first Runtime](0001-python-first-runtime.md) | 接受 |
| 0002 | [将 `UNKNOWN` 作为一等结果](0002-unknown-first-class.md) | 接受 |
| 0003 | [使用 Journal 状态实现持久化 Worker Queue](0003-database-backed-worker-queue.md) | 接受 |
| 0004 | [在 Action 快照中持久化 W3C Trace Context](0004-durable-trace-context.md) | 接受 |
| 0005 | [将 Harnessix 演进为 Harnessix Code](0005-evolve-to-harnessix-code.md) | 接受；平台范围由ADR 0063扩展 |

### 4.2 Agent Kernel、Session与公共边界

当前事实源：[Agent](../modules/agent.md)、[Session](../modules/session.md)、[Protocol](../modules/protocol.md)、[Observability](../modules/observability.md)。

| 编号 | 决策 | 状态说明 |
|---|---|---|
| 0006 | [采用 Thread、Turn、Item 与 AgentEvent 领域模型](0006-thread-turn-item-event-model.md) | 接受 |
| 0007 | [采用持久边界驱动的 Agent Loop 与分层取消](0007-agent-loop-and-cancellation.md) | 接受 |
| 0008 | [采用模型无关的流式 Provider Event](0008-provider-event-model.md) | 接受 |
| 0009 | [App Server v1 使用标准 JSON-RPC 2.0 与 stdio JSONL](0009-app-server-protocol.md) | 接受 |
| 0010 | [SQLite Event Log、事务投影与保守恢复](0010-session-store-and-recovery.md) | 接受 |
| 0011 | [0.3 Kernel 使用进程内宿主与初始聚合投影](0011-kernel-host-and-initial-projection.md) | 接受 |
| 0012 | [持久审批检查点与显式继续](0012-durable-approval-checkpoint.md) | 接受 |
| 0013 | [Kernel 语义契约、诊断与存储验收](0013-kernel-contracts-and-telemetry.md) | 接受 |

### 4.3 Model Runtime、用量、成本与Smoke

当前事实源：[Models](../modules/models.md)、[Smoke](../modules/smoke.md)、[Session](../modules/session.md)。

| 编号 | 决策 | 状态说明 |
|---|---|---|
| 0014 | [首个 OpenAI-compatible Provider](0014-openai-compatible-provider.md) | 接受 |
| 0015 | [第二类 Provider 与用量演进边界](0015-anthropic-provider.md) | 接受 |
| 0016 | [模型尝试账本与累计用量事实](0016-model-attempt-ledger.md) | 接受 |
| 0017 | [实际 SDK 的尝试与用量映射](0017-provider-attempt-usage.md) | 接受 |
| 0018 | [版本化 Token 价格与可重算成本报告](0018-versioned-token-cost.md) | 接受 |
| 0019 | [受控模型 Smoke 与白名单诊断](0019-controlled-model-smoke.md) | 接受 |
| 0020 | [响应计费元数据与尝试绑定](0020-observed-billing-context.md) | 接受 |
| 0021 | [并发 Session 初始化的 WAL 切换边界](0021-session-wal-initialization.md) | 接受 |
| 0022 | [百炼单次计价验收的证据边界](0022-bailian-price-validation.md) | 接受；真实计价证据仍待路线图0.9.6关闭 |

### 4.4 Coding Tool、Patch、Process与效果恢复

当前事实源：[Tools](../modules/tools.md)、[Artifacts](../modules/artifacts.md)、[Patches](../modules/patches.md)、[Processes](../modules/processes.md)。

| 编号 | 决策 | 状态说明 |
|---|---|---|
| 0023 | [工作区绑定与首批只读编码工具](0023-workspace-read-tools.md) | 接受 |
| 0024 | [有界搜索与 Artifact 归属拆片](0024-bounded-search-and-artifact-scope.md) | 接受 |
| 0025 | [可信工具执行作用域与旧端口兼容](0025-trusted-tool-execution-scope.md) | 接受 |
| 0026 | [同一 Session 事务发布有界 Artifact](0026-transactional-artifacts.md) | 接受 |
| 0027 | [先准备精确 Patch，再开放受控写入](0027-prepared-patch-and-write-admission.md) | 接受 |
| 0028 | [受管副本内的持久单文件 Patch 执行](0028-managed-patch-execution.md) | 接受 |
| 0029 | [受管 Patch 的调用绑定与 Agent 接入顺序](0029-managed-patch-agent-bridge.md) | 接受 |
| 0030 | [Kernel 写准入、持久审批与双账本恢复](0030-kernel-managed-patch-admission.md) | 接受 |
| 0031 | [多文件计划、部分效果与有界结构化 Diff](0031-patch-batches-and-structured-diff.md) | 接受 |
| 0032 | [整组事务预留与持久审批](0032-durable-batch-reservation-and-approval.md) | 接受 |
| 0033 | [整组一次性消费、部分效果与只核对恢复](0033-batch-consumption-and-effect-recovery.md) | 接受 |
| 0034 | [整组调用桥接与 Kernel 接入边界](0034-batch-call-bridge-and-kernel-integration.md) | 接受 |
| 0035 | [Kernel 整组审批、效果与跨账本恢复](0035-kernel-batch-approval-and-recovery.md) | 接受 |
| 0036 | [真实整组差异报告与归档准入](0036-batch-diff-documents-and-artifact-admission.md) | 接受 |
| 0037 | [整组差异报告的事务发布](0037-batch-diff-transaction-publication.md) | 接受 |
| 0038 | [受信宿主进程生命周期与有界捕获](0038-host-process-lifecycle.md) | 接受 |
| 0039 | [复用Action Plane的持久命令准入](0039-process-action-plane-admission.md) | 接受 |
| 0040 | [Agent进程调用的单一审批权威与恢复Saga](0040-agent-process-action-saga.md) | 接受 |
| 0041 | [Process输出Artifact契约与事务发布](0041-process-output-artifact.md) | 接受 |
| 0042 | [Process跨库恢复、等待取消与SDK闭环](0042-process-saga-recovery-and-cancellation.md) | 接受 |
| 0043 | [Git只读反馈与宿主测试Profile](0043-git-and-controlled-test-feedback.md) | 接受 |

### 4.5 Coding Eval、真实Campaign与交付

当前事实源：[Evals](../modules/evals.md)、[Delivery](../modules/delivery.md)、[测试与Eval规范](../testing-and-evals.md)。

| 编号 | 决策 | 状态说明 |
|---|---|---|
| 0044 | [Coding Eval任务、证据与确定性评分基线](0044-coding-eval-contract-and-grader.md) | 接受 |
| 0045 | [历史缺陷任务物化与宿主隐藏检查](0045-historical-eval-materialization-and-checks.md) | 接受 |
| 0046 | [历史任务的Agent Runtime、审批、Worker与评分编排](0046-historical-eval-runtime-orchestration.md) | 接受 |
| 0047 | [Coding Eval多试验计划与可重算证据](0047-coding-eval-campaign-evidence.md) | 接受 |
| 0048 | [受控真实Coding Eval Campaign执行](0048-controlled-real-eval-campaign-execution.md) | 接受 |
| 0049 | [版本化Coding Eval Token预算](0049-versioned-eval-token-budget.md) | 接受 |
| 0050 | [模型可纠正的工具参数校验反馈](0050-model-correctable-tool-validation.md) | 接受 |
| 0051 | [版本化公开Coding Eval最终回答契约](0051-versioned-eval-final-answer-contract.md) | 接受 |
| 0052 | [Coding Eval受控变更包与显式合入](0052-controlled-eval-change-delivery.md) | 接受 |
| 0053 | [Tool有界并发与统一错误分类](0053-tool-concurrency-and-error-taxonomy.md) | 接受 |

### 4.6 Context、Compaction与会话生命周期

当前事实源：[Context](../modules/context.md)、[Agent](../modules/agent.md)、[Session](../modules/session.md)、[Models](../modules/models.md)。

| 编号 | 决策 | 状态说明 |
|---|---|---|
| 0054 | [版本化 Context 规划、指令优先级与无正文检查记录](0054-context-planning-and-inspection.md) | 接受 |
| 0055 | [受控项目指令 Source 与持久 freshness](0055-project-instruction-source-and-freshness.md) | 接受 |
| 0056 | [Workspace、Git、环境Source与乐观一致性](0056-workspace-git-environment-sources-and-consistency.md) | 接受 |
| 0057 | [Tool Result稳定模型视图与完整Artifact绑定](0057-tool-result-model-view-and-artifact-binding.md) | 接受 |
| 0058 | [可审计压缩窗口与独立摘要尝试](0058-compaction-windows-and-accounted-summary-attempts.md) | 接受 |
| 0059 | [独立摘要账本与分用途成本报告](0059-compaction-attempt-ledger-and-purpose-costs.md) | 接受；作为ADR 0058的账本与成本子决策 |
| 0060 | [Thread生命周期与无授权Fork](0060-thread-lifecycle-and-authority-free-forks.md) | 接受 |
| 0061 | [终态Turn重试与Provider中立历史](0061-terminal-turn-retry-and-provider-neutral-history.md) | 接受 |

### 4.7 1.0边界、平台、许可与可信执行

当前事实源：[产品章程](../product-charter.md)、[Execution](../modules/execution.md)、[Sandbox](../modules/sandbox.md)、[Trusted Actions](../modules/trusted-actions.md)、[Delivery](../modules/delivery.md)。

| 编号 | 决策 | 状态说明 |
|---|---|---|
| 0062 | [Harnessix Code 1.0本地优先商用边界](0062-local-first-v1-commercial-boundary.md) | 接受 |
| 0063 | [Windows纳入Harnessix Code 1.0支持范围](0063-windows-v1-platform-support.md) | 接受 |
| 0064 | [AGPL社区版与商业许可证双许可](0064-agpl-and-commercial-dual-licensing.md) | 接受 |
| 0065 | [平台能力端口与不可变执行计划](0065-platform-capability-ports-and-execution-plan.md) | 接受 |
| 0066 | [Sandbox、网络与 Secret 失败关闭边界](0066-sandbox-network-and-secret-boundaries.md) | 接受 |
| 0067 | [跨平台进程所有权与终端生命周期](0067-process-ownership-and-terminal-lifecycle.md) | 接受 |
| 0068 | [事务性 Workspace 与 Git 交付](0068-transactional-workspace-and-git-delivery.md) | 接受 |
| 0069 | [统一 Coding Action 风险路由](0069-unified-coding-action-risk-route.md) | 接受 |

### 4.8 Agent Protocol、产品运行时与扩展

当前事实源：[Protocol](../modules/protocol.md)、[App Server](../modules/app-server.md)、[MCP](../modules/mcp.md)、[Skills](../modules/skills.md)、[Hooks](../modules/hooks.md)、[Product Config](../modules/product-config.md)。

| 编号 | 决策 | 状态说明 |
|---|---|---|
| 0070 | [Agent Protocol v1公共投影、游标与兼容边界](0070-agent-protocol-v1-boundaries.md) | 接受 |
| 0071 | [Headless App Server与Agent SDK生命周期](0071-headless-app-server-and-sdk-lifecycle.md) | 接受 |
| 0072 | [持久交互与Pull-Live事件流](0072-durable-interaction-and-pull-live-stream.md) | 接受 |
| 0073 | [MCP目录绑定与Sandbox执行边界](0073-mcp-catalog-binding-and-sandbox.md) | 接受 |
| 0074 | [Skill快照与Hook Action安全边界](0074-skill-snapshot-and-hook-action-boundary.md) | 接受 |
| 0075 | [Provider Profile、Secret 引用与安全 Fallback](0075-provider-profile-secret-and-safe-fallback.md) | 接受 |

### 4.9 可读性与结构治理

当前事实源：[0.9.0历史设计](../m09-code-maintainability.md)、[治理规范](../governance/README.md)。

| 编号 | 决策 | 状态说明 |
|---|---|---|
| 0076 | [代码可读性、可维护性与结构治理](0076-code-readability-and-structural-governance.md) | 接受 |
| 0077 | [版本化文档合同与分层阻断门禁](0077-versioned-documentation-contract-and-gates.md) | 接受；DOC-1.6实施中 |

## 5. 维护规则

1. 新ADR使用下一个连续编号和[ADR模板](../governance/templates/adr-template.md)，在评审期间标记`reviewing`；
2. 接受决策时必须链接对应源码研究、重大变更设计、现行模块和验证入口；
3. ADR正文只追加结论、后果或取代关系，不静默改写原决策来匹配新实现；
4. 局部扩展不等于整体取代；只有核心决策不再适用时才使用`superseded`；
5. 模块源码变化不能只更新ADR，必须同步相应现行模块设计；
6. ADR中的CI编号和测试数量是历史证据，当前发布判定必须重新执行当前质量门。

## 6. 已知治理边界

本次状态迁移没有把早期ADR机械扩写成现行详细设计。部分早期正文只具备背景、决策和结果三个章节，
其当前实现细节已由30份模块设计和Action Plane子系统设计承接。结构补写仅在决策本身发生复审或取代时进行，
避免通过批量改写破坏历史真实性。
