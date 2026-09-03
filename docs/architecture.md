# Harnessix Code 总体架构

## 1. 文档状态

本文同时描述 Harnessix 0.1 的**当前实现**和 Harnessix Code 1.0 的**目标架构**。所有尚未实现的组件均明确标记，避免把路线图能力描述成现有功能。

当前状态：

- 已实现：Action Plane、HTTP API、SDK、Worker、SQLite/PostgreSQL Journal、基础 Policy/Approval、OpenTelemetry；
- 已完成设计：Thread/Turn/Item/Event、Agent Loop 与取消、Provider Event、App Server Protocol、Session Store 与恢复、威胁模型和测试/Eval 规范；
- 已实现 0.3.1 核心切片：进程内 Loop、基础领域模型、SQLite Session Store、Fake/Scripted Provider、取消、保守恢复和进程故障注入；
- 已实现 0.3.2：持久审批检查点、答复/取消/显式继续、指纹绑定、跨重启预算和 Session v1→v2 迁移；
- 已实现 0.3.3：Plan/Compaction/Error 语义契约、统一错误、Store Contract、Agent OTel 和 v1/v2→v3 迁移；0.3 范围本地验收完成；
- 0.4 进行中：双 Adapter、尝试/失败用量账本、0.4.3a 成本报告、0.4.3b1 受控 Smoke/白名单诊断、0.4.3b2 响应计费元数据已通过离线验收；百炼文本/内存工具/审批重开实测通过；真实计价适用性验收尚未完成。其他后续规划：Context Engine、Coding Tools、Sandbox、MCP/Skills 和产品化 Evals；
- 当前版本仍不能作为完整 Coding Agent 使用。

## 2. 架构目标

Harnessix Code 的目标是成为生产级、本地优先、模型无关的 Coding Agent。系统必须同时满足：

1. 在真实仓库中形成“理解—修改—验证—交付”闭环；
2. 模型、工具、客户端和持久化后端通过稳定契约解耦；
3. 长时间运行可以取消，进程退出后可以恢复到明确状态；
4. Context 的来源、预算、裁剪和压缩可检查；
5. 文件、命令、网络和外部副作用具有分层安全边界；
6. 核心行为能够使用 Fake Provider 确定性复现；
7. 每个关键状态转换、工具执行和审批都有结构化事件；
8. 不以多 Agent、RAG 或复杂工作流掩盖单 Agent Runtime 的不可靠。

## 3. 系统上下文

```text
                 Developer / CI / IDE
                          │
               CLI / TUI / SDK / Client
                          │
               Versioned Agent Protocol
                          │
┌─────────────────────────▼─────────────────────────┐
│                 Harnessix Code                    │
│                                                   │
│  App Server ─ Agent Runtime ─ Context Engine      │
│                     │             │               │
│               Model Runtime   Session Store       │
│                     │                             │
│               Coding Tool Runtime                 │
│                     │                             │
│           Workspace / Process / Sandbox           │
│                     │                             │
│             Harnessix Action Plane                │
└───────────────┬─────────────┬─────────────┬────────┘
                │             │             │
        Model Providers   Local OS      MCP / SaaS / DB
```

## 4. 逻辑分层

### 4.1 Client Layer

职责：

- 接收用户输入并展示流式文本、Tool Call、Diff、审批和错误；
- 发起创建、恢复、取消、审批、回滚和分叉请求；
- 不拥有 Agent 状态机，不直接执行工具。

第一版提供 CLI；TUI 和 IDE 在协议稳定后接入。

### 4.2 App Server 与 Protocol

职责：

- 提供基于标准 JSON-RPC 2.0、默认 stdio JSONL 的版本化双向 Agent Protocol；
- 管理 Client Session 与 Thread 的绑定；
- 将命令路由到 Agent Runtime；
- 推送 Item 生命周期、文本增量、工具进度和审批请求；
- 支持 Headless 客户端和后续 IDE/Web 客户端。

协议不直接暴露供应商事件。所有 Provider 响应必须先归一化为 Harnessix 内部事件。

### 4.3 Agent Runtime

Agent Runtime 是 Coding Agent 的核心应用服务，负责：

- 创建和恢复 Thread；
- 驱动 Turn 生命周期；
- 构造模型请求并消费流式模型事件；
- 将 Tool Call 交给 Tool Runtime；
- 把 Tool Result 回送给模型；
- 处理最大步数、取消、重试、上下文溢出和终止；
- 持久化可恢复事件并发布客户端事件。

