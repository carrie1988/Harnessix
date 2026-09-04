# Harnessix Code 设计与开发路线图

## 1. 路线图原则

Harnessix Code 的目标是生产级 Coding Agent，不是 POC 或功能演示。路线图采用“可发布的纵向切片”，但每个切片都必须包含正式契约、失败语义、持久化、可观测性、测试和文档。

开发顺序遵循：

```text
源码求证 → 架构决策 → 领域契约 → 最小正式实现
→ 失败与恢复测试 → 真实场景验证 → 文档同步
```

不按功能数量判断完成度。没有取消、超时、恢复、安全边界和回归测试的功能，不得标记为生产完成。

## 2. 当前基线：0.1 Action Plane

### 已完成

- [x] Python 3.12+ 工程骨架；
- [x] Action Contract v1 和 Tool Registry；
- [x] 运行时副作用分类；
- [x] Policy、持久化 Approval 和请求指纹；
- [x] SQLite/PostgreSQL Effect Journal；
- [x] 幂等键和载荷冲突检测；
- [x] 独立 Worker、持久队列、租约、心跳和过期恢复；
- [x] PostgreSQL 多 Worker 原子 Claim；
- [x] 显式 `UNKNOWN` 和 Executor 对账；
- [x] FastAPI、Python SDK 和 LangGraph Action Adapter；
- [x] OpenTelemetry Trace/Metrics 和结构化日志；
- [x] 不确定副作用和无重复恢复测试。

### 0.1 当时的限制（当前实现状态见后续里程碑）

- [ ] 没有 Agent Loop、Model Provider 和流式模型事件；
- [ ] 没有 Thread/Turn/Item 会话模型；
- [ ] 没有 Coding Tools、Context Engine 和 Sandbox；
- [ ] 没有 Agent CLI/TUI、App Server Protocol 和 Coding Eval；
- [ ] 当前版本不能称为完整 Coding Agent。

## 3. 里程碑总览

| 版本 | 里程碑 | 核心结果 | 依赖 |
|---|---|---|---|
| 0.2 | 产品与架构基线 | 研究框架、目标架构、协议和状态机决策 | 0.1 |
| 0.3 | Agent Runtime Kernel | 可恢复的 Thread/Turn/Item 与确定性 Agent Loop | 0.2 |
| 0.4 | Model Runtime | 两类 Provider、流式事件、错误和用量归一化 | 0.3 |
| 0.5 | Coding Tool Runtime | 完成读取、搜索、补丁、Shell、Git、测试闭环 | 0.4 运行基线；计价证据独立跟踪 |
| 0.6 | Context 与持久会话 | 指令、预算、压缩、恢复、取消和 Replay | 0.5 |
| 0.7 | 安全执行 | Workspace 边界、权限、Sandbox、网络和 Secret | 0.6 |
| 0.8 | App Server 与扩展 | 双向协议、Headless、MCP、Skills、Hooks | 0.7 |
| 0.9 | 产品硬化与 Evals | CLI/TUI、故障注入、质量/成本基线、跨平台 CI | 0.8 |
| 1.0 | 生产发布 | 稳定协议、安装升级、安全文档和发布保障 | 0.9 |

版本号代表能力成熟度，不承诺固定日期。每个里程碑完成后根据 Eval、风险和实际投入重新估算后续计划。

## 4. 0.2：产品与架构基线

状态：**已完成（2026-09-02）**。本阶段只完成研究与架构决策，没有实现或宣称 Agent Runtime 能力。

### 目标

通过源码研究和 ADR 固化 Coding Agent 的核心边界，避免一边编码一边猜测主流实现。

### 设计任务

- [x] 更新产品名称为 Harnessix Code；
- [x] 将 Action Plane 调整为内部治理子系统；
- [x] 建立目标架构和源码研究计划；
- [x] 固化 Codex、OpenCode、Claude Code 的研究提交号；
- [x] 完成 Agent Loop、Session、Protocol、Tool、Context、安全六个首要主题研究；
- [x] ADR：Thread/Turn/Item 数据模型；
- [x] ADR：Agent Loop 状态机和取消语义；
- [x] ADR：Provider 统一事件模型；
- [x] ADR：App Server Protocol 与传输；
- [x] ADR：Session Store 和恢复模型；
- [x] 威胁模型 v1；
- [x] 测试策略和 Eval 规范 v1。

