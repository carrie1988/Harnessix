---
doc_type: governance-index
status: current
version: 4
code_revision: c3ed6c917368f705bab90a85ea82572520909ac5
owners:
  - core
modules:
  - documentation
  - research
related_adrs:
  - docs/adr/0078-product-shell-and-recoverable-client-state.md
  - docs/adr/0079-preflight-and-native-read-port.md
  - docs/adr/0080-capability-proven-product-action-composition.md
related_tests: []
supersedes: []
---

# Harnessix Code 源码研究索引

## 1. 文档定位

本目录保存Codex、OpenCode、Claude Code逆向样本及相关协议/SDK的冻结源码研究。研究资料只提供可核验的
参考事实、行为证据、推断和工程取舍，不直接定义Harnessix公共契约。Harnessix采用或拒绝某种机制的决定必须进入
[ADR索引](../adr/README.md)，当前实现必须进入[模块设计](../README.md#3-当前事实源)。

截至标注代码版本，本目录包含30份冻结研究资料和本索引。每份资料均绑定创建时的Harnessix代码提交，并在正文顶部
登记访问日期；参考仓库提交、产品版本、证据路径和适用范围由正文或[统一研究基线](baselines.md)固定。

## 2. 证据边界

| 类型 | 含义 | 能否直接成为Harnessix契约 |
|---|---|---|
| 源码事实 | 可从固定提交、协议版本或SDK代码直接复核 | 否 |
| 行为验证 | 在明确环境和输入下观察到的结果 | 否 |
| 推断 | 由多处调用关系或外部行为归纳 | 否，必须再次求证 |
| 独立决策 | Harnessix基于约束做出的取舍 | 只有进入ADR/正式设计后才生效 |

Claude Code逆向仓库不是官方源码，只作为交叉佐证；安全、协议和持久化结论不得仅依赖该来源。研究不复制参考实现代码，
不把参考仓库加入Harnessix构建依赖，也不以相似命名证明行为等价。

## 3. 研究到实现的证据流

```mermaid
flowchart LR
    Version[固定仓库提交或协议版本] --> Evidence[文件、符号与行为证据]
    Evidence --> Finding[事实/推断/限制]
    Finding --> ADR[Harnessix ADR]
    ADR --> Design[变更设计与现行模块设计]
    Design --> Test[合同、故障与真实验证]
```

## 4. 研究目录

### 4.1 统一基线

| 主题 | 冻结访问日期 | 参考版本 | 采用结果/当前入口 |
|---|---|---|---|
| [统一基线与证据等级](baselines.md) | 2026-09-02 | 0.2冻结基线 | [研究计划](../research-plan.md) |

### 4.2 基础运行机制

| 主题 | 冻结访问日期 | 参考版本 | 采用结果/当前入口 |
|---|---|---|---|
| [Agent Loop与状态机](agent-loop.md) | 2026-09-02 | 统一0.2基线 | [ADR 0007](../adr/0007-agent-loop-and-cancellation.md)、[Agent模块](../modules/agent.md) |
| [Session、Thread、Turn与事件模型](session-model.md) | 2026-09-02 | 统一0.2基线 | [ADR 0006](../adr/0006-thread-turn-item-event-model.md)、[Session模块](../modules/session.md) |
| [Provider Event与App Server Protocol](protocol.md) | 2026-09-02 | 统一0.2基线 | [ADR 0008](../adr/0008-provider-event-model.md)、[ADR 0009](../adr/0009-app-server-protocol.md) |
| [Context预算模型](context-engine.md) | 2026-09-02 | 统一0.2基线 | [ADR 0054](../adr/0054-context-planning-and-inspection.md)、[Context模块](../modules/context.md) |
| [Tool Runtime与执行契约](tool-runtime.md) | 2026-09-03 | 统一0.2基线 | [Tools模块](../modules/tools.md)、[ADR 0053](../adr/0053-tool-concurrency-and-error-taxonomy.md) |
| [Permission、Approval与Sandbox](security.md) | 2026-09-08 | 统一0.2基线 | [Sandbox模块](../modules/sandbox.md)、[Policy模块](../modules/policy.md) |

### 4.3 Coding Tool与Eval

| 主题 | 冻结访问日期 | 参考版本 | 采用结果/当前入口 |
|---|---|---|---|
| [Patch计划、效果与恢复](patch-runtime.md) | 2026-09-03 | 正文专项基线 | [ADR 0027](../adr/0027-prepared-patch-and-write-admission.md)、[Patches模块](../modules/patches.md) |
| [Tool调度与错误分类](tool-scheduling-and-errors.md) | 2026-09-07 | 正文专项基线 | [ADR 0053](../adr/0053-tool-concurrency-and-error-taxonomy.md) |
| [工具参数校验反馈适用性](tool-validation-feedback-applicability.md) | 2026-09-06 | 正文专项基线 | [ADR 0050](../adr/0050-model-correctable-tool-validation.md) |
| [多试验质量与成本证据](eval-campaign.md) | 2026-09-06 | 正文专项基线 | [ADR 0047](../adr/0047-coding-eval-campaign-evidence.md) |
| [真实Campaign执行与费用边界](eval-campaign-execution.md) | 2026-09-06 | 正文专项基线 | [ADR 0048](../adr/0048-controlled-real-eval-campaign-execution.md) |
| [Eval Token预算适用性](eval-token-budget-applicability.md) | 2026-09-06 | 正文专项基线 | [ADR 0049](../adr/0049-versioned-eval-token-budget.md) |
| [Eval最终回答契约可见性](eval-final-answer-contract-applicability.md) | 2026-09-06 | 正文专项基线 | [ADR 0051](../adr/0051-versioned-eval-final-answer-contract.md) |
| [Eval变更交付适用性](eval-change-delivery.md) | 2026-09-07 | 正文专项基线 | [ADR 0052](../adr/0052-controlled-eval-change-delivery.md)、[Delivery模块](../modules/delivery.md) |

### 4.4 Context与持久会话

| 主题 | 冻结访问日期 | 参考版本 | 采用结果/当前入口 |
|---|---|---|---|
| [Context规划、指令与预算](context-planning-and-instructions.md) | 2026-09-07 | 正文专项基线 | [ADR 0054](../adr/0054-context-planning-and-inspection.md) |
| [Context Source与Tool Result视图](context-sources-and-tool-results.md) | 2026-09-08 | 正文专项基线 | [ADR 0055～0057](../adr/0055-project-instruction-source-and-freshness.md) |
| [Compaction与长会话窗口](compaction-and-context-windows.md) | 2026-09-08 | 正文专项基线 | [ADR 0058](../adr/0058-compaction-windows-and-accounted-summary-attempts.md)、[Context模块](../modules/context.md) |
| [Thread Resume、Fork与Archive](thread-lifecycle-and-fork.md) | 2026-09-08 | 正文专项基线 | [ADR 0060](../adr/0060-thread-lifecycle-and-authority-free-forks.md) |
| [Turn Retry与Provider切换](turn-retry-and-provider-switch.md) | 2026-09-08 | 正文专项基线 | [ADR 0061](../adr/0061-terminal-turn-retry-and-provider-neutral-history.md) |

### 4.5 可信执行与交付

| 主题 | 冻结访问日期 | 参考版本 | 采用结果/当前入口 |
|---|---|---|---|
| [0.7可信执行与工程交付](trusted-execution-and-delivery.md) | 2026-09-09 | 正文专项基线 | [ADR 0065～0069](../adr/0065-platform-capability-ports-and-execution-plan.md) |
| [统一Action Plane与扩展边界](unified-action-plane-and-extension-boundaries.md) | 2026-09-09 | 正文专项基线 | [ADR 0069](../adr/0069-unified-coding-action-risk-route.md)、[Trusted Actions模块](../modules/trusted-actions.md) |

### 4.6 产品协议与扩展

| 主题 | 冻结访问日期 | 参考版本 | 采用结果/当前入口 |
|---|---|---|---|
| [Agent Protocol与产品运行时](agent-protocol-product-runtime.md) | 2026-09-09 | 正文专项基线 | [ADR 0070～0072](../adr/0070-agent-protocol-v1-boundaries.md) |
| [MCP运行时与安全边界](mcp-runtime-and-security.md) | 2026-09-09 | 正文专项基线 | [ADR 0073](../adr/0073-mcp-catalog-binding-and-sandbox.md)、[MCP模块](../modules/mcp.md) |
| [Skills、Hooks与供应链边界](skills-hooks-and-supply-chain.md) | 2026-09-09 | 正文专项基线 | [ADR 0074](../adr/0074-skill-snapshot-and-hook-action-boundary.md) |
| [Provider配置与安全Fallback](provider-profile-config-and-safe-fallback.md) | 2026-09-09 | 正文专项基线 | [ADR 0075](../adr/0075-provider-profile-secret-and-safe-fallback.md) |

### 4.7 质量与结构治理

| 主题 | 冻结访问日期 | 参考版本 | 采用结果/当前入口 |
|---|---|---|---|
| [代码可读性与结构治理](code-readability-and-structure.md) | 2026-09-12 | 正文专项基线 | [ADR 0076](../adr/0076-code-readability-and-structural-governance.md) |

### 4.8 产品体验与终端交互

| 主题 | 冻结访问日期 | 参考版本 | 采用结果/当前入口 |
|---|---|---|---|
| [CLI/TUI产品体验与可恢复客户端](cli-tui-product-experience.md) | 2026-09-13 | 正文专项基线 | [ADR 0078](../adr/0078-product-shell-and-recoverable-client-state.md)、[0.9.1详细设计](../changes/m09-1-cli-tui-product-experience.md) |
| [配置、Preflight与Windows原生只读Runtime](configuration-preflight-and-windows-read-runtime.md) | 2026-09-13 | 0.9.1d专项基线 | [ADR 0079](../adr/0079-preflight-and-native-read-port.md)、[0.9.1d详细设计](../changes/m09-1d-configuration-preflight-windows-read.md) |
| [默认Trusted Action产品组合](default-trusted-action-product-composition.md) | 2026-09-13 | 0.9.1e专项基线 | [ADR 0080](../adr/0080-capability-proven-product-action-composition.md)、[0.9.1e详细设计](../changes/m09-1e-default-trusted-action-composition.md) |

## 5. 推荐阅读顺序

1. 先读[统一研究基线](baselines.md)，理解三个参考项目的固定提交、证据等级和clean-room限制；
2. 按当前问题选择专题研究，只读取其明确的研究范围、调用链和失败路径；
3. 进入关联ADR，区分参考项目事实与Harnessix独立选择；
4. 进入现行模块设计核对当前源码、接口、数据和失败恢复；
5. 最后从模块设计链接到测试与验证证据，不用研究文档中的历史测试数字判断当前发布状态。

## 6. 生命周期与更新规则

1. 已冻结研究保持`historical`，上游分支移动不得静默改写结论；
2. 需要升级参考版本时创建新研究版本或明确的新章节，记录新旧提交、访问日期和结论差异；
3. 外部源码路径优先使用提交永久链接；本地复查命令使用`HARNESSIX_RESEARCH_ROOT`占位符，不记录个人目录；
4. 新发现先标记事实、行为验证、推断或待验证，不能把推断直接写成产品能力；
5. 研究结论进入实现后，必须链接ADR、变更设计、现行模块和证明该边界的测试；
6. 参考实现的许可证、来源性质或可复查性变化时，必须同步[自研与复用边界](../build-vs-buy.md)。

## 7. 当前限制

冻结资料覆盖了0.2～0.9.0已经使用的主要参考机制，以及0.9.1完整TUI设计所需的产品交互证据，但不代表持续
跟踪上游最新版本。0.9后续切片若涉及Eval基线、三平台发行、长期Soak、远程MCP认证或供应链发布，必须先建立
对应的新版本研究证据，不能继续外推现有冻结结论。