Agent Runtime 不直接访问文件、执行 Shell 或调用供应商 SDK。

### 4.4 Model Runtime

Model Runtime 包含：

- `ModelProvider` 统一端口；
- OpenAI-compatible 和 Anthropic Adapter；
- 流式文本、Tool Call、Usage、Stop Reason 和错误归一化；
- Provider 能力发现；
- 限流、可重试错误分类和退避；
- 请求取消和 Token/Cost 统计。

Provider Adapter 不决定 Agent 是否重试写操作，也不直接调用本地工具。

0.4.2b1 提供 ModelAttemptStarted/ModelUsageObserved/ModelAttemptFinished 的 Provider v2 元数据契约，b2 已接入两个实际 SDK。Kernel 先提交尝试意图再继续消费，累计观测按差额计入唯一的 Turn 预算，取消/恢复结算开放尝试而不伪造未知用量。缓存和推理为总量子集；旧自定义 Provider 仍可使用 v1 响应记账。Adapter 类型不等于计费平台。详见 [ADR 0016](adr/0016-model-attempt-ledger.md) 与 [ADR 0017](adr/0017-provider-attempt-usage.md)。

0.4.3a 的价格与成本报告为独立纯函数模块：可信宿主显式绑定快照和计费上下文，按已结束尝试的完整用量计算 Token 成本；未计价尝试和旧步骤保持未知，不与已知小计混成总账单。JSON 重新加载会重算价格/用量/金额，历史会话不回填当前价格。实时费用硬上限、账单核对与自动能力发现仍未实现，见 [ADR 0018](adr/0018-versioned-token-cost.md)。

0.4.3b2 将响应原生计费元数据与 Usage 同事务持久化，当前 Provider Event 为 v3。只有可信宿主明确声明匹配直连计费平台时，才映射对应服务等级/地域/单一 TTL；代理或百炼不自动归因，冲突拒绝，缺失及混合 TTL 保持未知。CostReport v1 仍是事后重算资料，不能独立证明元数据来源；原始观测证据留在 Session，见 [ADR 0020](adr/0020-observed-billing-context.md)。

### 4.5 Context Engine

Context Engine 负责：

- 系统基础指令；
- 用户级和项目级指令发现；
- Workspace、Git 和环境摘要；
- Tool Definition 选择；
- Thread 历史规范化；
- Token Budget 计算；
- 大型 Tool Result 裁剪；
- 自动 Compaction 和摘要持久化；
- Context 来源与决策的诊断输出。

Context Engine 不负责全文代码索引。只有真实 Eval 证明必要时才引入索引或向量检索。

### 4.6 Coding Tool Runtime

第一版工具集合：

```text
read_file       list_files       glob           grep
apply_patch     shell            git_status     git_diff
run_tests       ask_user         mcp_call
```

Tool Runtime 负责：

- Tool Schema 注册与版本化；
- 参数校验和规范化；
- 并发、互斥和取消策略；
- 输出大小限制与落盘引用；
- Tool Call 与 Tool Result 精确关联；
- 权限判定和审批请求；
- 结构化错误和可观测性。

只读工具可以并发。文件写入、Shell 和其他有副作用工具默认串行，除非工具明确声明安全并发语义。

### 4.7 Workspace、Process 与 Sandbox

Workspace Runtime 提供：

- 规范化工作目录和符号链接检查；
- Workspace 内外路径边界；
- 原子文件写入和变更 Diff；
- Git 仓库状态与脏文件保护；
- Shell 子进程、进程组、超时、取消和输出截断；
- 环境变量和 Secret 的最小化注入；
- Host 与隔离执行后端的显式安全级别；
- 网络出口策略。

第一版至少提供：

- `host`：本机受限执行，依赖权限审批，不宣称强隔离；
- `container`：容器化隔离后端，用于需要更强边界的命令。

如果 Python 无法可靠提供跨平台 PTY、进程树清理或低层隔离，再以基准和失败测试为依据引入 Rust Sidecar。

### 4.8 Session Store

Session Store 与 Action Plane 的 Effect Journal 分离：

- Session Store 保存 Agent 对话和运行生命周期；
- Effect Journal 保存外部副作用事实；
- 两者通过稳定的 `thread_id`、`turn_id`、`item_id`、`action_id` 关联；
- Session Store 使用追加式 AgentEvent 和事务内物化投影；
- 不把模型流式 token 全部当作业务事实无限保存，Item 终值才是默认恢复事实；
- 持久事件、物化快照和 Compaction 必须有 Schema 版本及迁移。