### 阶段产出

- [研究基线](research/baselines.md)；
- [Agent Loop](research/agent-loop.md)、[Session](research/session-model.md)、[Protocol](research/protocol.md)、[Tool](research/tool-runtime.md)、[Context](research/context-engine.md)、[安全](research/security.md)；
- [ADR 0006](adr/0006-thread-turn-item-event-model.md) 至 [ADR 0010](adr/0010-session-store-and-recovery.md)；
- [威胁模型 v1](threat-model.md)；
- [测试与 Eval 规范 v1](testing-and-evals.md)。

### 验收标准

- 每个首要主题包含源码调用链、失败路径和 Harnessix 决策；
- 核心状态图和 Protocol 草案能够覆盖正常、审批、取消和崩溃恢复；
- 所有规划能力明确标记，README 不把目标能力写成当前能力；
- `make check` 继续通过。

### 非目标

- 不实现真实模型 Agent Loop；
- 不重构现有 Action Plane 目录；
- 不选择 TUI 外观和 IDE 集成。

## 5. 0.3：Agent Runtime Kernel

状态：**已完成 0.3 范围内实现与本地验收**。0.3.1 核心、0.3.2 持久审批、0.3.3 语义契约/可观测性/存储门禁均已落地；自动规划、自动压缩、真实模型和真实写工具不属于本阶段。具体支持边界见 [Kernel 实施设计](m03-runtime-kernel.md)。

### 目标

实现不依赖真实模型的确定性 Agent Runtime，使生命周期、持久化和故障语义先于 Provider 复杂度稳定下来。

### 核心交付

- [x] `Thread`、`Turn`、`Item`、`AgentEvent` 基础领域模型；
- [x] 基础 Item 的 `started/delta/completed/failed/cancelled` 生命周期；
- [x] Agent Loop 状态机和步数、报告 Token、时间、输出预算；
- [x] `ModelProvider` 与只读 `ToolRuntime` 端口；
- [x] SQLite Session Store、Schema v3 和 v1/v2→v3 事务迁移；
- [x] 单调事件序列、CAS 和初始聚合快照；
- [x] Fake Provider、Scripted Provider、Transcript Replay；
- [x] Turn Cancel Token 和基础结构化错误；
- [x] Agent/Action TraceContext 与关联 ID 映射；
- [x] 持久审批等待、答复、取消和恢复（可信只读工具）；
- [x] Plan/Compaction/Error 语义 Item、生命周期与统一错误契约；
- [x] 0.3 范围 Agent OTel Trace/Metrics、跨暂停片段关联和导出故障隔离；
- [x] SessionStore 共享契约套件与损坏、不可写、磁盘满等存储故障场景。

### 关键测试

- [x] 单轮无工具响应；
- [x] 多次只读工具调用后完成；
- [x] 重复、缺失、乱序 Tool Result 被拒绝；
- [x] 达到最大步数和预算后确定性终止；
- [x] 模型流和只读工具执行阶段取消、清理；
- [x] 等待审批阶段取消；
- [x] 7 个关键边界的真实子进程退出与无重复恢复；
- [x] 审批请求/决定事务、消费边界、执行前后等 10 个真实进程退出场景；
- [x] 9 个语义 Item 提交崩溃边界与存储故障矩阵；
- [x] 真实 0.3.1/0.3.2 Transcript 的旧 Schema 迁移与混合版本 Replay。

### 验收标准

- 不连接外部 API 即可重放完整 Turn；
- 进程异常退出后不存在无法解释的“仍在运行”状态；
- 同一 Transcript 重放得到相同的状态和客户端事件序列；
- Runtime 不导入任何具体 Provider SDK。

## 6. 0.4：Model Runtime

状态：**0.4.1 / 0.4.2a / 0.4.2b1/b2 / 0.4.3a/b1/b2 已完成离线验收，整体 0.4 进行中**。已实现双 Adapter、尝试账本、SDK 用量/计费元数据映射、显式价格绑定的成本报告和受控 Smoke/白名单诊断；百炼文本/内存工具/审批重开已实测通过，计价适用性仍待单次授权。按用户继续后续阶段的要求，独立推进 0.5 离线开发，不将 0.4 标记完成。见 [0.4 实施计划](m04-model-runtime.md)。

