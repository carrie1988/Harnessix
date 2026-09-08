# Harnessix Code 总体架构

## 1. 文档状态

本文同时描述 Harnessix Code 的**当前实现**（含0.1 Action Plane至0.6.5和已完成的0.7可信执行与工程交付）和1.0的**目标架构**。所有尚未实现的组件均明确标记，避免把路线图能力描述成现有功能。

当前状态：

- 已实现：Action Plane、HTTP API、SDK、Worker、SQLite/PostgreSQL Journal、基础 Policy/Approval、OpenTelemetry；
- 已完成设计：Thread/Turn/Item/Event、Agent Loop 与取消、Provider Event、App Server Protocol、Session Store 与恢复、威胁模型和测试/Eval 规范；
- 已实现 0.3.1 核心切片：进程内 Loop、基础领域模型、SQLite Session Store、Fake/Scripted Provider、取消、保守恢复和进程故障注入；
- 已实现 0.3.2：持久审批检查点、答复/取消/显式继续、指纹绑定、跨重启预算和 Session v1→v2 迁移；
- 已实现 0.3.3：Plan/Compaction/Error 语义契约、统一错误、Store Contract、Agent OTel 和 v1/v2→v3 迁移；0.3 范围本地验收完成；
- 0.4核心运行能力已交付但整体仍待0.4.3c关闭：双Adapter、尝试/失败用量账本、成本报告、受控Smoke/白名单诊断和响应计费元数据已通过对应验收；百炼文本、内存工具和审批重开实测通过；真实计价适用性作为0.9发布证据门禁继续跟踪，未关闭时不得发布1.0；
- 0.5 已实现只读工具、有界Artifact、受管单文件/整组Patch及计划/效果Diff、受控Process/Git/测试反馈、真实缺陷Eval和显式单文件工作树交付；0.5.6补齐受信并发能力、连续只读有界调度、写/审批屏障、失败排空和统一工具错误类别。任意Shell字符串被正式排除，非交互命令由结构化`host.process`承担；OS隔离、通用多文件发布和自动commit/push属于后续版本，见[实施设计](m05-coding-tools.md)；
- 0.6.1已实现静态Context Fragment、固定指令优先级、保守输入预算、双Provider system映射、Event v10检查记录和诊断；0.6.2a已完成项目指令Source与Context Inspection v2；0.6.2b已完成Workspace/Git/环境Source、乐观双观测、Context Inspection v3、Event/Thread v12、migration14及低基数一致性指标。0.6.2c已实现Tool Result稳定模型视图与Artifact覆盖校验。0.6.3已实现独立摘要账本、Cost v2、无工具摘要、轮前/reactive触发、线性活动窗口、Model History Inspection v2和语义Eval。0.6.4已实现同身份Resume、无授权Fork、Archive、跨代Artifact所有者校验和来源CAS。0.6.5已实现终态Turn Retry、Interrupted Recovery、双向Provider切换和长会话综合恢复；当前Event/Thread为v17、migration为19，见[实施设计](m06-context-and-sessions.md)、[自动Compaction详设](compaction-runtime-and-windows.md)、[Thread生命周期详设](thread-lifecycle.md)与[Retry详设](turn-retry-and-provider-switch.md)；
- Windows已进入1.0目标；0.7.1已增加Windows原生Workspace Snapshot端口，0.7.2在三平台运行Sandbox/Secret合同并以Docker兼容容器提供强隔离适配；0.7.3的Windows Process/Job Object/ConPTY及Container统一生命周期、0.7.4受管Git交付和0.7.5平台中立Action入口均已通过综合六矩阵门禁；完整发行物尚未交付，不能据此宣称Windows产品当前可用；
- 0.7.0已冻结Codex/OpenCode/Claude Code参考版本，完成差距矩阵、五项ADR及Threat Model v2；0.7.1实现平台路径、选择资源Snapshot、跨进程fencing租约、完整Execution Plan/Approval指纹和私有持久检查点；0.7.2实现Container Profile/Command、能力实测、选择性网络、受管CONNECT/SNI出口、Secret Provider/Redactor/Guard和Profile持久化；0.7.3交付Process合同、计划绑定、append-only Lease Store、POSIX Session/PTY owner、Windows suspended Job/ConPTY，以及ContainerExecution到ProcessLaunch的正式绑定、即时网络复核和标签化残留清理；0.7.4交付Workspace Transaction、私有CAS、append-only事务账本、POSIX发布/恢复、新事务Rollback、完整Diff和受管Git worktree/checkpoint/commit；0.7.5交付宿主Binding、规范资源、统一Policy/Approval、哈希链审计、受限Extension端口和独立Git Push/reconcile。Windows Snapshot和Container冷启动探测加固后，0.7最终由[CI 34268017600](https://github.com/carrie1988/Harnessix/actions/runs/34268017600)关闭，见[可信执行设计](m07-trusted-execution-and-delivery.md)；
- 当前版本仍不能作为完整 Coding Agent 使用。

## 2. 架构目标

Harnessix Code的目标是独立实现面向真实软件工程任务的、本地优先、模型无关、安全可控、可恢复、可审计、可评测、可扩展的生产级Coding Agent。系统必须同时满足：

1. 在真实仓库中形成“理解—规划—修改—执行—验证—审查—交付”闭环；
2. 模型、工具、客户端和持久化后端通过稳定契约解耦；
3. 长时间运行可以取消，进程退出后可以恢复到明确状态；
4. Context 的来源、预算、裁剪和压缩可检查；
5. 文件、命令、网络和外部副作用具有分层安全边界；
6. 核心行为能够使用 Fake Provider 确定性复现；
7. 每个关键状态转换、工具执行和审批都有结构化事件；
8. 不以多 Agent、RAG 或复杂工作流掩盖单 Agent Runtime 的不可靠。

1.0是面向大量独立macOS、Linux和Windows终端用户安装和长期使用的本地优先商业版本，而不是Runtime演示或集中式多租户SaaS。“大量用户”要求发行物、兼容升级、恢复、诊断、安全和质量基线可以在大量相互独立的本地实例中复现；不要求1.0实现远程执行池、云端高可用或集中式服务SLO。产品与平台边界见[ADR 0062](adr/0062-local-first-v1-commercial-boundary.md)和[ADR 0063](adr/0063-windows-v1-platform-support.md)。

社区版从许可证切换边界起按照`AGPL-3.0-only`发布，版权所有者保留独立商业授权能力。历史MIT版本、外部贡献、品牌和第三方依赖分别维持可审计权利链，见[ADR 0064](adr/0064-agpl-and-commercial-dual-licensing.md)。

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

1.0在macOS、Linux和Windows提供CLI/TUI；IDE、桌面客户端和Web在协议稳定且有真实需求后于1.x评估。薄CLI在0.8随Protocol和App Server接入，完整交互体验在0.9收口。

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

0.6.1静态规划和0.6.2动态来源实现位于`harnessix.context`。`ContextPlanner`接收Runtime生成的确定性历史/Tool文档，按Runtime > User > Project > Workspace > Git > Environment排序并在明确窗口内选择；Runtime/User为必选，超限在发网前失败。`AsyncContextPlanner`先通过宿主绑定Source刷新外部状态，再委托同一纯规划器。`ProjectInstructionSource`只在能力根到工作目录祖先链读取`AGENTS.override.md|AGENTS.md`；`WorkspaceContextSource`提供根与工作目录一级概览；`GitContextSource`复用固定Git只读运行时；`EnvironmentContextSource`只读取宿主allowlist。Source正文只能进入宿主固定kind的Fragment。

每次启用Planner的模型步骤先持久`ContextPrepared`，再调用Provider。Context Inspection v1记录静态规划；v2增加单Source的无正文revision、scope、字节数和Fragment绑定；v3为两个及以上Source增加`optimistic-double-observation/v1`一致性快照。该快照只证明两轮有界窗口内未检测到变化，不是跨文件系统/Git事务。`utf8-bytes/v1`是确定性的保守估算，不等同于Provider Usage或精确Tokenizer。来源读取失败不降级成empty，也不在缺少持久正文时回退到旧revision。

### 4.6 Coding Tool Runtime

1.0工具集合：

```text
read_file       list_files       glob           grep
apply_patch     host.process     git_status     git_diff
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

当前实现只有受信定义显式opt-in的连续只读调用可以有界并发。文件写入、Process、审批和其他调用是顺序屏障；未来有副作用工具不得仅凭名称或模型声明开启并发。

### 4.7 Workspace、Process 与 Sandbox

Workspace Runtime 提供：

- POSIX与Windows分离的平台实现，以及不包含平台判断的上层领域端口；
- 规范化工作目录和符号链接检查；
- Workspace 内外路径边界；
- 多文件事务写入、Checkpoint、Rollback和变更Diff；
- Git仓库状态、脏文件保护、Branch/Worktree和显式Commit；
- 受控Shell、PTY、标准输入、后台进程、进程组、超时、取消、宿主死亡监督和输出Artifact；
- 环境变量和 Secret 的最小化注入；
- Host 与隔离执行后端的显式安全级别；
- 网络出口策略。

1.0至少提供：

- `host`：本机受限执行，依赖权限审批，不宣称强隔离；
- `container`：容器化隔离后端，用于需要更强边界的命令。

如果 Python 无法可靠提供跨平台 PTY、进程树清理或低层隔离，再以基准和失败测试为依据引入 Rust Sidecar。

Windows原生实现不能复用POSIX权限位和Process Group假设。Workspace必须单独处理盘符、UNC、保留名、ADS、大小写折叠、长路径和Reparse Point/Junction；Process使用Job Object等原生能力建立进程树归属。Windows强隔离优先通过受管WSL2或Docker Desktop后端提供，但仅在WSL2内运行不能冒充Windows原生Workspace支持。

### 4.8 Session Store

Session Store 与 Action Plane 的 Effect Journal 分离：

- Session Store 保存 Agent 对话和运行生命周期；
- Effect Journal 保存外部副作用事实；
- 两者通过稳定的 `thread_id`、`turn_id`、`item_id`、`action_id` 关联；
- Session Store 使用追加式 AgentEvent 和事务内物化投影；
- 不把模型流式 token 全部当作业务事实无限保存，Item 终值才是默认恢复事实；
- 持久事件、物化快照和 Compaction 必须有 Schema 版本及迁移。

1.0本地Session Store使用SQLite；Action Plane继续保留现有SQLite/PostgreSQL后端。远程服务端Session的PostgreSQL实现只有在1.x多实例需求明确后立项。

当前Agent Event/Thread为v17，最低读者由Migration 0019约束；v1–v16事件与旧Schema保持原文。受管Patch的完整计划/镜像/意图留在副本账本，Session保存写审批和最小私有效果证据；两库不原子提交，通过稳定调用ID只读核对，不重放写入。尝试用量属于Session事实，不放入对话Item或外部副作用Journal。

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

0.7.5在旧Effect Journal之上增加`TrustedActionRouter`：所有新Coding Tool来源先形成宿主Binding、规范资源和不可变`ExecutionPlanV2`，再统一决定deny/allow/require approval。低风险只读不进入旧外部Effect Worker，但仍以轻量Action Audit记录Plan、资源摘要和结果摘要；外部、高风险或结果可能不确定的副作用再确定性投影到专用durable ledger或旧Effect Journal。这样避免为普通读取承担远端Worker成本，同时不允许扩展绕过统一风险路由。

Action Audit、Session Store、Process/Delivery Ledger和外部Effect Journal各自保存单一事实：Action Audit提供跨组件因果索引，Session保存对话和用户决定，专用账本保存本地文件/进程事实，Effect Journal保存外部Action。跨库不伪装为原子事务；稳定plan/action id、不可变fingerprint和只读reconcile用于恢复。

### 4.10 Extension Runtime

扩展层支持：

- MCP Client 和可选 MCP Server；
- 项目指令文件；
- Skills 的发现、选择和渐进加载；
- 生命周期 Hooks；
- 自定义 Tool Provider。

所有扩展最终仍通过 Tool Runtime、Permission 和 Sandbox 边界，不能绕过安全策略直接执行。0.7.5的`ExtensionActionPort`已经按source/source id固定能力，只暴露Binding查询、plan、execute、reconcile和status；MCP传输、Skill加载和Hook生命周期仍由0.8实现。

### 4.11 Observability 与 Evals

横切能力包括：

- Thread、Turn、Model、Tool、Action 的关联 Trace；
- Token、成本、延迟、重试和错误分类指标；
- 默认脱敏的结构化日志；
- Transcript Replay；
- 真实仓库任务 Eval；
- 故障注入和恢复测试；
- 版本间质量与成本回归报告。

评测从每个纵向切片开始，而不是等到0.9集中补建。0.7及后续实现必须同时提供确定性回归、按风险选择的故障注入、真实仓库场景和必要的受控真实Provider证据；0.9负责冻结任务集、指标口径和1.0发布阈值。

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
8. Compaction 结果必须作为独立版本化事实（摘要账本及窗口记录）持久化，不能原地篡改历史事实。
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
├── context/           # 已实现：规划、Source、一致性、稳定工具视图、自动Compaction与活动窗口
├── tools/             # 已实现：Workspace只读、搜索、Git与Artifact读取入口
├── artifacts/         # 已实现：事务正文、分页、配额、TTL与清理
├── patches/           # 已实现：单文件/整组计划、执行、恢复与Diff
├── processes/         # 已实现：宿主进程、Action桥接、测试Profile与输出
├── workspace/         # 已实现：跨平台Snapshot、路径端口与fencing租约
├── sandbox/           # 已实现：Host/Container能力、网络与启动绑定
├── secrets/           # 已实现：版本绑定、短期解析、流式脱敏与泄漏门禁
├── delivery/          # 已实现：事务、Git worktree/checkpoint/commit/push
├── trusted_actions/   # 已实现：统一风险路由、审计和扩展端口
├── protocol/          # 规划：App Server Protocol
├── session/           # 已实现 SQLite Event Log、聚合投影、迁移和宿主锁
├── extensions/        # 规划：MCP、Skills、Hooks
├── evals/             # 已实现：历史任务、评分、Campaign与受控交付
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
- 1.0面向大量独立macOS、Linux和Windows终端实例，不包含集中式多租户SaaS；
- Windows原生Workspace、Git、Process和CLI属于1.0正式范围，强隔离优先使用受管WSL2或Docker Desktop后端；
- 远程Sandbox、云任务、多租户控制面和分布式Agent Worker进入1.x候选范围；
- SQLite 为本地 Session Store；
- Session Store 采用 Thread 内单调 AgentEvent 与事务投影；
- Agent Loop 采用持久边界驱动的状态机和分层 Cancel Token；
- Provider 使用供应商中立的流式事件和结构化错误；
- Agent Protocol使用标准JSON-RPC 2.0，1.0基线传输为stdio JSONL；
- 0.3 Kernel 先提供进程内宿主，使用本地单宿主锁和初始聚合快照，见 [ADR 0011](adr/0011-kernel-host-and-initial-projection.md)；
- 持久审批采用暂停返回、答复仅落库、显式继续；默认仅开放可信只读工具，见 [ADR 0012](adr/0012-durable-approval-checkpoint.md)；受管单文件 Patch 通过独立写审批与专用端口开放，见 [ADR 0030](adr/0030-kernel-managed-patch-admission.md)；
- Action Plane 作为治理子系统保留；
- 参考实现采用 clean-room 研究方式。
- 社区版采用`AGPL-3.0-only`，版权所有者保留独立商业授权能力，代码许可证不授予品牌权利。

已确认事项分别由[ADR 0005](adr/0005-evolve-to-harnessix-code.md)、[ADR 0006](adr/0006-thread-turn-item-event-model.md)、[ADR 0007](adr/0007-agent-loop-and-cancellation.md)、[ADR 0008](adr/0008-provider-event-model.md)、[ADR 0009](adr/0009-app-server-protocol.md)、[ADR 0010](adr/0010-session-store-and-recovery.md)、[ADR 0062](adr/0062-local-first-v1-commercial-boundary.md)、[ADR 0063](adr/0063-windows-v1-platform-support.md)和[ADR 0064](adr/0064-agpl-and-commercial-dual-licensing.md)固化。

### 必须通过 ADR 决定

- 通用多文件Workspace事务、Git Checkpoint和Rollback模型；
- Host/Container Sandbox 的默认策略；
- 通用Shell、PTY和后台进程的能力与审批边界；
- TUI 技术栈；
- 本地发行物签名、自动更新和失败回滚策略；
- 是否以及何时引入 Rust Process/Sandbox Sidecar；
- Subagent 的状态隔离、预算和权限继承模型。

### 整组宿主审批与执行的当前落点（0.5.3c2）

`ManagedPatchBatches` 借用单个 ManagedPatchWorkspace 的连接、所有权和锁，复用单文件计划/镜像/事件校验与事务，而非新增副本管理器或替换引擎。副本账本 v2 将整组计划和所有成员在同一事务预留；独立组审批记录只代表完整计划的宿主决定，审批阶段成员保持 pending，旧单文件写入口拒绝拆分消费。组请求暂为宿主稳定身份，不冒充已验证的 Kernel Call。c2b 在账本 v3 增加独立运行记录并按顺序调用原单文件引擎，部分/未知效果与终止原因分开，见 [ADR 0033](adr/0033-batch-consumption-and-effect-recovery.md)；Agent/Session 批量契约由下述 c3b 接入。


### 整组宿主调用桥接的当前落点（0.5.3c3a）

`ManagedPatchBatchBridge` 在 `ManagedPatchBatches` 上校验宿主调用与完整组计划，使用独立调用审批指纹；不继承旧单文件审批，也不复用旧单文件工具名。工作线程复用既有组执行/核对，串行锁、操作停止信号和排空保障异步生命周期。它只借用副本，不读取 Session，也不能替代 Kernel 的活跃调用/原时限/等待状态持久消费。

`BatchCallResult` 分离公开有界路径/效果摘要与私有计划、后端审批、实际运行事实。c3a 当时未改变 Agent/Session/Provider/副本版本和旧单文件定义。c3b 已新增 Kernel 组端口与持久事件，见下节；c3c 才新增基于实际调用的 Diff Artifact 准入，不能放松现有只读发布器。详细分片见 [ADR 0034](adr/0034-batch-call-bridge-and-kernel-integration.md)。

### Kernel 整组调用的当前落点（0.5.3c3b）

`AgentRuntime.patch_batches` 是独立显式端口，仍复用原循环、审批事务与 Reducer；不在通用写注册表开放权限。`agent/batch_patching.py` 只校验完整授权与私有效果，不持有存储或后台任务；文件效果继续由 c3a 桥接和 c2 组引擎负责。Session v7 Item 保存完整审批计划及有界私有组效果，migration8 不重写旧历史或新增未来空表，副本仍为v3。

执行准入为当前活跃调用、完整匹配决定、原 Turn 时限和已持久消费等待；复核上下文不是执行许可。恢复只查原记录/观察已开始成员，任一证明不足为 unknown，不跨两个账本补批/重放。结果超预算也保留已发生效果，非正常组运行不能冒充完成 Turn。模型历史仍按原白名单转换，不传私有组计划/批准/效果。Diff Artifact 仍需后续专用事务发布，不修改只读发布器，见 [ADR 0035](adr/0035-kernel-batch-approval-and-recovery.md)。

### 差异报告准备的当前落点（0.5.3c3c1）

`diff_document_contracts` 定义文档及 JSONL 行契约；`diff_document` 复用原精确编辑迭代器，生成计划或已归因历史视图。`ManagedPatchBatchBridge.diff` 在副本锁内核对完整调用/组计划/批准和当前已结算运行，再读取私有镜像准备文档；不读取当前目标、不新增观察或执行。

公共报告与完整宿主证据分离，生成报告不授予发布权。现有 Artifact 仅接受成功只读 ToolResult，表中每调用仅一条，引用也只认 `output.artifact`；c3c2 必须对计划和效果分别绑定真实 Session 事实并保持同事务，不能伪造只读调用或成功结果。本片不提前修改 Session 格式，见 [ADR 0036](adr/0036-batch-diff-documents-and-artifact-admission.md)。

整组 Diff 采用独立事务发布端口：审批/结果的 `diff_artifact` 绑定 Artifact 表的用途，不改变组公开结果或私有效果。计划/效果正文与对应 Session 事实同事务；归档失败可省略展示引用，但不能丢失真实效果。详见 [ADR 0037](adr/0037-batch-diff-transaction-publication.md)。

0.5.4a的 `HostProcessRuntime` 是显式受信宿主API，依赖既有取消/错误模型而不修改Kernel或引入新Agent Loop。二流使用公开SubprocessProtocol/Transport区分退出、EOF与强制关闭；这里只保证声明范围内的执行/回收，不是权限审批、进程树容器或跨重启恢复，详见 [ADR 0038](adr/0038-host-process-lifecycle.md)。

0.5.4b1通过`ProcessActionExecutor`把上述API接入0.1 Action Plane，而不是增加进程专用账本。Effect Journal保管命令意图、ToolDescriptor、审批、租约和UNKNOWN事实；Executor只负责比较持久工具版本/宿主绑定并执行。宿主硬退出只能把过期RUNNING恢复UNKNOWN，不能证明操作系统进程已终止，详见 [ADR 0039](adr/0039-process-action-plane-admission.md)。

0.5.4b2b1新增`AgentProcessCallPlan`和纯桥接准备/核对函数。Thread/Turn/Call、工作区、完整ToolCall、Action请求、主体、持久工具版本和宿主绑定共同确定Action ID与幂等键；重新准备只能得到同一Action。

0.5.4b2b2把已核对的Action事实投影到Agent v9。进程审批、Action状态和Tool Result效果使用独立类型；Reducer强制`WAITING_APPROVAL → WAITING_ACTION → EXECUTING_TOOLS`，活跃Action不能产生结果或恢复模型循环，终止结果必须绑定最后观察。Session私有计划、决定和效果不进入模型历史。真实v8旧wheel、旧reader拒绝及migration10硬退出已验证事件/投影原字节兼容。

0.5.4b2c1增加显式`ProcessRuntime`端口和`ProcessAgentBridge`。Agent Runtime只负责稳定Action提交、唯一决定协调与单次观察；`ActionWorker`仍是唯一执行调度方。审批答复不运行命令，决定已写Action而Session未提交时按相同决定补投影；WAITING_ACTION恢复每次最多读取一个快照，活跃快照去重，终态状态与Tool Result同批提交。公开结果只有生命周期和流摘要，不含Base64正文。单一审批与跨库恢复边界见 [ADR 0040](adr/0040-agent-process-action-saga.md)。

0.5.4b2c2增加`ProcessArtifactPublisher`端口和SQLite实现。已核对的完整`ProcessResult`只在宿主私有观察中传递，生成summary+双流Base64 chunk的`process-output/v1`文档；正文、manifest、结果引用和Process终态事件同Session事务。`process_output`用途由migration11显式加入白名单，读取时重新绑定批准/Action/Call和流摘要。发布失败降级保留无引用终态，提交前崩溃可从原Action重建，提交后不重放；Effect Journal与Session仍非原子。详见 [ADR 0041](adr/0041-process-output-artifact.md)。

0.5.4b2c3完成当前Process Saga恢复矩阵。Runtime可从Session已有ToolCall但无审批Item的窗口按稳定身份找回同一Action；缺少原端口时保持事实。WAITING取消只结束Session观察并以unknown/INTERRUPTED结算，不撤销Action决定或队列状态。SQLite/PostgreSQL把RUNNING/RECONCILING租约过期与`UNKNOWN/lease_expired`结果同事务保存；跨进程相同审批幂等、不同审批冲突。八个跨库边界和一个Worker租约边界以真实退出验证，双SDK离线HTTP完成审批重开、外部Worker和Artifact摘要读取。详见 [ADR 0042](adr/0042-process-saga-recovery-and-cancellation.md)。

0.5.4c在不新增执行权威的前提下增加两个窄入口。`GitReadRuntime`只由宿主显式绑定绝对Git可执行文件，固定status/diff子命令、环境、config、精确仓库根和输出预算，经`CodingToolRuntime`只读端口暴露`git_status`/`git_diff`。`RunTestsAgentBridge`把公开`run_tests(profile)`解析成宿主固定`ProcessRequest`；公开工具版本绑定Profile全集、工作区和后端Process工具，实际Action仍为`host.process`，继续由原Journal审批、Worker执行和Artifact归档。非零测试退出映射`passed=false`而不伪装基础设施失败。组合闭环在三个权威事实域之间只读核对，不引入跨库超级事务或自动重放。详见 [ADR 0043](adr/0043-git-and-controlled-test-feedback.md)。

0.5.5在上述正式Runtime上增加版本化历史任务、隐藏检查、无Golden Patch评分、可恢复真实Provider Campaign和显式单文件交付。任务v3在固定历史缺陷上三次严格通过；交付层重新核对来源、工作树、前后镜像和批准指纹后原子修改目标工作树，恢复只观察已持久证据。它不提供三方合并、通用多文件发布或自动commit/push，见[ADR 0052](adr/0052-controlled-eval-change-delivery.md)。

0.5.6在现有`ToolDescriptor`增加默认关闭且只允许`READ_ONLY`的并发能力。Agent Runtime只并发连续安全前缀，默认上限4；执行完成后仍按Provider顺序写Session，任一异常或取消先排空兄弟任务。`CodingToolRuntime`以独立有界信号量限制实际读取；审批、Patch和Process保持屏障。工具域稳定错误统一归类但不改错误码。该设计是单进程调度，不替代0.7的跨进程锁和Sandbox，见[ADR 0053](adr/0053-tool-concurrency-and-error-taxonomy.md)。

## Tool Result双层历史边界（0.6.2c）

事实层仍由Session Event、原始Item、效果证据和Artifact构成，不能因输入预算回写或删除。投影层由纯`prepare_model_history`生成；`ModelHistoryPrepared`以Event v13保存首次决定和每步检查。Kernel先核验Artifact的当前访问scope、关联与内容，再提交决定并规划Context，最后发送模型请求。

Artifact完整性是字段覆盖证明而非通用“备份成功”标志：搜索归档只覆盖记录，Process归档只覆盖已捕获流，Batch Diff只覆盖差异。后两者不能替代任意结果字段。旧模型前缀、Artifact TTL、取消/超时和崩溃重启均属于此边界；应用不持久化供应商SDK对象。工具视图引入Event/Thread v13与migration15，摘要账本升级为v14/migration16，活动窗口升级为v15/migration17，Thread生命周期升级为v16/migration18，终态Retry来源升级为v17/migration19。Tool Result View v1和Context Inspection v3不变，Model History Inspection新增v2窗口证据，见[摘要账本设计](compaction-attempt-ledger.md)、[活动窗口设计](compaction-runtime-and-windows.md)、[Thread生命周期设计](thread-lifecycle.md)与[Retry设计](turn-retry-and-provider-switch.md)。

## Thread生命周期与Fork边界（0.6.4）

Resume是同身份、只读重新附着；Archive是追加事件表达的不可变只读状态。Fork在终结Turn边界将有界模型历史、Tool Result视图决定和Artifact真实所有者冻结到子Thread首事件，但固定`authority=none`且不复制父Thread的Turn、审批、Action租约或Runtime任务。子Thread可把继承事实发送给模型，执行端口不能把历史Tool Call解释为待处理工作。

跨Thread创建由SessionStore执行来源sequence CAS和完整快照纯函数重建，目标Event与Projection同事务提交。确定性子Thread和事件身份覆盖Commit后响应丢失；Rebuild先重放来源到冻结sequence，再验证子快照。Artifact正文保留在真实父级或祖先所有者下，每次模型使用仍按当前Workspace scope执行完整验证。该边界不提供文件系统回退、跨Workspace Fork、活跃Turn Fork或Unarchive，详见[ADR 0060](adr/0060-thread-lifecycle-and-authority-free-forks.md)。

## 终态Turn Retry与Provider中立历史（0.6.5）

Retry、Resume和Provider内部尝试是三个独立层次。`resume_turn`只继续同一Turn的持久审批或Action等待边界；Adapter内部Retry只重试尚未暴露语义或工具效果的暂态HTTP尝试；`retry_turn`则从最新失败、取消或中断终态创建新Turn，持久记录`retry_of_turn_id`，来源终态不可逆。

Retry使用固定续作User Item，不复制原Prompt。来源已完成Tool Result作为规范历史可见，但不属于新Turn的待执行集合；来源存在UNKNOWN结果时接受前失败关闭。普通Turn请求指纹保持兼容，Retry指纹额外绑定来源，幂等读取覆盖Commit后响应丢失。Runtime与Reducer分别校验最新来源、可重试终态和未知效果，避免直接写事件绕过入口。

Provider切换通过规范Item重新构造目标协议。历史Tool Call和Result共享`call_<UUID hex>`内部身份，供应商原生调用ID、Thinking签名、流游标及请求metadata不跨边界持久复用；每个`ModelAttempt`仍记录实际Provider和模型。该边界支持OpenAI-compatible与Anthropic双向切换，但不实现自动模型路由、Thinking缓存迁移或未知效果自动Reconcile，详见[ADR 0061](adr/0061-terminal-turn-retry-and-provider-neutral-history.md)。