第一版使用 SQLite，服务端多实例需求明确后再增加 PostgreSQL 实现。

当前 Agent Event/Thread 为 v5，最低读者由 Migration 0005 约束；v1–v4 事件与旧 Schema 保持原文。尝试用量属于 Session 事实，不放入对话 Item 或外部副作用 Journal。

### 4.9 Harnessix Action Plane

现有 Action Plane 继续拥有：

- `ActionRequest` 和 `ToolDefinition`；
- 副作用分类和风险等级；
- Policy、Approval 和请求指纹；
- 幂等键和 Effect Journal；
- Worker Lease、Heartbeat 和过期恢复；
- `UNKNOWN` 和 Executor Reconciliation；
- SQLite/PostgreSQL 后端；
- Trace Context 和结构化审计。

Coding Agent 中只有外部、高风险或结果可能不确定的副作用必须进入 Action Plane。普通只读文件工具不承担 Durable Action 的额外成本。

### 4.10 Extension Runtime

扩展层支持：

- MCP Client 和可选 MCP Server；
- 项目指令文件；
- Skills 的发现、选择和渐进加载；
- 生命周期 Hooks；
- 自定义 Tool Provider。

所有扩展最终仍通过 Tool Runtime、Permission 和 Sandbox 边界，不能绕过安全策略直接执行。

### 4.11 Observability 与 Evals

横切能力包括：

- Thread、Turn、Model、Tool、Action 的关联 Trace；
- Token、成本、延迟、重试和错误分类指标；
- 默认脱敏的结构化日志；
- Transcript Replay；
- 真实仓库任务 Eval；
- 故障注入和恢复测试；
- 版本间质量与成本回归报告。

## 5. 核心领域模型

```text
Thread
  └── Turn
        ├── UserMessage Item
        ├── AssistantMessage Item
        ├── ToolCall Item
        ├── ToolResult Item
        ├── ApprovalRequest Item
        ├── Plan Item
        └── Error Item
```

### Thread

表示一个可恢复 Coding Agent 会话，拥有 Workspace、配置快照和单调递增事件序列。

### Turn

表示从一次用户输入开始，到 Agent 给出最终响应、失败或被取消为止的执行边界。一个 Turn 可以包含多次模型调用和工具调用。

### Item

表示客户端可观察、可持久化的语义单元。Item 使用 `started → delta* → completed|failed|cancelled` 生命周期；不是所有 Item 都产生 delta。

### Run State

Run State 是 Agent Runtime 的运行中状态，负责当前模型请求、工具任务、取消信号和预算，不直接等同于持久化 Transcript。

## 6. 关键执行流程

### 6.1 正常 Coding Turn

```text
Client 提交消息
  → 创建 Turn 与 UserMessage
  → Context Engine 构造请求
  → Model Runtime 流式响应
  → Agent Runtime 发现 Tool Call
  → Permission 判定
  → Tool Runtime 执行并持久化结果
  → 结果回送模型
  → 模型给出最终回答
  → Turn COMPLETED
```

### 6.2 审批流程

```text
Tool Call
  → 规范化参数、cwd、环境和风险
  → 生成不可变审批指纹
  → 发布 ApprovalRequest Item
  → Turn 进入 WAITING_APPROVAL
  → 用户批准或拒绝
  → 校验指纹和会话身份
  → 执行或返回拒绝结果
```

### 6.3 取消流程

```text
Client Cancel
  → 设置 Turn Cancel Token
  → 取消模型流
  → 终止可终止工具和进程树
  → 等待资源清理
  → 持久化 CANCELLED
  → 发布最终状态
```

已经提交但结果未知的外部副作用不能伪装成已取消，必须交给 Action Plane 进入 `UNKNOWN` 或对账。

### 6.4 崩溃恢复

恢复时根据持久状态分类：

- 未发出的模型请求可以重新构造；
- 中断的只读操作可以安全重试；
- 中断的本地写操作先检查文件和 Git 状态；
- 外部写操作不自动重放，由 Action Plane 对账；
- 无法证明安全恢复的 Turn 进入 `INTERRUPTED`，由用户选择继续或终止。

## 7. 运行时不变量

### Agent 与会话

1. 同一 Thread 的持久事件序号严格递增。
2. 同一 Turn 同时最多只有一个活跃的主 Agent Loop。
3. 每个 Tool Result 必须引用已存在且唯一的 Tool Call。
4. Turn 终态不可隐式回到运行态。
5. 恢复操作不得静默重复未知副作用。
6. Provider 原始事件不得直接成为公共协议契约。