### 目标

接入真实模型，但不让供应商协议污染 Agent Runtime。

### 核心交付

- [x] OpenAI-compatible Provider（Chat Completions；离线 SDK/HTTP 契约通过）；
- [x] Anthropic Provider（非 Thinking 的 Messages 配置，离线验收）；
- [x] 文本、Tool Call、Usage 总量/明细/失败观测、Stop Reason 流式事件归一化；
- [x] 当前支持配置的工具/并行/流式 Usage 能力描述；
- [x] 模型配置、认证和 Secret 环境引用；
- [x] 限流、超时、错误归一化和首事件前有限退避；
- [x] 请求取消与连接清理（含 HTTP 错误 body）；
- [x] 0.4.2b1：尝试账本、未知/部分/完整累计用量、预算去重、取消/恢复和 v4 迁移；
- [x] 0.4.2b2：两个实际 SDK 发出尝试元数据，映射缓存/推理明细及失败用量；
- [x] Token 统计与 0.4.3a 显式价格绑定后的事后成本估算（不是实时预算硬上限或供应商账单）；
- [x] 0.4.3b1：受控 Smoke 与白名单请求诊断，固定文本/内存工具/审批重开三场景；
- [x] 0.4.3b2：原生响应计费元数据、持久身份与价格绑定冲突验证；不自动推断代理平台计价规则。

### 关键测试

- [x] 两类 Provider 共用一套 Contract Test；
- [x] 分段 Tool Call 参数正确组装；
- [x] 流中断、限流、认证失败、上下文超限分类正确（普通 400 不猜测为超限）；
- [x] Retry 不重复提交已经交给 Tool Runtime 的调用；
- [x] 普通认证/错误路径的 Session 与诊断 canary 验证；Smoke 不输出任意语义内容，不承诺对恶意供应商反射做通用 DLP；
- [x] 显式启用的 Smoke 入口与默认离线 CI 分离；
- [ ] 0.4.3c：真实 API、Usage/计费上下文与脱敏证据验收；百炼文本/内存工具/审批重开已通过，计价适用性未收口，见 [验证记录](validation/bailian-2026-09-03.md)。

### 验收标准

- 同一个 Agent Runtime 测试场景可以切换 Provider；
- Provider 退出、超时和取消不泄漏连接与任务；
- CI 不依赖真实 API Key。

## 7. 0.5：Coding Tool Runtime

状态：**0.5.1 / 0.5.2 与 0.5.3a/b1/b2a/b2b 已完成范围内验收，整体 0.5 进行中**。已有只读工具、可信作用域、事务 Artifact、完整 Patch 计划、受管私有副本中的持久审批/写意图/单文件修改/崩溃核对，以及调用绑定的宿主异步桥接。已接通单文件模型 Patch 的离线 SDK 闭环，仍无 Shell 或完整 Coding Eval。0.4.3c 计价证据独立待验收，不阻塞离线开发。具体边界见 [0.5 实施设计](m05-coding-tools.md)、[ADR 0027](adr/0027-prepared-patch-and-write-admission.md)、[ADR 0028](adr/0028-managed-patch-execution.md) 和 [ADR 0029](adr/0029-managed-patch-agent-bridge.md)。0.5.3b2b 的 Agent v6 / migration 7、独立写审批、专用端口与双账本恢复见 [ADR 0030](adr/0030-kernel-managed-patch-admission.md)。0.5.3c 已完成 c1 只读整组计划与有界结构化 Diff；c2a/c2b 已交付整组事务预留/审批、顺序一次性执行和部分/未知效果核对，c3a 已实现宿主整组调用桥接，下一片为 **c3b Kernel/模型接入**，随后 c3c Diff Artifact。见 [ADR 0031](adr/0031-patch-batches-and-structured-diff.md)。不把逐文件替换冒充整体原子提交，不提前接入 Shell 或源目录自动合入。

### 目标