### Context

7. 每次模型请求都能解释主要 Context 来源和预算使用。
8. Compaction 结果必须作为独立 Item 持久化，不能原地篡改历史事实。
9. Tool Result 截断必须显式标识，并保留获取完整结果的受控引用。

### 工具与安全

10. Tool 风险和权限来自运行时注册表，不能信任模型自报。
11. 文件路径在执行前必须解析到明确 Workspace 边界。
12. 审批绑定规范化后的工具、参数、cwd、环境摘要和策略版本。
13. 用户取消后，Runtime 必须停止接受该 Turn 的新 Tool Call。
14. 扩展工具不得绕过 Permission、Sandbox 和审计边界。

### Action Plane

15. 要求幂等的 Tool 没有幂等键时不得执行。
16. 状态更新和 Action Event 追加必须位于同一事务。
17. 外部写操作发生未分类异常时默认进入 `UNKNOWN`。
18. `UNKNOWN` 不自动重放原 Action。
19. 对账只能观察外部系统，不能重复执行原操作。
20. 明文凭据不得进入 Transcript、Journal、Trace 或日志。

## 8. 当前代码到目标模块的演进

```text
src/harnessix/
├── agent/             # 已实现基础切片：Loop、领域模型、Reducer、取消
├── models/            # 中立端口、离线 Provider、OpenAI/Anthropic Adapter 与有界传输
├── context/           # 规划：指令、预算、裁剪、压缩
├── tools/             # 规划：Coding Tool Runtime
├── workspace/         # 规划：文件、Git、进程、Sandbox
├── protocol/          # 规划：App Server Protocol
├── session/           # 已实现 SQLite Event Log、聚合投影、迁移和宿主锁
├── extensions/        # 规划：MCP、Skills、Hooks
├── evals/             # 规划：Replay、任务评测、故障注入
├── domain/            # 已实现：Action Plane 领域模型
├── storage/           # 已实现：Effect Journal
├── policy/            # 已实现：Action Policy
├── executors/         # 已实现：Action Executor
├── api/               # 已实现，后续扩展为 App Server
├── sdk/               # 已实现：Action SDK，后续增加 Agent SDK
├── adapters/          # 已实现：LangGraph Action Adapter
├── observability/     # 已实现，后续扩展 Agent 指标
├── runtime.py         # 已实现：ActionService
└── worker.py          # 已实现：Action Worker
```

演进过程中不进行一次性目录大搬迁。新模块按里程碑加入；只有出现清晰依赖冲突时，才将现有 Action Plane 移入独立命名空间。

## 9. 已确认与待决策事项

### 已确认

- 产品名：Harnessix Code；
- Python-first；
- 自研 Agent Loop；
- 本地优先、CLI + Headless App Server；
- SQLite 为本地 Session Store；
- Session Store 采用 Thread 内单调 AgentEvent 与事务投影；
- Agent Loop 采用持久边界驱动的状态机和分层 Cancel Token；
- Provider 使用供应商中立的流式事件和结构化错误；
- Agent Protocol 使用标准 JSON-RPC 2.0，第一版传输为 stdio JSONL；
- 0.3 Kernel 先提供进程内宿主，使用本地单宿主锁和初始聚合快照，见 [ADR 0011](adr/0011-kernel-host-and-initial-projection.md)；
- 持久审批采用暂停返回、答复仅落库、显式继续；仅开放可信只读工具，见 [ADR 0012](adr/0012-durable-approval-checkpoint.md)；
- Action Plane 作为治理子系统保留；
- 参考实现采用 clean-room 研究方式。

已确认事项分别由 [ADR 0005](adr/0005-evolve-to-harnessix-code.md)、[ADR 0006](adr/0006-thread-turn-item-event-model.md)、[ADR 0007](adr/0007-agent-loop-and-cancellation.md)、[ADR 0008](adr/0008-provider-event-model.md)、[ADR 0009](adr/0009-app-server-protocol.md) 和 [ADR 0010](adr/0010-session-store-and-recovery.md) 固化。

### 必须通过 ADR 决定

- Patch 的原子提交和回滚模型；
- Host/Container Sandbox 的默认策略；
- TUI 技术栈；
- 是否以及何时引入 Rust Process/Sandbox Sidecar；
- Subagent 的状态隔离、预算和权限继承模型。