形成第一个完整的真实编码闭环，而不是添加互不关联的工具 Demo。

### 核心交付

- [ ] Tool Contract、Registry、风险和并发元数据；
- [x] 文件读取、搜索与有界 JSONL 输出管理（进程日志留到 0.5.4）；
  - [x] 0.5.1：`list_files` / `read_file`，根/规则持久绑定、严格参数/输出、分页失效、取消回收；
  - [x] 0.5.2：`glob` / `grep` 与输出 Artifact；
    - [x] 0.5.2a：有界通配/字面量搜索、显式缺口、搜索→revision 读取、审批和中断恢复；
    - [x] 0.5.2b：可信执行作用域、私有 Artifact 归属/配额/原子发布/过期及孤儿恢复；
      - [x] 0.5.2b1：显式 Scoped 端口、持久调用归属、严格工作区绑定、旧审批兼容与并发/取消/恢复验证；
      - [x] 0.5.2b2：同库正文/manifest/ToolResult 原子提交，受控分页、配额、过期清理、未提交回滚与崩溃不重搜；
- [ ] `apply_patch` 和结构化 Patch Result；
  - [x] 0.5.3a：宿主只读准备、完整前后镜像摘要、精确非重叠编辑、来源漂移复核；
  - [x] 0.5.3b：受管单文件 Patch 的模型调用闭环；
    - [x] 0.5.3b1：私有副本工厂、持久计划/审批/意图、单文件替换、取消/崩溃观察和源目录只读；
    - [x] 0.5.3b2：Agent 写审批契约升级、Scoped 准入、模型工具接入、双账本边界和 Kernel 恢复；
      - [x] 0.5.3b2a：稳定调用/计划绑定、宿主审批桥接、私有证据分离、异步取消排空、只读恢复和桥接崩溃矩阵；
      - [x] 0.5.3b2b：版本化写审批/恢复事件、最低 reader 迁移、专用 Kernel 端口、SDK 离线闭环与 Session × 副本组合恢复；
        - KWP-01～10 对应实现与证据见 [ADR 0030](adr/0030-kernel-managed-patch-admission.md) 和 [验收记录第 22 节](testing-and-evals.md#22-053b2b-kernel-受管写闭环验收2026-09-04)；默认仍只读，显式开启后仅受管副本单文件可写。
  - [ ] 0.5.3c：多文件部分效果与结构化 Diff 交付（c1/c2/c3a 已完成，c3b/c3c 待开发）；
    - [x] 0.5.3c1：有序唯一整组提案、不可变计划/整体复核、有界 UTF-8 字节坐标 Diff、四份独立 Schema；仅宿主只读；
    - [x] 0.5.3c2：整组持久预留/审批、逐文件一次性消费、部分/未知效果、取消与崩溃只核对；
      - [x] 0.5.3c2a：整组事务预留、不可变审批绑定、持久决定、旧接口禁止拆分消费、账本 v2 迁移及真实旧 wheel 验收；
      - [x] 0.5.3c2b：整组消费/逐成员执行、部分/未知效果、只核对恢复与每成员写崩溃矩阵；
    - [ ] 0.5.3c3：Kernel 整组审批/结果兼容、模型工具闭环、Diff Artifact 归属与旧会话升级；
      - [x] 0.5.3c3a：独立整组调用契约与宿主异步桥接、完整批准绑定、取消排空/只核对恢复；不接入模型；
      - [ ] 0.5.3c3b：Kernel 专用组端口与持久审批/结果、Session reader 升级、双 SDK 离线及双账本崩溃闭环；
      - [ ] 0.5.3c3c：真实调用归属的计划/历史效果 Diff Artifact、事务发布、预算/分页/过期及恢复；
- [ ] `shell` 的非交互执行；
- [ ] `git_status`、`git_diff`；
- [ ] `run_tests`；
- [x] 有界搜索输出截断、事务归档引用和过期清理；Process 双流捕获尚未实现；
- [ ] 只读并发、写操作互斥和 Turn 取消；
- [ ] 统一 Tool Error Taxonomy；
- [ ] 变更摘要和最终 Diff 交付。

### 关键测试

- [ ] UTF-8、二进制、大文件、长行、空文件和符号链接；
- [ ] Patch 上下文漂移、部分失败和重复应用；
- [ ] Shell 超时、超大输出、非零退出和进程树终止；
- [ ] 脏工作区中不覆盖用户已有修改；
- [ ] 并发读与写互斥行为确定；
- [ ] 从失败测试到修复通过的端到端任务。

### 验收标准

- Agent 能在一个非示例仓库中自主定位并修复受控缺陷；
- 最终回答与实际 Git Diff、测试结果一致；
- 失败不会留下半写文件或孤儿进程；
- 所有工具都有参数、结果、错误和取消契约。

## 8. 0.6：Context Engine 与持久会话

### 目标

支持长任务、多轮会话和可解释的上下文管理。

### 核心交付

- [ ] 系统指令、用户指令、项目指令的优先级；
- [ ] Workspace/Git/环境 Context Fragment；
- [ ] Token Budget；
- [ ] Tool Result 裁剪和完整结果引用；
- [ ] 自动 Compaction；
- [ ] Compaction Summary 的版本和持久化；
- [ ] Session Resume、Fork 和 Archive；
- [ ] Turn Retry 与 Interrupted Recovery；
- [ ] Context Inspect 诊断输出；
- [ ] 历史规范化和 Provider 切换兼容。

### 关键测试

- [ ] 指令优先级和冲突；
- [ ] 接近模型上下文上限时自动压缩；
- [ ] 压缩前后关键任务约束不丢失；
- [ ] 恢复后 Tool Call/Result 仍正确配对；
- [ ] Provider 切换后的历史格式正确；
- [ ] 长输出不会无限增长 Session 数据库。

### 验收标准

- 长任务不依赖手工清理上下文；
- 用户能够查看 Context 主要来源和压缩记录；
- Resume/Fork 不重复已完成的副作用。

## 9. 0.7：安全执行与 Action Plane 集成

### 目标

把“提示模型谨慎”升级为可执行的权限、隔离和副作用治理边界。

### 核心交付

- [ ] Workspace 路径、符号链接和外部目录策略；
- [ ] Permission Rule 与命令风险分类；
- [ ] 审批指纹绑定 Tool、参数、cwd、环境摘要和策略版本；
- [ ] Host Executor 安全级别；
- [ ] Container Sandbox Executor；
- [ ] 网络出口域名/IP/端口策略；
- [ ] Secret Provider 与最小化注入；
- [ ] 文件和命令审计记录；
- [ ] Coding Tool 与 Action Plane 风险路由；
- [ ] 外部副作用 `UNKNOWN → reconcile`；
- [ ] Threat Model v2。

### 关键测试

- [ ] `..`、绝对路径、符号链接和竞态路径逃逸；
- [ ] 审批后参数、cwd 或环境变化导致指纹失效；
- [ ] 子进程、后台进程和进程树不能逃逸取消；
- [ ] 禁止网络时 DNS、IPv4/IPv6 和代理路径都受控；
- [ ] Secret 不出现在模型 Context、日志、Trace、Diff；
- [ ] 外部写操作结果丢失时不重复执行；
- [ ] Host 和 Container 模式安全声明准确。

### 验收标准

- 未授权工具不能通过扩展、Shell 或路径技巧绕过边界；
- 隔离后端不可用时默认策略明确，不能静默降级；
- 高风险外部 Action 可以恢复和对账。

## 10. 0.8：App Server、MCP 与扩展

### 目标

将 Runtime 从单一 CLI 中解耦，形成可供 TUI、SDK、IDE 和自动化调用的稳定服务边界。

### 核心交付

- [ ] Agent Protocol v1；
- [ ] Thread/Turn/Item 请求、响应和通知；
- [ ] 双向审批、提问和取消；
- [ ] Headless App Server；
- [ ] Python Agent SDK；
- [ ] MCP Client；
- [ ] 可选 MCP Server；
- [ ] 项目指令发现；
- [ ] Skills 渐进加载；
- [ ] 生命周期 Hooks；
- [ ] 扩展 Tool 的 Permission/Sandbox 强制接入。

### 关键测试

- [ ] Protocol Schema 兼容性和未知字段处理；
- [ ] 客户端断线重连和事件续传；
- [ ] 审批请求与对应 Tool Call 不错配；
- [ ] MCP Server 崩溃、超时和 Schema 变化；
- [ ] 恶意 Skill/Hook 不能绕过安全边界；
- [ ] CLI 进程内模式与 App Server 模式行为一致。

### 验收标准

- Headless 客户端能够完整驱动 Coding Turn；
- 客户端断线不导致 Agent 状态损坏；
- 核心 Runtime 不依赖具体 UI。

## 11. 0.9：产品硬化、TUI 与 Evals

### 目标

从“功能完整”进入“可长期使用、可回归、可公开证明质量”的发布候选状态。

### 核心交付

- [ ] 交互式 CLI/TUI；
- [ ] 流式消息、工具进度、Diff、审批和成本展示；
- [ ] 配置诊断、环境检查和错误自助信息；
- [ ] macOS/Linux CI；
- [ ] Unit、Contract、Integration、E2E、Failure Injection 分层；
- [ ] 受控真实仓库 Eval 数据集；
- [ ] 任务成功率、测试通过率、Token、成本、延迟基线；
- [ ] Transcript Regression；
- [ ] 性能和数据库增长基准；
- [ ] 安全测试与依赖扫描；
- [ ] Dogfooding 记录和缺陷清单。

### 验收标准

- 至少覆盖 Bug Fix、Feature、Refactor、Test、Review 五类任务；
- 每次关键变更能够输出与基线对比的 Eval 报告；
- 连续故障注入运行后无 Session 损坏、孤儿进程和重复外部副作用；
- 新用户根据文档可以安装并完成首个真实任务。

## 12. 1.0：生产发布

### 发布范围

- [ ] 稳定的 Agent/Tool/Provider/Protocol v1 契约；
- [ ] OpenAI-compatible 与 Anthropic Provider；
- [ ] 完整 Coding Tool 闭环；
- [ ] 持久 Session、Resume/Fork、Context Compaction；
- [ ] Host/Container 执行、安全策略、审批和 Secret；
- [ ] MCP、项目指令、Skills 和 Hooks；
- [ ] CLI/TUI 与 Headless App Server；
- [ ] Action Plane 外部副作用治理；
- [ ] 可复现 Eval 和质量报告。

### 发布门禁

- [ ] 所有公共 Schema 有版本和兼容策略；
- [ ] 所有数据库变更有向前迁移和回滚说明；
- [ ] macOS/Linux 安装、升级和卸载验证通过；
- [ ] 安全文档、威胁模型和 Secret 处理完成审查；
- [ ] 默认 CI 不依赖外部模型和不稳定网络；
- [ ] 真实 Provider Smoke Test 在受控环境通过；
- [ ] 不存在未分类的高优先级故障恢复缺陷；
- [ ] README 中的功能声明全部有可运行证据；
- [ ] 发布包可复现，包含 Changelog 和迁移说明。

## 13. 1.0 之后

只有 1.0 单 Agent Runtime 稳定后，才评估：

- Subagent、Reviewer 和并行任务；
- IDE/桌面客户端；
- 远程 Sandbox 与云任务；
- 团队策略、多租户和集中审计；
- LSP、代码索引和大型 Monorepo 优化；
- Windows 原生支持；
- Rust Process/Sandbox Sidecar；
- 分布式 Session 和 Agent Worker。

这些能力不能提前侵入 1.0 核心，除非已有真实用户场景和评测数据证明必要。

## 14. 每个迭代的统一完成定义

每个开发项只有同时满足以下条件才能勾选：

1. 需求、边界和失败语义已写入设计或 ADR；
2. 公共输入输出有类型和版本化 Schema；
3. 实现没有绕过既有分层和安全端口；
4. 正常、失败、取消、超时和恢复路径按风险完成测试；
5. 日志、Trace 和指标不包含明文凭据；
6. 数据库变更包含迁移和兼容测试；
7. `make check` 通过；
8. 相关 README、架构、部署和安全文档同步；
9. 至少有一个跨组件集成验证；涉及模型或编码行为的里程碑还需真实 Provider 或真实仓库验证；
10. Git Diff 仅包含该迭代必要变更。
