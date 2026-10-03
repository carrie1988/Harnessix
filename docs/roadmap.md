---
doc_type: roadmap
status: current
version: 165
code_revision: 9b9e52fdaab74567b5ad7cd5614801f1936689bc
owners:
  - core
modules:
  - product
  - documentation
related_adrs:
  - docs/adr/0005-evolve-to-harnessix-code.md
  - docs/adr/0062-local-first-v1-commercial-boundary.md
  - docs/adr/0063-windows-v1-platform-support.md
  - docs/adr/0064-agpl-and-commercial-dual-licensing.md
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
  - docs/adr/0078-product-shell-and-recoverable-client-state.md
  - docs/adr/0080-capability-proven-product-action-composition.md
  - docs/adr/0081-single-coding-agent-product-boundary.md
  - docs/adr/0082-multi-repository-eval-suite-and-transcript-evidence.md
  - docs/adr/0085-versioned-third-party-eval-dataset-and-golden-boundary.md
  - docs/adr/0086-formal-eval-case-adapter-and-recorded-provider-boundary.md
  - docs/adr/0083-built-in-immutable-coding-eval-task-pack.md
  - docs/adr/0084-recoverable-sequential-eval-suite-runner.md
  - docs/adr/0087-deterministic-offline-eval-suite-composition.md
  - docs/adr/0088-controlled-real-provider-suite-baseline.md
  - docs/adr/0089-bounded-local-transport-lifecycle.md
  - docs/adr/0090-plan-first-store-maintenance-and-backup.md
related_tests:
  - tests/product_config/test_coding_workflow_instructions.py
  - tests/product_config/test_product_patch_rollback.py
  - tests/product_config/test_product_rollback_sdk.py
  - tests/product_config/test_product_state_restore.py
  - tests/governance
  - tests/product_ui
  - tests/product_config/test_action_contracts.py
  - tests/product_config/test_action_catalog.py
  - tests/product_config/test_action_config_runtime.py
  - tests/product_config/test_action_runtime.py
  - tests/evals/test_suite_execution.py
  - tests/trusted_actions/test_router.py
  - tests/trusted_actions/test_agent_gateway.py
  - tests/agent/test_trusted_action_runtime.py
  - tests/evals/test_suite.py
  - tests/evals/test_task_pack.py
  - tests/integration/test_task_pack_profiles.py
  - tests/evals/test_task_pack_execution.py
  - tests/evals/test_task_pack_suite.py
  - tests/evals/test_offline_suite_runner.py
  - tests/evals/test_provider_suite_contracts.py
  - tests/evals/test_provider_suite_execution.py
  - tests/evals/test_provider_suite_cli.py
  - tests/evals/test_provider_suite_evidence.py
  - tests/integration/test_task_pack_execution.py
  - tests/app_server/test_server_sdk.py
  - tests/agent/test_store_maintenance.py
  - tests/tools/test_argument_feedback.py
supersedes: []
---

# Harnessix Code 设计与开发路线图

## 1. 路线图原则

Harnessix Code通过研究Codex、OpenCode、Claude Code等主流Coding Agent的架构、公开行为和可核验实现思路，独立设计并实现面向真实软件工程任务的、本地优先、模型无关、安全可控、可恢复、可审计、可评测、可扩展的生产级Coding Agent。系统必须在真实代码仓库中稳定完成理解、规划、修改、执行、验证、审查和交付闭环，并具备完整的协议契约、失败语义、持久化、可观测性、安全边界、兼容升级、真实评测和产品发布能力。

Harnessix Code 1.0不是POC、功能演示或仅供二次开发的Runtime库，而是能够供大量独立macOS、Linux和Windows终端用户安装并长期使用的本地优先正式商用版本。“大量用户”指大量相互独立的本地实例，不表示1.0包含集中式多租户云控制面；远程Sandbox、云任务和分布式Agent Worker按真实需求在1.x评估。产品与平台边界由[ADR 0062](adr/0062-local-first-v1-commercial-boundary.md)和[ADR 0063](adr/0063-windows-v1-platform-support.md)固化。

路线图采用“可发布的纵向切片”，每个切片都必须包含正式契约、失败语义、持久化、可观测性、完备测试、总体方案设计和详细设计。参考项目仅作为架构与行为证据，研究必须记录固定提交或产品版本、来源和Harnessix独立决策；任何代码复用必须满足许可证和归属要求。

社区版自许可证切换边界起按照`AGPL-3.0-only`发布，并保留独立商业授权能力。历史MIT版本、外部贡献、品牌标识和第三方依赖必须分别保留可审计权利链，见[ADR 0064](adr/0064-agpl-and-commercial-dual-licensing.md)。

开发顺序遵循：

```text
源码研究 → 架构决策 → 领域契约 → 最小正式实现
→ 失败与恢复测试 → 真实场景验证 → 文档同步
```

不按功能数量判断完成度。没有取消、超时、恢复、安全边界和完备测试的功能，不得标记为生产完成。
重大变更必须在实现前形成可评审的正式设计，并在合入前同步现行模块设计、源码与测试映射；
文档角色、结构和完成门槛以[文档工程规范](governance/documentation-standard.md)为准。

**首发范围收敛**：0.9～1.0剩余工作以[六个发布工作包与逐项处置](changes/m09-to-v1-release-scope-convergence.md)及[ADR 0106](adr/0106-v1-release-scope-and-risk-based-gates.md)为准。保留完整本地编码产品、三平台原生核心流程、安全/恢复、权利及真实质量；远端MCP/OAuth、公网Git自动Push、通用维护平台、全模型/价格矩阵、自动更新与多安装渠道后置。延期不计作已完成，也不允许已知危险入口继续暴露。

## 2. 历史基线：0.1 Action Plane

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

历史后继阶段已为Trusted Action Runtime补齐Policy/Executor/Reconcile，执行治理收敛为Coding Agent内部能力。
独立Action Plane HTTP/Worker及其运行依赖已删除，本节只说明历史，不构成当前部署或活动任务。

## 3. 里程碑总览

| 版本 | 里程碑 | 核心结果 | 依赖 |
|---|---|---|---|
| 0.2 | 产品与架构基线 | 研究框架、目标架构、协议和状态机决策 | 0.1 |
| 0.3 | Agent Runtime Kernel | 可恢复的 Thread/Turn/Item 与确定性 Agent Loop | 0.2 |
| 0.4 | Model Runtime | 两类 Provider、流式事件、错误和用量归一化 | 0.3 |
| 0.5 | Coding Tool Runtime | 完成读取、搜索、补丁、Shell、Git、测试闭环 | 0.4 运行基线；计价证据独立跟踪 |
| 0.6 | Context 与持久会话 | 指令、预算、压缩、恢复、取消和 Replay | 0.5 |
| 0.7 | 可信执行与工程交付 | 跨平台端口、Permission、Sandbox、Process、事务性交付和Trusted Action Runtime | 0.6 |
| 0.8 | 产品运行时与扩展 | 双向协议、Headless、薄CLI、MCP、Skills、Hooks、Provider/Profile产品配置 | 0.7 |
| 0.9 | Release Candidate与质量工程 | 可维护性和文档工程基线、完整CLI/TUI、三平台发行物、质量/成本基线、安装与Dogfooding | 0.8 |
| 1.0 | 本地优先正式商用发布 | macOS/Linux/Windows稳定契约、升级回滚、安全审查和发布保障 | 0.9 |
| 1.x | 按需求演进 | 云任务、多租户、远程Sandbox、IDE和分布式运行 | 1.0 |

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

状态：**核心运行能力已交付；0.4.3c按首发最小范围并入R3，扩大计价研究延期**。双Adapter、尝试账本、显式价格估算及固定Smoke已经实现；旧计价验证记录保留。1.0必须验证正式认证配置的真实功能、Usage、凭据保护与估算适用边界，但不以全地域/服务等级价格矩阵或供应商账单对账为前置条件。未知价格不能报零或冒充人民币硬预算。历史实现见[0.4实施计划](m04-model-runtime.md)，当前发布条件见[R3](changes/m09-to-v1-release-scope-convergence.md#5-六个发布工作包与退出条件)。

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
- [ ] 0.4.3c首发边界（R3）：有限认证配置的真实API、Usage、脱敏与估算适用性；全计价组合延期。旧[验证记录](validation/bailian-2026-09-03.md)保留，不因范围修改自动通过。

### 验收标准

- 同一个 Agent Runtime 测试场景可以切换 Provider；
- Provider 退出、超时和取消不泄漏连接与任务；
- CI 不依赖真实 API Key。

## 7. 0.5：Coding Tool Runtime

状态：**已完成（2026-09-07）**。0.5.1–0.5.6的实现、本地验收和[CI 34083177442](https://github.com/carrie1988/Harnessix/actions/runs/34083177442)均通过。任务v3真实Campaign在固定历史缺陷上3/3严格通过，0.5.5d把通过结果转换为私有单文件变更包，并以来源/干净状态/前镜像复核、批准指纹、原子替换和崩溃核对完成显式工作树合入；0.5.6补齐Tool并发契约、连续只读有界调度、写/审批屏障、失败排空和统一错误类别。见[0.5 实施设计](m05-coding-tools.md)、[ADR 0052](adr/0052-controlled-eval-change-delivery.md)、[ADR 0053](adr/0053-tool-concurrency-and-error-taxonomy.md)和[任务v3真实基线](validation/bailian-2026-09-06-coding-eval-v3/README.md)。非交互命令由结构化`host.process`实现，不开放任意Shell字符串；通用多文件交付、自动commit/push和OS Sandbox属于后续版本。

### 目标

形成第一个完整的真实编码闭环，而不是添加互不关联的工具 Demo。

### 核心交付

- [x] Tool Contract、Registry、风险和并发元数据；
- [x] 文件读取、搜索与有界 JSONL 输出管理（Process已捕获日志由0.5.4b2c2补齐）；
  - [x] 0.5.1：`list_files` / `read_file`，根/规则持久绑定、严格参数/输出、分页失效、取消回收；
  - [x] 0.5.2：`glob` / `grep` 与输出 Artifact；
    - [x] 0.5.2a：有界通配/字面量搜索、显式缺口、搜索→revision 读取、审批和中断恢复；
    - [x] 0.5.2b：可信执行作用域、私有 Artifact 归属/配额/原子发布/过期及孤儿恢复；
      - [x] 0.5.2b1：显式 Scoped 端口、持久调用归属、严格工作区绑定、旧审批兼容与并发/取消/恢复验证；
      - [x] 0.5.2b2：同库正文/manifest/ToolResult 原子提交，受控分页、配额、过期清理、未提交回滚与崩溃不重搜；
- [x] `apply_patch` 和结构化 Patch Result；
  - [x] 0.5.3a：宿主只读准备、完整前后镜像摘要、精确非重叠编辑、来源漂移复核；
  - [x] 0.5.3b：受管单文件 Patch 的模型调用闭环；
    - [x] 0.5.3b1：私有副本工厂、持久计划/审批/意图、单文件替换、取消/崩溃观察和源目录只读；
    - [x] 0.5.3b2：Agent 写审批契约升级、Scoped 准入、模型工具接入、双账本边界和 Kernel 恢复；
      - [x] 0.5.3b2a：稳定调用/计划绑定、宿主审批桥接、私有证据分离、异步取消排空、只读恢复和桥接崩溃矩阵；
      - [x] 0.5.3b2b：版本化写审批/恢复事件、最低 reader 迁移、专用 Kernel 端口、SDK 离线闭环与 Session × 副本组合恢复；
        - KWP-01～10 对应实现与证据见 [ADR 0030](adr/0030-kernel-managed-patch-admission.md) 和 [验收记录第 22 节](testing-and-evals-milestone-history.md#22-053b2b-kernel-受管写闭环验收2026-09-04)；默认仍只读，显式开启后仅受管副本单文件可写。
  - [x] 0.5.3c：多文件部分效果与结构化 Diff 交付（c1/c2/c3a/c3b/c3c1/c3c2 已完成范围内验收）；
    - [x] 0.5.3c1：有序唯一整组提案、不可变计划/整体复核、有界 UTF-8 字节坐标 Diff、四份独立 Schema；仅宿主只读；
    - [x] 0.5.3c2：整组持久预留/审批、逐文件一次性消费、部分/未知效果、取消与崩溃只核对；
      - [x] 0.5.3c2a：整组事务预留、不可变审批绑定、持久决定、旧接口禁止拆分消费、账本 v2 迁移及真实旧 wheel 验收；
      - [x] 0.5.3c2b：整组消费/逐成员执行、部分/未知效果、只核对恢复与每成员写崩溃矩阵；
    - [x] 0.5.3c3：Kernel 整组审批/结果兼容、模型工具闭环、Diff Artifact 归属与旧会话升级；
      - [x] 0.5.3c3a：独立整组调用契约与宿主异步桥接、完整批准绑定、取消排空/只核对恢复；不接入模型；
      - [x] 0.5.3c3b：Kernel 专用组端口与持久审批/结果、Session reader 升级、双 SDK 离线及双账本崩溃闭环；
      - [x] 0.5.3c3c：真实调用归属的计划/历史效果 Diff Artifact、事务发布、预算/分页/过期及恢复；
        - [x] c3c1：完整调用/组账本绑定的计划与历史效果报告，有界 JSONL、全部成员说明、取消排空及只读重开；
        - [x] c3c2：计划/效果独立引用与真实 Session 事实同事务发布、reader 兼容升级、分页/配额/过期及失败恢复；
- [x] 非交互命令执行（结构化`host.process`支持宿主预绑定程序与模型argv；默认不开放，拒绝任意Shell字符串）；
  - [x] 0.5.4a：受信宿主程序绑定、argv/环境准入、双流有界捕获、组终止、取消/关闭与管道回收；不注册模型工具；
  - [x] 0.5.4b：持久命令意图/审批/结果、当前范围宿主死亡处理及安全恢复，不按历史PID自动杀进程或重放命令；
    - [x] b1：复用Action Plane持久意图/审批/租约/UNKNOWN，绑定宿主执行权限，硬退出不杀旧PID或重放；不接模型；
    - [x] b2：Agent Session与Action Plane的单一审批绑定、长输出Artifact及当前范围宿主死亡恢复；
      - [x] b2a：冻结Action审批唯一权威、稳定Action身份、跨库恢复Saga、WAITING_ACTION与Process Artifact边界；
      - [x] b2b：桥接契约、Agent事件/Session迁移及旧reader兼容；
        - [x] b2b1：稳定调用/Action身份、确定性Action ID与幂等键、持久ToolDescriptor/宿主绑定核对及冻结计划Schema；不接Session或执行；
        - [x] b2b2：Agent审批/等待/结果投影事件、Session migration10及真实v8旧reader升级；
          - [x] b2b2a：Agent Event/Thread v9、Process审批/状态/结果私有投影、持久WAITING_ACTION、纯Reducer/Replay与migration10；Runtime只保留等待，不执行Action；
          - [x] b2b2b：用真实`e0e8498` v8 wheel生成/升级会话，验证旧事件原字节、旧reader拒绝及migration10提交前后硬退出；
      - [x] b2c：Agent Runtime执行/恢复、Process Artifact与双SDK离线闭环；
        - [x] b2c1：显式Process Agent端口、稳定Action准备/唯一决定、外部Worker、WAITING_ACTION单次观察与有界模型结果；
        - [x] b2c2：Process stdout/stderr Artifact事务发布、配额/分页/TTL、损坏恢复、migration11及提交窗口硬退出；
        - [x] b2c3：Session×Action真硬退出矩阵、等待取消/时限/关闭、跨进程决定、租约UNKNOWN和双SDK离线闭环；
  - [x] 0.5.4c：在上述准入上接入固定Git读取和宿主预注册`run_tests`，完成失败→修复→通过→Diff反馈；任意Shell仍关闭；
- [x] `git_status`、`git_diff`（显式Git绑定、固定命令/config、精确仓库根和有界UTF-8结果）；
- [x] `run_tests`（模型只选Profile，固定argv进入原Process Action审批/Worker链路）；
- [x] 有界搜索/Process输出截断、事务归档引用和过期清理；Process Artifact不对Action已捕获前缀做第二次隐藏截断；
- [x] 只读并发、写/审批顺序屏障和 Turn 取消（单Runtime范围；跨进程锁属于0.7）；
- [x] 统一 Tool Error Taxonomy；
- [x] 0.5.4c闭环中的变更摘要和最终Diff读取；0.5.5d已补显式单文件工作树合入，commit/push和通用发布仍待后续。
- [x] 0.5.5真实缺陷Coding Eval与变更交付；
  - [x] 0.5.5a：版本化任务/检查/Git/最终回答/报告契约、无Golden Patch评分器及原子脱敏报告；
  - [x] 0.5.5b：首个Harnessix历史真实缺陷物化、隐藏检查和同一Runtime/Worker驱动；
    - [x] 0.5.5b1：固定真实历史来源、单提交私有物化、ready清单和宿主隐藏检查；
    - [x] 0.5.5b2：复用同一Agent Runtime、Process/Patch审批和外部Worker完成端到端运行与评分；
  - [x] 0.5.5c：显式真实Provider多次试验、成本/时延和失败分类基线；
    - [x] 0.5.5c1：请求前Campaign计划、独立运行证据核对、失败分类及Token/时延/成本聚合；
    - [x] 0.5.5c2：受控真实Campaign执行与首轮多次基线；
      - [x] 0.5.5c2a：默认禁网CLI、私有配置、单宿主锁、持久进度、试验间费用停止及崩溃恢复；
      - [x] 0.5.5c2b：百炼北京精确模型三次独立试验、人民币10元停止线及脱敏基线归档；结果0/3，主分类均为预算失败；
    - [x] 0.5.5c3：真实预算、工具自纠正适用性与可比较重基线；
      - [x] 0.5.5c3a：源码求证、真实Token开销分析、任务版本升级、预算临界可观测性及离线回归；
      - [x] 0.5.5c3b：新授权边界内完成同模型任务v2三次独立Campaign；结果0/3均为预算终态，定位到分页缺少`expected_revision`时的通用错误不可自纠正；
      - [x] 0.5.5c3c：源码求证、稳定校验错误契约、有界模型反馈、持久化/双SDK/恢复回归和离线模型纠正闭环；
      - [x] 0.5.5c3d：完成分页纠正后同模型三次独立Campaign；纠正采用率3/3、代码闭环2/3，同时定位最终回答Schema未进入模型上下文；
      - [x] 0.5.5c3e：公开最终回答契约并形成可比较任务v3基线；
        - [x] 0.5.5c3e1：任务v3显式裸JSON Schema、旧版本兼容、严格评分与离线回归；
        - [x] 0.5.5c3e2：使用新Campaign完成任务v3三次真实试验；严格通过3/3，费用¥0.828428，完整脱敏证据已归档；
  - [x] 0.5.5d：受控单文件变更包、来源漂移/脏工作区冲突、批准绑定、崩溃核对和显式工作树合入。
- [x] 0.5.6 Tool Contract收口：并发能力默认关闭且仅限只读，连续安全前缀有界执行、Provider顺序提交、失败快停/排空、工具域错误统一分类和兼容部署。

### 关键测试

- [x] UTF-8、二进制、大文件、长行、空文件和符号链接；
- [x] Patch 上下文漂移、部分失败和重复应用；
- [x] Process超时、超大输出、非零退出和进程树终止；
- [x] 脏工作区中不覆盖用户已有修改；
- [x] 并发读与写/审批屏障行为确定；
- [x] 从失败测试到修复通过的端到端任务。

### 验收标准

- Agent 能在一个非示例仓库中自主定位并修复受控缺陷；
- 最终回答与实际 Git Diff、测试结果一致；
- 已承诺的写入、超时、取消和关闭路径不会留下半写文件或同组孤儿进程；宿主硬退出进入`unknown`且不误报已停止，跨宿主监督属于0.7；
- 所有工具都有参数、结果、错误和取消契约。

## 8. 0.6：Context Engine 与持久会话

状态：**已完成**。0.6.1已完成固定指令优先级、供应商中立输入预算、双Provider映射、Event v10持久检查记录及Context Inspect；0.6.2a已完成受控项目指令发现、异步Source端口、每步freshness、Context Inspection v2和Event/Thread v11，并通过远端[CI 34104413651](https://github.com/carrie1988/Harnessix/actions/runs/34104413651)四矩阵验收；0.6.2b已实现Workspace/Git/环境Source、乐观双观测、Context Inspection v3、Event/Thread v12和Session migration14。0.6.2c已完成Tool Result稳定模型视图、完整Artifact覆盖证明、Event/Thread v13和Session migration15，并通过[CI 34173011955](https://github.com/carrie1988/Harnessix/actions/runs/34173011955)四矩阵验收。0.6.3已完成[Compaction源码研究](research/compaction-and-context-windows.md)、[窗口规划](compaction-window-planning.md)、[独立摘要账本](compaction-attempt-ledger.md)及[自动Compaction运行时与活动窗口](compaction-runtime-and-windows.md)，并通过[CI 34183895692](https://github.com/carrie1988/Harnessix/actions/runs/34183895692)四矩阵验收。0.6.4已完成[Thread生命周期源码研究](research/thread-lifecycle-and-fork.md)、[ADR 0060](adr/0060-thread-lifecycle-and-authority-free-forks.md)及[Resume/Fork/Archive详细设计](thread-lifecycle.md)，并通过[CI 34188329001](https://github.com/carrie1988/Harnessix/actions/runs/34188329001)四矩阵验收。0.6.5已完成[Retry与Provider切换源码研究](research/turn-retry-and-provider-switch.md)、[ADR 0061](adr/0061-terminal-turn-retry-and-provider-neutral-history.md)、[详细设计](turn-retry-and-provider-switch.md)及对应实现；该切片交付时Event/Thread为v17、Session migration为20，本地严格验收及[CI 34192389373](https://github.com/carrie1988/Harnessix/actions/runs/34192389373)四矩阵全部通过。当前版本号以[总体架构](architecture.md)为准。见[详细实施设计](m06-context-and-sessions.md)。

### 目标

支持长任务、多轮会话和可解释的上下文管理。

### 核心交付

- [x] 系统指令、用户指令、项目指令的优先级；
- [x] 受控项目指令发现、Source freshness和无正文持久快照；
- [x] Workspace/Git/环境 Context Fragment及跨来源有界一致性；
- [x] Token Budget（供应商中立输入门禁与自动压缩；精确Tokenizer仍属后续优化）；
- [x] Tool Result 裁剪和完整结果引用（0.6.2c稳定视图与Artifact覆盖校验）；
- [x] 自动 Compaction；
- [x] Compaction Summary 的版本和持久化；
- [x] Session Resume、Fork 和 Archive；
- [x] Turn Retry 与 Interrupted Recovery；
- [x] Context Inspect 诊断输出；
- [x] 历史规范化和 Provider 切换兼容。

### 关键测试

- [x] 指令优先级、稳定排序和结构边界冲突；
- [x] 项目指令层级/override、缺失/失败、超时/取消、更新和Replay；
- [x] Workspace/Git/环境边界、非仓库语义、allowlist、跨Source漂移和v12 Replay；
- [x] 接近模型上下文上限时自动压缩；
- [x] 压缩前后关键任务约束不丢失；
- [x] 恢复后 Tool Call/Result 仍正确配对；
- [x] Provider 切换后的历史格式正确；
- [x] 重复压缩后模型可见历史保持有界，原Session事实按审计策略保留。

### 验收标准

- 长任务不依赖手工清理上下文；
- 用户能够查看 Context 主要来源和压缩记录；
- Resume/Fork 不重复已完成的副作用。

## 9. 0.7：可信执行与工程交付

状态：**已完成（2026-09-09）**。0.7.0～0.7.5的源码研究、正式契约、实现、失败恢复、安全攻击、真实仓库验证和文档已经交付；最终实现、Windows Snapshot稳定性及Container冷启动探测加固由[CI 34268017600](https://github.com/carrie1988/Harnessix/actions/runs/34268017600)完成Python 3.12/3.13、macOS、Windows、PostgreSQL和固定摘要真实Container六矩阵验收。0.7 Action入口当前是进程内宿主API；Agent Protocol、MCP/Skill/Hook产品接线、完整CLI/TUI、公网Git凭据装配和发行物仍属于0.8/0.9。

### 目标

把“提示模型谨慎”和0.5的受控工具闭环升级为可执行的权限、隔离、通用工程命令、多文件事务交付和副作用治理边界，使Agent能够安全完成真实项目，而不是依赖每个仓库预注册固定命令。

### 纵向切片

- [x] **0.7.0 研究基线与生产差距**：刷新Codex、OpenCode和Claude Code行为研究版本；求证POSIX/Windows路径、进程、Sandbox和发行接口；输出安全执行、进程、工作区交付差距矩阵；完成Threat Model v2；明确0.4.3c由0.9发布证据门禁收口；
- [x] **0.7.1 跨平台Workspace与Permission**：建立Workspace平台端口；统一POSIX路径以及Windows盘符、UNC、保留名、ADS、大小写折叠、长路径、Reparse Point/Junction、外部目录和跨进程所有权；审批指纹绑定Tool、参数、cwd、环境摘要、Workspace revision和策略版本；
- [x] **0.7.2 Sandbox、网络与Secret**：定义三平台Host安全级别和失败关闭策略；实现Container Sandbox执行适配、资源限制、网络出口域名/IP/端口策略、代理防绕过及Secret Provider最小化注入；Windows强隔离优先采用受管WSL2或Docker Desktop后端；通用spawn/回收由0.7.3接入；
- [x] **0.7.3 跨平台Process与终端监督**：在Permission和Sandbox内提供通用argv/受控Shell、PTY、标准输入、后台进程、超时、取消、宿主死亡监督和有界输出Artifact；POSIX使用Session/Process Group，Windows使用Job Object等原生归属能力；是否引入Rust Sidecar由基准和失败测试决定；
- [x] **0.7.4 事务性交付与Git闭环**：把受管副本扩展为通用多文件Workspace事务，支持来源CAS、脏工作区保护、完整Diff、Checkpoint、Rollback、Branch/Worktree和显式Commit；Push始终单独授权且默认关闭；
- [x] **0.7.5 Action Plane与安全验收**：统一Coding Tool风险路由、文件/命令审计、外部副作用`UNKNOWN → reconcile`和扩展强制接入点，完成真实仓库、安全攻击、崩溃恢复及跨组件发布门禁。

### 关键测试

- [x] POSIX的`..`、绝对路径、符号链接、挂载点，以及Windows盘符、UNC、ADS、保留名、Junction/Reparse Point和检查后替换的竞态逃逸；
- [x] 审批后参数、cwd、环境、Workspace revision或策略变化导致授权失效；
- [x] POSIX Process Group和Windows Job Object下的子进程、PTY、后台进程和进程树在取消、超时及宿主崩溃后进入可核对状态；
- [x] 禁止网络时DNS、IPv4/IPv6、代理、重定向和解析漂移均受策略约束；
- [x] Secret不出现在模型Context、Session、日志、Trace、Diff或诊断包；
- [x] 多文件写入、Checkpoint、Commit各崩溃切点不产生未归因交付或盲目重放；
- [x] 外部写操作结果丢失时不重复执行，并能够通过Reconcile结束；
- [x] Host和Container模式的实际隔离能力、降级和不可用声明准确；
- [x] 每个切片至少完成一个真实仓库任务和对应的确定性回归。

### 验收标准

- Agent能够在明确Permission和Sandbox边界内执行真实项目命令，不要求为每条命令硬编码Profile；
- 未授权工具不能通过扩展、Shell、PTY、路径或网络技巧绕过边界；
- 隔离后端不可用时失败关闭或明确要求用户选择Host风险，不静默降级；
- 多文件修改能够原子交付或恢复到可核对状态，Git结果与最终回答一致；
- 高风险外部Action可以恢复和对账，连续故障测试不产生重复副作用或失管进程。
- Windows原生Workspace、Git和Process通过正式契约；仅WSL2可运行不能标记为Windows原生支持。CLI通过0.8的Protocol/Headless边界接入，不能由0.7执行端口推导为已完成。

## 10. 0.8：产品运行时与扩展

状态：**已完成（2026-09-09）**。0.8.1～0.8.6的源码研究、架构决策、正式契约、实现、失败恢复、安全测试、产品装配和中文文档均已完成；最终实现及Eval低速物化加固提交`3588d76`由[CI 34351402193](https://github.com/carrie1988/Harnessix/actions/runs/34351402193)完成Python 3.12/3.13、macOS、Windows、PostgreSQL和固定摘要Container六矩阵验收。所有客户端、MCP、Skill、Hook和Provider配置只能通过同一Runtime、Permission、Secret及Sandbox边界工作。

### 目标

将Runtime从进程内调用解耦为稳定的本地产品服务，使CLI、TUI、SDK和自动化客户端能够断线恢复、持续交互和安全扩展，而不复制Agent状态机。

### 纵向切片

- [x] **0.8.1 Agent Protocol v1**：版本化Command/Query/Event、Thread/Turn/Item、事件游标、幂等请求、错误、未知字段和兼容策略；
- [x] **0.8.2 Headless App Server与Agent SDK**：stdio JSONL基线、本地调用身份、Workspace绑定、优雅关闭、断线重连、事件续传、背压和Python Agent SDK；
- [x] **0.8.3 薄CLI与双向交互**：创建/恢复/分叉/归档、流式文本、计划与工具进度、审批、提问、取消、运行中Steering及Diff确认；完整TUI视觉和发布体验留给0.9；
- [x] **0.8.4 MCP**：MCP Client、可选MCP Server、进程生命周期、能力快照、Schema漂移和所有Tool的Permission/Sandbox强制接入；
- [x] **0.8.5 Skills与Hooks**：来源、版本、渐进加载、生命周期Hook、冲突、超时、取消和供应链信任边界；
- [x] **0.8.6 Provider与配置产品化**：模型/Profile选择、能力诊断、Secret引用、配置迁移和安全切换；任何自动Fallback不得跨越已经暴露模型输出或工具调用的边界。

### 关键测试

- [x] Protocol Schema兼容、旧客户端、未知字段和重复Command；
- [x] 客户端在任意事件前后断线，重连后不丢事件、不重复审批和副作用；
- [x] Steering、审批、提问、取消与对应Turn/Tool Call不会错配；
- [x] 慢客户端、背压、服务端重启和同时关闭不会损坏Session；
- [x] MCP Server崩溃、超时、Schema变化和恶意Tool描述失败关闭；
- [x] 恶意Skill/Hook不能读取未授权Secret或绕过Tool/Sandbox；
- [x] 进程内、Headless和薄CLI模式对同一Transcript产生等价领域结果。
- [x] 配置重复键、链接、错版Secret、能力不足、Fallback环、迁移崩溃和活动CAS失败关闭；
- [x] 零暴露可重试失败仅在审计成功后切换，响应/文本/Tool Call暴露后绝不自动Fallback；
- [x] OpenAI-compatible与Anthropic Adapter显式Secret注入、固定Workspace产品启动和EOF关闭均通过离线真实SDK路径。

### 验收标准

- Headless和薄CLI客户端能够完整驱动并恢复一个真实Coding Turn；
- 客户端断线、服务端重启和扩展故障不导致Agent状态损坏；
- 核心Runtime不依赖具体UI，扩展不拥有额外执行权限；
- 公共协议、SDK和配置均有版本、升级及错误诊断。

## 11. 0.9：Release Candidate与质量工程

状态：**0.9.0～0.9.3已完成；0.9.4～0.9.6按收敛范围继续，整体0.9未完成**。
DOC-1文档治理与三平台0.9.3d Soak保持关闭；既有真实编码[0/20基线](validation/provider-engineering-2026-09-20-v1/README.md)和当前许可/Windows失败保持原判定。范围缩减不是验收通过。
后续执行以R1～R6为活动队列，不根据附录中的历史开放项恢复已延期功能。

### 目标

从“功能完整”进入“代码可长期维护、真实用户可长期使用、问题可诊断、质量可回归、发布声明有证据”的候选版本状态。

### 纵向切片

- [x] **0.9.0 代码可读性、可维护性与结构治理**：建立可复现的源码规模、文档字符串、复杂度、依赖和公共API基线；制定简体中文注释、命名及模块边界规范；按Agent核心状态机、副作用与恢复、产品运行时与扩展、模型/Context/Eval的优先级补齐模块、类、函数和关键不变量说明；在独立ADR和行为保持测试约束下治理超大文件与过重职责；渐进建立新增及变更代码的可读性防退化门禁。不得以机械注释覆盖率替代语义质量，不得把功能开发、公共契约变更或无关重构混入本切片；
- [x] **0.9.1 CLI/TUI产品体验**：完整交互、流式消息、计划、工具进度、Diff、审批、成本、会话管理、配置向导、环境检查和错误自助；完成Windows原生只读Coding Tool Runtime与统一Action的产品装配，不以WSL兼容替代原生端口；a～f全部子切片已经对应全矩阵CI验收；
- [x] **0.9.2 Eval与Transcript基线**：覆盖Bug Fix、Feature、Refactor、Test和Review的多仓库任务集，记录任务成功率、测试通过率、人工干预率、Token、成本和延迟；离线20/20执行链与真实Provider 0/20严格质量基线均已冻结；
- [x] **0.9.3 可靠性与性能**：长会话Soak、进程/数据库/客户端故障注入、并发与锁、内存、启动时延、Artifact和数据库增长基准；
- [ ] **0.9.4 安全、许可证与供应链（R1/R2）**：首发实际可达的公开输出/凭据、Owner/取消/恢复、高风险副作用、许可证/权利链、锁定安装输入、SBOM与发行物扫描；复用现有TM相关测试，不建设重复安全平台；
- [ ] **0.9.5 三平台发行、停机升级与小批Beta（R4/R5）**：统一Wheel通道，全新安装、当前认证候选升级、数据库与Key同机恢复、卸载、低敏诊断、3～5名真实开发者及至少15个任务；公网Git认证和自动更新后置；
- [ ] **0.9.6 真实编码与有限Provider发布证据（R3）**：原0/20归因及主链整改，现有Task Pack固定质量运行；至少一份认证配置的真实功能、Usage、取消、凭据保护与价格适用边界；不扩大模型/地域/计价矩阵。

### 0.9.4收敛后的完成边界

- [ ] **0.9.4a（R1）**：首发装配的模型、Tool/Action、Session/Artifact、Protocol/SDK及诊断入口；当前Key/Store来源认证和同机备份恢复。未知旧历史不补签，危险维护入口须正式拒绝。
- [ ] **0.9.4b（R2）**：处理12件Archive许可复核，补商业权利及实际发行输入；复用现有扫描、库存和SBOM，不放宽现有拒绝策略。
- [ ] **0.9.4c（R1）**：建立首发可达TM风险到既有测试的映射，只补真正缺失的正反例，不另建重复测试平台。
  [控制追踪详设](changes/m09-r1-existing-safety-coverage.md)已建立18组、54个原函数锚点；
  [固定源码本地复验](validation/v1-safety-coverage-2026-09-29-v1/README.md)双Python各146通过/2原生Windows跳过。
  映射及本地组件结果不关闭0.9.4c、R1或同候选原生安全门禁。
- **0.9.4d：延期1.1+**。远端MCP Streamable HTTP/OAuth不再阻断1.0；本地stdio MCP保留，未支持远端配置必须在正式入口拒绝。

当前实现依据：[托管Session Key](changes/m09-4a-managed-session-key-and-root.md)、
[认证SQLite](changes/m09-4a-authenticated-sqlite-session.md)、[Artifact原正文认证](changes/m09-4a-authenticated-artifact-body.md)、
[安全/供应链详设](changes/m09-4-security-and-supply-chain.md)。既有固定验证目录不修改。
安全阶段仅在R1/R2必要门禁通过后关闭；延期能力不能标记为实现完成。

[R1维护安全](changes/m09-r1-store-maintenance-safety.md)补认证容量原证明、认证Store旧式维护拒绝、
候选恢复预检、有界工作线程及取消/确认丢失回归。后继完整备份/恢复和
[规范Wheel三平台实测](validation/canonical-wheel-three-platform-2026-09-29-v1/README.md)
已证明空编码场景六库/原Key恢复及卸载重装；后继[原生复杂业务恢复专项](validation/windows-business-state-recovery-2026-09-29-v1/README.md)
保留原Windows82通过/2失败，并在严格分类及合法错Key修复后取得原生业务步骤成功。
整体安全、认证存储容量及消费者发布仍需验收。
上述专项不标记R1或0.9.4完成，不恢复已延期的通用维护平台。

### 六个剩余发布工作包

| 顺序 | 工作包 | 明确完成结果 |
|---|---|---|
| R1 | 核心安全与恢复收口 | 正式入口安全；取消/超时及恢复确定；错Key/坏备份不破坏原状态；延期危险能力拒绝 |
| R2 | 权利与发布输入 | 许可证/商业权利处置、锁定安装输入、实际发行物SBOM/Secret/通知一致 |
| R3 | 真实编码质量与有限模型认证 | 原0/20归因；现有20 Trial按预注册门槛验证；至少一份真实认证配置 |
| R4 | 三平台发行与手动升级 | 脱离源码安装，原生编码闭环，认证候选升级、同机恢复和卸载通过 |
| R5 | 小批真实Beta与文档 | 3～5名独立开发者、至少15任务及三平台使用；P0/P1处置；用户可独立操作 |
| R6 | 1.0正式封板 | 同一候选必要门禁通过，版本/制品/支持矩阵与商业授权资料交付 |

执行优先级按实际产品能力排序：R1高风险安全与恢复、R3真实编码质量、R4原生Windows核心编码及安装升级优先推进。
R2中的许可证、权利链和其他不直接改变功能的治理工作低优先并行处理，不阻挡功能研发与内部验证；
正式发行前仍必须完成必要处置，不能把未处置事项标记为已通过。不得新增不必要的治理平台。

R1已补[Container资源能力准入](changes/m09-r1-container-resource-admission.md)，拒绝缺少内存、CPU CFS或
PIDs机制的引擎，并在每次启动及MCP连接前复核；不关闭R1整体或真实编码发布门禁。
固定`753d6a8`的[专项验收](validation/container-resource-admission-2026-09-29-v1/README.md)保留
修复前23失败、本地完整5724通过、真实Linux资源值核对及同一新Wheel三平台生命周期结果。
Scripted 20/20不是新真实质量；消费者OS、版本升级、独立Beta与R1～R6整体仍开放。
R3原70元周期账本复用，不重置周期或释放旧未决预留。固定镜像及显式同Engine评测链已通过前置检查；
固定`7bbce10`的新完整20 Trial已完成，严格成功0/20、必需测试通过1/20，质量未达标；
后继修复候选尚未完成真实复验。默认Desktop路径与消费者平台仍独立开放。
预算可用不代表编码质量已验收；不以替换镜像、放宽资源机制或脚本成绩绕过真实20 Trial门禁。

R4当前增量见[Windows原生NTFS文件事务与默认审批写链](changes/m09-r4-windows-native-file-transactions.md)。
固定源码`87f9353`已完成该文件事务专项及双Python受影响回归，详见[验证报告](validation/windows-native-file-transactions-2026-09-28-v1/README.md)。
该增量不关闭R4：Windows默认Git读取/交付、三平台脱离源码安装、升级恢复、独立Beta与最终同候选门禁仍需完成。
该切片复用原审批、Lease和Transaction状态机；原生验证、Git产品装配及三平台发行退出条件仍分别验收。
[规范发行Wheel详设](changes/m09-r4-installed-product-acceptance.md)及
[固定源码实际验证](validation/canonical-wheel-three-platform-2026-09-29-v1/README.md)
完成同一Run唯一Wheel三平台源码外安装、完整备份恢复、卸载及重装，制品摘要一致。
Windows消费者环境、完整编码闭环、版本升级/回退、独立Beta和同候选全部发布门禁仍开放；不据此关闭R4。
详细逐项处置、退出阈值、依赖及延期表见[发布范围收敛计划](changes/m09-to-v1-release-scope-convergence.md)。


### 0.9.1实施计划与完成边界

0.9.1按[源码研究](research/cli-tui-product-experience.md)、
[ADR 0078](adr/0078-product-shell-and-recoverable-client-state.md)和
[详细设计](changes/m09-1-cli-tui-product-experience.md)拆分为以下可独立验证的纵向子切片。先关闭协议和恢复正确性，
再建设终端表现层；任何后续子切片不得绕过未完成前置项：

- [x] **0.9.1a 严格SDK与可恢复客户端内核**：Response/Frame/Result/Handshake加固，版本化Client State、
  发送前Command分配、连接代际和确定性Projection Reducer；
- [x] **0.9.1b TUI基础产品链**：`harnessix code`、Textual生命周期、Transcript、Composer、Session Picker、
  Resume和真实stdio纵向恢复；实现、并发稳定化与三平台CI已经完成；
- [x] **0.9.1c 完整领域交互**：按[专项详细设计](changes/m09-1c-domain-interactions.md)交付Plan、Tool、Approval、Question、Diff、Usage/Cost、Cancel、Steer和错误自助；65项专项测试、全仓3434项通过/13项跳过、513幅Mermaid真实渲染及[CI 34727612571](https://github.com/carrie1988/Harnessix/actions/runs/34727612571)全矩阵验收完成；
- [x] **0.9.1d 配置与Windows原生只读链**：按[专项研究](research/configuration-preflight-and-windows-read-runtime.md)、[ADR 0079](adr/0079-preflight-and-native-read-port.md)和[详细设计](changes/m09-1d-configuration-preflight-windows-read.md)交付配置向导、Preflight、Doctor、Windows Workspace安全端口和默认产品启动；主体实现`532e59b`与验证修复`9372377`已由[CI 34735529084](https://github.com/carrie1988/Harnessix/actions/runs/34735529084)完成全矩阵验收并关闭；
- [x] **0.9.1e 统一Action产品装配**：Artifact、Patch、Process、Delivery通过Trusted Action、Policy、Approval、
  Action Audit、专用效果事实、Sandbox和Reconcile进入默认能力目录；[专项源码研究](research/default-trusted-action-product-composition.md)、
  [ADR 0080](adr/0080-capability-proven-product-action-composition.md)和[详细设计](changes/m09-1e-default-trusted-action-composition.md)
  已完成；e1由[CI 34739842959](https://github.com/carrie1988/Harnessix/actions/runs/34739842959)验收关闭；e2实现提交`328aa2d`已由[CI 34744116155](https://github.com/carrie1988/Harnessix/actions/runs/34744116155)完成全矩阵验收并关闭；e3实现提交`71a4794`与验证修复`a263f96`已由[CI 34748685155](https://github.com/carrie1988/Harnessix/actions/runs/34748685155)完成POSIX安全多文件Patch纵向链及全矩阵验收并关闭；e4实现提交`f5a3936`与状态等待稳定化提交`4b28fa4`已由[CI 35434198163](https://github.com/carrie1988/Harnessix/actions/runs/35434198163)完成统一Catalog、Supervisor生命周期、审批执行、输出Artifact、真实固定镜像及七任务全矩阵验收并关闭；e5实现提交`e5b7a8a`接入外部Action Config安全加载、Doctor能力报告、Product/Action双指针原子CAS、上一活动配置恢复Router、`running/reconciling → unknown → reconcile`和统一`ProductActionRuntimeOwner`，已由[CI 35439332019](https://github.com/carrie1988/Harnessix/actions/runs/35439332019)完成七任务全矩阵验收并关闭。
- [x] **0.9.1f 单一产品运行时收敛**：按[ADR 0081](adr/0081-single-coding-agent-product-boundary.md)和
  [专项详细设计](changes/m09-1f-single-product-runtime-convergence.md)撤销`serve/worker`、Action HTTP SDK和
  LangGraph Action Adapter的公共产品地位；f1先关闭公共入口并冻结旧调用方白名单，f2把Process、Git Push和
  历史Eval迁入`TrustedActionRouter`，f3再删除HTTP API、Worker Queue、旧Bootstrap、专用Adapter与相关依赖。
  Policy、Approval、Effect、`UNKNOWN`与Reconcile继续作为Coding Agent进程内Trusted Action Runtime能力。f1实现提交
  `142dfa8`已由[CI 35418034976](https://github.com/carrie1988/Harnessix/actions/runs/35418034976)完成七任务全矩阵验收并关闭；
  f2a由0.9.1e4/e5的固定Container Process和启动恢复Owner关闭；f2b实现提交`e2d8c24`已删除Git Push对旧ActionService/Effect Journal的生产依赖，
  由直接Definition/Executor、exact lease、响应丢失及宿主硬崩溃重开只对账测试建立正式链，并由[CI 35442924441](https://github.com/carrie1988/Harnessix/actions/runs/35442924441)完成七任务全矩阵验收并关闭；f2c实现提交`89485f3`已把历史Eval新运行改为Catalog/Gateway/Router、Execution Plan/Action Audit、POSIX Supervisor与Action Output Artifact，治理白名单由7项缩为6项，本地3569项通过/20项跳过，并由[CI 35446341997](https://github.com/carrie1988/Harnessix/actions/runs/35446341997)完成七任务全矩阵验收并关闭。f3实现Revision `a81868c`把旧生产引用集合清零，删除API/Worker/SDK/Adapter/Journal/专用Process Bridge及其直接依赖、规格、示例和测试；旧Session Process事件仅允许读取并以`legacy_process_state_archived`拒绝继续执行，旧SQLite/PostgreSQL状态按离线归档手册处置。本地3472项通过/18项跳过且190幅变化Mermaid真实渲染通过；[CI 35453082992](https://github.com/carrie1988/Harnessix/actions/runs/35453082992)随后通过Linux Python 3.12/3.13、macOS、Windows、固定镜像Container和Documentation六实例矩阵，f3、f及0.9.1据此关闭。

界面可启动或单个Prompt正常返回不能关闭0.9.1。六个子切片必须分别完成合同、失败/恢复、取消/超时、持久化、
可观测性、三平台测试、真实场景和现行文档同步；全部勾选后才可勾选0.9.1总项。

### 0.9.2实施计划与完成边界

0.9.2按[源码研究](research/eval-suite-and-transcript-baseline.md)、
[ADR 0082](adr/0082-multi-repository-eval-suite-and-transcript-evidence.md)和
[详细设计](changes/m09-2-eval-suite-and-transcript-baseline.md)拆分。单任务Campaign继续负责同一任务的重复试验；
Suite在其上负责跨任务、跨仓库身份冻结、证据核对和可重算聚合，不能把一次成功运行包装成“多仓库基线”：

- [x] **0.9.2a Suite与Transcript正式契约**：在首个Provider请求前冻结Suite/Case/Campaign身份；实现五类任务、
  至少两个固定仓库Revision、脱敏Transcript摘要、测试证据、任务成功率、测试通过率、人工干预率、Token、成本、
  延迟聚合，以及0600、no-follow、有界、原子Plan/Report读写。实现Revision `d42ab6c`已由[CI 35456635653](https://github.com/carrie1988/Harnessix/actions/runs/35456635653)完成六实例全矩阵验收并关闭；
- [x] **0.9.2b Task Pack v1**：建立许可证和来源可审计的固定任务包，不接受运行时任意URL、任意测试命令或宿主脚本；
  每个任务固定来源Commit、Tree摘要、允许路径、基线/行为/回归检查和预算。实现Revision `608c07a`交付内置双语言种子Pack、安全Archive/Git物化、
  消费点身份重验、固定Product Profile和真实Container先失败后通过验收，已由[CI 35461708961](https://github.com/carrie1988/Harnessix/actions/runs/35461708961)完成六实例全矩阵验收并关闭；
- [x] **0.9.2c 可恢复Suite Runner**：在任何模型请求前持久化计划，按固定Campaign顺序执行；取消、崩溃和重开只沿用
  同一Run ID补证据，不重复已完成Case，当前Case交由下层Campaign/Run权威事实恢复；成本未知、证据缺失和身份漂移停止后续试验。实现Revision `ffd3db4`交付计划先行、单写者锁、
  连续Case证据前缀、显式停止恢复和报告发布恢复，已由[CI 35465458256](https://github.com/carrie1988/Harnessix/actions/runs/35465458256)完成六实例全矩阵验收并关闭；
- [x] **0.9.2d 多仓库离线基线**：至少10个Case、3个固定仓库、Bug Fix/Feature/Refactor/Test/Review每类至少2个，
  每Case至少2次试验；通过固定Container Profile运行全部检查，形成可复跑的离线Suite报告；
  - [x] **d1 数据集与检查闭环**：`harnessix-engineering/v1`固定3仓10 Case、五类各2个、来源许可证、确定性生成、
    Review源码证据和Wheel外Golden；实现Revision `ee4d0db`已由[CI 35469387988](https://github.com/carrie1988/Harnessix/actions/runs/35469387988)完成固定Container与六实例全矩阵验收并关闭；
  - [x] **d2 正式Case Adapter**：Task Pack Case经现有Agent、Session、Trusted Action、Process Artifact、Grader和
    Campaign执行/恢复，不另建Eval Agent或旁路审批；实现Revision `a04606b`覆盖Trial Report与Campaign Report发布窗口恢复，
    已由[CI 35479723645](https://github.com/carrie1988/Harnessix/actions/runs/35479723645)完成固定Digest Container与六实例全矩阵验收并关闭；
  - [x] **d3 完整离线Suite**：每Case固定2 Trial，验证取消、超时、崩溃、UNKNOWN、完成前缀和报告发布恢复，发布
    20 Trial脱敏可重算报告后关闭0.9.2d。首轮固定Container运行识别出v1两个Test Case依赖未跟踪新文件；修正保持
    v1 Manifest/Archive原字节，新增只替换受版本控制失败测试基线的v2，未放宽Workspace Patch或Grader。实现Revision
    `505bc53`由[CI 35483905418](https://github.com/carrie1988/Harnessix/actions/runs/35483905418)完成v2固定Container、
    20/20 Trial、双Suite提交窗口恢复和六实例最终验收，[证据已冻结](validation/offline-engineering-2026-09-20-v2/README.md)；
- [x] **0.9.2e 受控真实Provider基线**：在固定模型、价格、地域、Token和费用预算下执行完整Suite，保存脱敏报告与
  验证证据；不保存Prompt、模型回答、工具参数/输出、代码正文、绝对路径或Secret；
  - [x] **e1 受控执行与证据候选**：完成通用Task Pack Suite组合、私有Provider配置、默认禁网CLI、Pack/Revision/宿主程序校验、Suite/Case恢复摘要绑定、每Trial独立Provider及白名单证据发布；首轮与第二轮真实运行分别暴露空Profile评分、测试分母、Campaign旧State和CLI零进度问题。最终修正Revision `fb4a0ea`保持严格Grader与安全边界，由[CI 35491527318](https://github.com/carrie1988/Harnessix/actions/runs/35491527318)完成六实例验收；
  - [x] **e2 真实运行与冻结**：在精确北京模型和有效价格窗口内完成10 Case × 2 Trial，20个Turn均正常终结，记录81次模型请求、318,478输入Token、12,148输出Token和CNY 1.46828完整已知成本；任务成功与测试通过均为0/20，未选择性重跑或放宽评分，[低敏证据已冻结](validation/provider-engineering-2026-09-20-v1/README.md)。关闭文档Revision `6dd391a`已由[CI 35492831821](https://github.com/carrie1988/Harnessix/actions/runs/35492831821)完成六实例验收。

只有a～e全部满足合同、失败与恢复、持久化、可观测性、完整测试、真实场景和文档同步，且d/e达到上述规模与证据门槛，
才可勾选0.9.2总项。Schema存在、单元测试通过或只有两个仓库五个Case均不能关闭0.9.2。

### 0.9.3实施计划与完成边界

0.9.3按[专项源码研究](research/reliability-and-performance.md)、
[ADR 0089](adr/0089-bounded-local-transport-lifecycle.md)、[ADR 0090](adr/0090-plan-first-store-maintenance-and-backup.md)、
[ADR 0091](adr/0091-action-runtime-fencing-and-bounded-reconciliation.md)、[ADR 0092评审稿](adr/0092-reproducible-local-soak-and-release-thresholds.md)和
[总体详细设计](changes/m09-3-reliability-and-performance.md)、[0.9.3b专项详细设计](changes/m09-3b-persistent-capacity-and-retention.md)、
[0.9.3c专项详细设计](changes/m09-3c-action-runtime-fencing-and-recovery.md)、[0.9.3d专项详细设计评审稿](changes/m09-3d-soak-and-performance-evidence.md)拆成四个连续纵向切片。不得用一次短压测或“未观察到异常”
替代容量合同、故障恢复与可重算证据：

- [x] **0.9.3a 本地传输可靠性**：stdio使用守护Reader/Writer泵，握手后执行协商Pending/Outbox上限；SDK以
  `Pending + Abandoned`共享容量保留迟到Response身份，Close由Transport拥有且调用方取消不打断回收；只公开不含
  ID、路径和stderr正文的资源快照。实现Revision `f113594`与文档Revision `7cbacba`已由
  [CI 35494960166](https://github.com/carrie1988/Harnessix/actions/runs/35494960166)完成Linux Python 3.12/3.13、macOS、Windows、Container和文档六实例验收；
- [x] **0.9.3b 持久容量与保留**：为Session、Protocol Request和Artifact建立版本化容量快照、Plan-first清理、
  活跃/未决/UNKNOWN禁删集合、崩溃恢复、备份与回滚；实现Revision `cb3f3ea`及本地`make check`（3589 passed、32 skipped）已完成，并由[CI 35498012926](https://github.com/carrie1988/Harnessix/actions/runs/35498012926)通过Linux Python 3.12/3.13、macOS、Windows、固定Container和Documentation六实例验收；
- [x] **0.9.3c 效果与进程恢复**：补齐Trusted Action/Process单Owner fencing、孤儿扫描、Route Deadline、
  Reconcile复合故障和零重复外部效果证据；实现Revision `0bc942b`的本地`make check`为3595 passed、32 skipped，
  [首次CI 35499848035](https://github.com/carrie1988/Harnessix/actions/runs/35499848035)发现文档同步和Container组合根Session初始化缺陷；修复版Revision `33fcf02`本地`make check`为3597 passed、32 skipped，由[CI 35691402329](https://github.com/carrie1988/Harnessix/actions/runs/35691402329)六实例验收关闭；
- [x] **0.9.3d 长会话Soak与发布阈值**：用固定场景和环境记录启动时延、操作分位数、峰值RSS、数据库/Artifact增长、
  清理水位和故障计数，冻结低敏Manifest与独立阈值验证。源码核查、ADR和专项详细设计处于评审阶段；
  已实现严格样本/Manifest、Run提交与独立重算、三平台RSS适配、长会话真实Runtime及多Thread应用服务
  缩小负载Runner，并补齐多Thread启动恢复时延指标合同。已归档[macOS 500 Thread单次诊断事实](validation/soak-macos-2026-09-23-v1/README.md)，
  但该Revision的跨平台基准Job失败，不能用于冻结Profile；后续拆分启动/分页期限并先排空超时SQLite任务的修复版已由[CI 35804642232](https://github.com/carrie1988/Harnessix/actions/runs/35804642232)六实例验收，旧Run仍仅供诊断。现有五个Runner已在负载前持久写入Attempt开始事实，异常保留失败终态，硬退出保留未完成事实；
  [macOS单Thread连续1000 Turn规模诊断](validation/soak-macos-2026-09-23-v2/README.md)已在干净Revision完成并经Run/Attempt双重重算，对应六实例CI通过；该旧Run尚未专门断言Context/Compaction。新的[长会话Context/Compaction v2详设](changes/m09-3d-long-session-context-proof.md)已落地逐Turn事件Proof和v1/v2独立Reader；[macOS一次v2千Turn规模基线](validation/soak-macos-2026-09-23-v3/README.md)已完成1000次正式Context检查、199次压缩摘要/窗口及Run/Attempt重读，实现Revision六实例CI通过。新的[500 Thread正式负载诊断](validation/soak-macos-2026-09-23-v4/README.md)完成Run/Attempt和45条样本重算；对应Revision的Windows Product UI首次尝试两项超时、第二次重跑成功，原因未明，不能升级为可冻结Profile的基线。Artifact真实Runner已有本地合同与失败回归；重启Runner已完成三平台基线、Profile及第二独立PASS报告；Action恢复场景已完成[三平台基线与第二独立PASS](validation/soak-action-recovery-three-platform-candidate-2026-09-24-v1/README.md)并关闭固定场景工程护栏；Artifact增长场景经两轮候选FAIL（原件保留）与[历史Artifact批量验证修复](changes/m09-3d-history-artifact-batch-verification.md)后完成[300件三平台基线](validation/soak-artifact-growth-three-platform-2026-09-24-v3/README.md)与[第二独立PASS](validation/soak-artifact-growth-three-platform-candidate-2026-09-24-v3/README.md)并关闭；长会话场景已完成[三平台基线](validation/soak-long-session-three-platform-2026-09-24-v1/README.md)与[第二独立PASS](validation/soak-long-session-three-platform-candidate-2026-09-24-v1/README.md)并关闭，[两平台基线与Windows诊断](validation/soak-long-session-two-platform-2026-09-24-v1/README.md)及70846f5治理降级运行保留为历史诊断；多Thread已封印Profile并取得第三轮三平台候选PASS，对应CI六Job成功，固定场景门禁关闭；[多Thread固定三平台采集入口](changes/m09-3d-many-threads-three-platform-evidence.md)已实现，[三平台一次正式负载原件](validation/soak-many-threads-three-platform-2026-09-23-v1/README.md)已归档；该Revision的[CI 35870214455](https://github.com/carrie1988/Harnessix/actions/runs/35870214455)六Job成功；历史v1缺逐轮集合Proof，旧入口超时排空无独立全局期限。现行[逐轮分页证明v6](changes/m09-3d-many-threads-proof-v6.md)与20分钟发布进程硬期限均已有本地回归，[三平台v6基线](validation/soak-many-threads-three-platform-2026-09-23-v2/README.md)已归档并独立复核，且同Revision[CI 35876009038](https://github.com/carrie1988/Harnessix/actions/runs/35876009038)六Job成功；[冻结Profile与第二独立Run详设](changes/m09-3d-many-threads-frozen-profile-candidate.md)已实现候选入口及本地回归；三平台Profile已从v6基线封印，[首轮三平台候选诊断](validation/soak-many-threads-three-platform-candidate-2026-09-23-v1/README.md)因同Revision常规CI失败不被接纳；[修复后第二轮候选](validation/soak-many-threads-three-platform-candidate-2026-09-23-v2/README.md)出现macOS三项性能越限FAIL，该Revision的场景门禁未通过；[Thread列表与启动恢复读路径收敛详设](changes/m09-3d-thread-list-and-recovery-read-path.md)已明确旧路径多次重复读取的结构性放大，索引分页与单事务全量校验恢复已通过本地全量测试，[新Revision三平台候选](validation/soak-many-threads-three-platform-candidate-2026-09-23-v3/README.md)在原封印Profile下三份PASS、24份原件和独立复算通过；同Revision[CI 35883976180](https://github.com/carrie1988/Harnessix/actions/runs/35883976180)六Job成功，固定多Thread场景工程护栏通过，旧FAIL保留；
  [单平台阈值独立复验内核](changes/m09-3d-threshold-verification.md)已实现冻结Profile与完整Attempt双重核验；候选在负载前将Profile ID/摘要持久写入STARTED v2，最终Manifest必须同值；显式工程余量、全部分位数/文件增长比较及不可覆盖报告已具备。[Artifact增长场景详设](changes/m09-3d-artifact-growth-soak.md)进入评审，v3低敏Proof、Manifest、独立Reader和真实Agent/Tool/Store Runner已实现，包含混合件、全页读取与逻辑到期清理；[首次macOS规模Run](validation/soak-macos-artifact-2026-09-23-v1/README.md)与[第二次规模Run](validation/soak-macos-artifact-2026-09-23-v2/README.md)均因各自Revision的Windows Benchmark句柄清理失败仅归档为诊断；两个独立缺口分别为同步只读连接关闭和异步连接取消收尾，[修复后macOS单平台Artifact规模基线](validation/soak-macos-artifact-2026-09-23-v3/README.md)已在干净Revision重跑、独立重算并由[CI 35824543623](https://github.com/carrie1988/Harnessix/actions/runs/35824543623)完成六实例验收。Linux/Windows正式负载、复合故障和工程阈值仍未验收。SDK容量和重启场景均已冻结三平台Profile并完成各自三平台第二独立PASS报告。Action恢复与Artifact增长场景也已完成三平台基线、冻结Profile与第二独立PASS；[完整产品重启Soak详设](changes/m09-3d-product-restart-soak.md)已进入评审，V5证据合同、独立Reader、受控硬退出、[500 Thread三平台基线和封印Profile](validation/soak-restart-three-platform-2026-09-23-v1/README.md)已完成，对应Revision的[CI 35864457452](https://github.com/carrie1988/Harnessix/actions/runs/35864457452)失败Job重跑后六Job成功；[冻结候选详设](changes/m09-3d-product-restart-frozen-profile-candidate.md)及[三平台第二独立Run与PASS报告](validation/soak-restart-three-platform-candidate-2026-09-23-v1/README.md)已归档；对应Revision的[CI 35867509424](https://github.com/carrie1988/Harnessix/actions/runs/35867509424)首次文档Job超时、仅重跑失败Job后六Job成功。SDK容量场景已新增[真实SDK/stdio容量Soak详设](changes/m09-3d-sdk-capacity-soak.md)、v4低敏Proof、独立Reader和64容量/迟到Response全路径回归；[macOS单平台正式规模基线](validation/soak-macos-sdk-2026-09-23-v1/README.md)已在干净Revision完成并独立重算，对应[CI 35833762475](https://github.com/carrie1988/Harnessix/actions/runs/35833762475)六实例成功。[三平台固定负载采集通道](changes/m09-3d-sdk-cross-platform-evidence.md)已完成真实Linux/macOS/Windows Job；[SDK容量三平台一次正式基线](validation/soak-sdk-three-platform-2026-09-23-v1/README.md)及原始Run/Attempt与摘要已复核，[CI 35838258049](https://github.com/carrie1988/Harnessix/actions/runs/35838258049)六实例成功。基线阶段各平台各有一次正式Run；[SDK冻结Profile与候选详设](changes/m09-3d-sdk-frozen-profile-candidate.md)及签封原件已建立；[三平台候选Run及PASS报告](validation/soak-sdk-three-platform-candidate-2026-09-23-v1/README.md)已完成三平台第二Run与独立PASS报告，对应Revision的[CI 35843734178](https://github.com/carrie1988/Harnessix/actions/runs/35843734178)六实例成功，SDK容量固定场景门禁关闭。既有Windows Product UI超时低敏诊断仍不等于偶发超时根因关闭；[新增退出期限诊断](validation/product-quit-windows-2026-09-23-v1/README.md)记录归档Revision的Windows首次失败与重跑成功，[20秒预算修复详设](changes/m09-3d-product-quit-close-budget.md)已由[CI 35847851813](https://github.com/carrie1988/Harnessix/actions/runs/35847851813)三次六Job成功验收，但旧失败排他根因仍未确认。单次基线和缩小负载均不得充作发布PASS。至此六个场景均已完成三平台正式负载、冻结Profile与第二独立PASS，[ADR 0092](adr/0092-reproducible-local-soak-and-release-thresholds.md)已于2026-09-25接受，0.9.3d与0.9.3关闭；全部历史失败证据保持只读。

只有a～d均通过合同、取消/超时、失败恢复、持久化、可观测性、三平台适用性、完整回归和文档同步，才可勾选
0.9.3总项。0.9.3不新增独立HTTP/Worker、性能控制面或远程数据库，也不把守护线程误述为底层I/O已被强制中断。

### DOC-1：设计文档与源码可追溯治理

DOC-1是0.9阶段的横向阻断工作流，不重开0.9.0，也不替代0.9.1～0.9.6产品切片。目标是把
历史上按里程碑和ADR累积的资料治理为可直接支持源码阅读、设计评审、测试和故障定位的正式
文档体系。完整范围、量化基线和执行顺序见[文档治理入口](governance/README.md)、
[现状全量盘点](governance/documentation-inventory.md)和
[整改待办](governance/documentation-remediation-backlog.md)。

- [x] **DOC-1.0 盘点与规范**：固定133份Markdown、30个顶层生产包和10个根级生产模块的起始基线；建立文档分类、状态、元数据、重大变更流程、图示、源码/测试链接规范、五类模板、追踪矩阵和分阶段待办。本切片不修改生产代码，也不宣称存量资料已经整改完成；
- [x] **DOC-1.1 导航与系统架构**：建立[文档总入口](README.md)、[当前系统架构](architecture.md)、30个包与10个根级模块的边界，以及[源码阅读路线](guides/source-reading-map.md)；明确默认产品、显式装配库和规划能力，覆盖正常任务、审批、取消、崩溃恢复和事务性交付五条时序；
- [x] **DOC-1.2 黄金样例**：完成[Agent Runtime模块设计](modules/agent.md)和[Action Plane子系统设计](subsystems/action-plane.md)，覆盖状态、接口、字段、正常/失败/恢复、事务、安全、观测、伪代码及源码测试双向映射；源码反向抽查均超过10个关键符号，测试正向定位均超过5类；
- [x] **DOC-1.3 Coding Agent主链**：19/19已完成；Wave A～D已补齐Session、Context、Artifact、Model、Tool、Patch、Execution、Process、Action安全链、Workspace、Delivery、[Evals](modules/evals.md)与[Observability](modules/observability.md)现行设计，并登记实现与历史资料之间的真实差异；
- [x] **DOC-1.4 产品运行时与扩展**：10/10已完成；[Protocol模块设计](modules/protocol.md)、[App Server模块设计](modules/app-server.md)、[SDK模块设计](modules/sdk.md)、[Product Config模块设计](modules/product-config.md)、[API模块设计](modules/api.md)、[Adapter模块设计](modules/adapters.md)、[MCP模块设计](modules/mcp.md)、[Skill模块设计](modules/skills.md)、[Hook模块设计](modules/hooks.md)与[Smoke模块设计](modules/smoke.md)均已建立；
- [x] **DOC-1.5 聚合与历史治理**：已将当前[测试与Eval规范](testing-and-evals.md)、[里程碑测试历史](testing-and-evals-milestone-history.md)、[真实验证证据](validation/README.md)、[六类运维资料](deployment.md)和里程碑增量设计分层；0.5及0.8采用历史索引与冻结完整原文结构，其余里程碑及0.6专题设计统一标记为`historical`并链接现行模块；[ADR索引](adr/README.md)覆盖76份接受决策，[源码研究索引](research/README.md)覆盖27份冻结研究，仓库内189份Markdown均已完成标准状态迁移；
- [x] **DOC-1.6 自动化门禁**：已交付[版本化策略](../governance/documentation-policy-v1.json)、全库静态检查、源码差异同步、公共合同逐字节漂移检查和25项新增正反例；Linux/macOS/Windows均执行离线文档门禁，Linux对变化Mermaid执行真实渲染，公共合同生成因Evals当前POSIX依赖而在Linux/macOS执行。实现提交`991b6f2`由[CI 34709603781](https://github.com/carrie1988/Harnessix/actions/runs/34709603781)完成全部Job验收，设计与边界见[ADR 0077](adr/0077-versioned-documentation-contract-and-gates.md)和[详细设计](changes/doc-1.6-automated-documentation-gates.md)。

DOC-1.1和DOC-1.2前置门禁已经完成。0.9.1及以后每次重大提交都必须按照
[详细设计模板](governance/templates/detailed-design-template.md)或
[重大变更设计模板](governance/templates/change-design-template.md)形成正式设计；没有失败与恢复、
安全边界、完备测试和实际源码映射的变更不得标记为生产完成。

### 0.9.0范围与完成边界

状态：**已完成**。源码研究、正式决策、机器可读基线、注释治理、Reducer职责拆分、渐进CI门禁、Worker执行结果提交竞态修复、本地全量回归和文档同步均已完成；最终实现提交`8a0686c`由[CI 34629640717](https://github.com/carrie1988/Harnessix/actions/runs/34629640717)完成六矩阵验收。

最终实现固定256个生产源码文件、273个静态公共导出、146个公共数据合同、13个超大文件、
148个超长或高复杂度符号、164条一级包依赖边和一个既有强连通分量。全部模块、公共行为和25组
高风险入口已通过说明门禁；`agent.reducer`保留稳定门面，并把Item、Turn和共用守卫提取到独立
模块。起始基线可从提交`e15ffaa20142e9f61cf8412b3d4499001a695368`逐字重建，详细事实见
[0.9.0设计](m09-code-maintainability.md)、[ADR 0076](adr/0076-code-readability-and-structural-governance.md)
与[测试验收](testing-and-evals-milestone-history.md#79-090代码可维护性治理验收2026-09-12)。

实施顺序继续遵循本路线图统一原则：先研究Harnessix现有职责、调用链、复杂度和测试保护，按固定
版本求证Codex、OpenCode与Claude Code的可维护性做法，再形成Harnessix独立决策、注释规范及
结构治理ADR。注释补齐与结构重构分批实施，禁止在同一大范围Diff中同时改变行为和解释行为。

核心任务包括：

1. 冻结源码文件、逻辑行、模块文档字符串、公共符号、复杂度、依赖环和超大职责热点基线，区分
   原始覆盖数字与实际语义质量，不用空泛文档字符串提高指标；
2. 建立简体中文代码注释与命名规范：文件说明职责和非职责，类说明生命周期、并发及持久化边界，
   函数说明前置条件、副作用、幂等性、失败和恢复语义，行内注释只解释设计原因、安全边界及不变量；
3. 优先治理Agent Runtime/Reducer、Trusted Action Runtime、Patch、Process、Workspace、Delivery和Session Storage，
   再治理Protocol、App Server、SDK、MCP、Skills、Hooks、Provider、Context和Eval；
4. 对Hash/Fingerprint/Revision、Cursor/Sequence、Attempt、Lease/Timeout/TTL、前后镜像和
   Effect事实等关键变量明确身份、作用域、来源及单位；优先使用自描述命名和类型，不为显然代码
   添加逐行复述；
5. 对`agent/runtime.py`等超大文件先完成内聚性、耦合、公共导入和状态机分析，再通过独立ADR逐个
   提取职责；保留公共Facade、稳定导入路径、Schema、错误码、事件顺序和持久化兼容性；
6. 先生成可读性报告，再按已治理包渐进启用文档及复杂度规则；新增公共API和高风险副作用路径
   必须具备语义说明，既有代码不通过一次性全局规则被迫产生模板化注释。

0.9.0只有同时满足以下条件才可完成：

- 所有非空业务模块均能说明职责、边界和主要依赖，公共API及高风险私有入口具有与实现一致的
  语义说明；
- 状态转换、CAS、锁、事务、Lease、取消、超时、崩溃恢复和UNKNOWN边界均能由邻近代码说明
  或稳定链接追溯到正式设计文档；
- 公共参数、导入路径、Schema、迁移、错误码、事件顺序和外部可观察行为没有未声明变化；
- 结构拆分均有拆分前特征测试、拆分后等价回归和独立评审证据，不以文件行数下降冒充职责清晰；
- Ruff、Mypy、全量测试、Schema冻结检查、离线示例及Python 3.12/3.13、macOS、Windows、
  PostgreSQL、固定镜像Container六矩阵CI全部通过；
- README、架构、测试规范、相关ADR和开发者文档与最终模块边界同步，且不包含过程性对话或敏感信息。

### 验收标准

- 0.9.0可维护性基线和渐进门禁完成，新增功能不再扩大未解释的核心职责或文档债务；
- 复用既有3仓10 Case/20 Trial Task Pack，按运行前冻结的R3阈值验证，不新增语言生态或评测平台；
- 编码行为变更补受影响回归；固定候选统一进行真实Eval，不对不影响编码行为的文档/局部修复反复调用模型；
- 连续故障注入和Soak后无Session损坏、孤儿进程、未归因文件和重复外部副作用；
- 受控Beta发现的发布阻塞缺陷已关闭或有明确降级边界；
- 新用户仅依据正式文档即可完成安装、配置、首个真实任务、恢复和卸载。

## 12. 1.0：本地优先正式商用发布

### 发布范围

- [ ] 版本化Agent/Tool/Provider/Context/Session/Protocol；现有CLI/TUI、stdio和Python SDK；
- [ ] 三平台Git仓库中的读取、搜索、多文件修改、受控进程、测试、Diff、本地Commit、Checkpoint和Rollback闭环；
- [ ] 持久会话、恢复、取消、审批、压缩、当前状态认证以及同机同用户数据库/Key备份；
- [ ] 两类Provider Adapter与离线契约；至少一份真实认证配置及明确的Usage/估算边界；
- [ ] 受控本地stdio MCP、项目指令、Skills/Hooks、Host等级及一种Container后端；
- [ ] 统一Wheel通道、有限三平台目标、手动升级/回退、删除/卸载、低敏诊断、支持文档与商业授权。

### 发布门禁

- [ ] **R1**：正式装配安全与失败恢复通过；没有可达但未经处置的高风险延期入口；
- [ ] **R2**：自有权利链、12件许可复核、精确安装输入和实际发行物扫描/SBOM/通知通过；
- [ ] **R3**：真实编码结果达到预注册阈值，认证配置的功能/Usage/保护通过；0/20不能视为商用质量；
- [ ] **R4**：三平台核心链、脱离源码全新安装、当前认证候选升级、同机恢复及卸载通过；
- [ ] **R5**：小批真实Beta及正式资料完成，无未处置P0/P1；
- [ ] **R6**：同一候选Revision的必要离线/原生/真实门禁通过，版本、校验、Changelog、迁移和支持矩阵一致。

公共Schema保留版本/兼容/废弃策略；现有数据库及Artifact变更仍需迁移和失败恢复证据。
R3已补[产品与评测的共享Context、编码指令及持久压缩装配](changes/m09-r3-product-context-composition.md)。
该整改保持Task Pack v2及评分合同不变；默认产品接线、离线回归或API鉴权均不能替代完整真实任务质量验收。
后续[R3可信文件快照](changes/m09-r3-trusted-file-snapshot.md)补齐默认模型读取至受管Patch前置摘要的来源；
旧分页合同保留，新目录版本化，读取/审批后文件漂移不得覆盖实际内容。该切片不关闭真实质量或Windows写入门禁。
[独立验证资料](validation/trusted-file-snapshot-2026-09-28-v1/README.md)保留原Python 3.12相关回归FAIL，
后继源码修复POSIX目录观察后984项通过/13项跳过；同机完整备份恢复及R1整体仍未关闭。
[R3验证请求预算](changes/m09-r3-verification-request-budget.md)复用正式Suite、Case和官方Adapter，
原周期账本先预留后发送，未知费用停止整个Suite；默认执行路径兼容，恢复身份包含有限Guard范围。
该机制仅保护受控验证请求，不恢复延期的通用计价平台、不降低原20 Trial门槛或关闭R3。
[后继单次有界复验](changes/m09-r3-bounded-reverification-budget.md)新增原周期内唯一Suite授权，
冻结完整旧请求前缀并保留原unknown全部预留；新费用按40元单轮和70元原总预算双上限保护，
默认未决即停及新增未决立即停止/重启拒绝保持。该离线机制不代表新完整真实质量成绩。
[实际有界复验](validation/bounded-provider-suite-interruption-2026-09-30-v1/README.md)在`940432f`首个Trial中断：
9次请求全部已知结算，新增估算0.219136元、无新增费用未知；原20.77824元预留保持。
模型8次固定Profile调用遗漏必填profile，没有审批、Profile结果或Trial报告；没有新的完整20 Trial成绩。
同Revision完整本机离线6033通过、111跳过，但不替代R3。
[固定Profile与Trusted输入反馈整改](changes/m09-r3-trusted-input-feedback.md)共享有界字段辅助，
明确显式必填Profile和实际Selector策略；保持原严格Decoder、独立批准、Token及评分门槛。
[统一专项材料](validation/trusted-input-feedback-2026-09-30-v1/README.md)保留原源码负对照、离线SDK、
实际Wheel及治理原失败；同候选完整离线6085通过/111跳过，测试成功不代替真实模型质量。
原真实失败保持，不重放旧未知效果；新完整Suite仍需原同一40元范围重新绑定，
已用0.219136元和旧20.77824元全额预留继续计入原70元，不视为新额度或实际账单。
[Profile观测分类与证据缺失停止整改](changes/m09-r3-profile-observation-and-evidence-stop.md)
从原Session只读副本求证：INTERRUPTED已是可返回终态，具体抛错来自参数拒绝被收集为实际Profile结果。
仅确认无效果且正式Decoder仍拒绝的输入错误不计检查；其余每项结果要求可信已知退出，
中间未知不能被后续成功覆盖。指定缺证路径持久停止Campaign与Suite，重开不重放、不推进完成前缀。
原Grader、Task Pack、Token、费用与评分门槛不变；原中断Suite不补写报告，完整20 Trial复验仍未通过。

[同一额度单次Suite切换](changes/m09-r3-same-cap-suite-rebinding.md)只为新候选提供显式管理合同：
原70元周期、原40元授权、旧预留和全部已用费用保持，不生成第二轮额度；仅显式登记升V2并撤销旧Suite运行资格。
实际账本登记、完整真实20 Trial及商用R3仍需独立证据，不能从离线切换实现推导已开跑或验收通过。
[R1全状态Owner](changes/m09-r1-product-state-ownership.md)将根外稳定互斥前移到Root准备和全部Store/Provider之前，
Action借用同一Owner，取消结算唯一目录线程后释放。该前置由后继完整备份/恢复及整体Root发布承接；
复杂Store/Artifact业务恢复与正式安全收口仍需完整验收，不关闭R1或R4。

[R1完整停机备份与原来源验真](changes/m09-r1-product-state-backup.md)已交付六库、可选Process事实、
事务Blob及独立Key的受管捕获；原MAC和跨Store引用先验真，再持久化根外本机回执并排他发布目录。
`state backup/verify`不构造Provider或Executor，原Root丢失后仍可验证已有可信备份。
[后继完整停机恢复](changes/m09-r1-product-state-restore.md)实现整体Root替换、耐久Journal、
启动前未决拒绝、显式继续/回退及稳定ID历史结果；原目录与原Key保留，不重放未知效果。
[固定源码独立复验](validation/product-state-restore-2026-09-28-v1/README.md)完成双Python专项与受影响回归，
三幅实际图示、Wheel与失败证据统一归档；这些macOS结果不替代三平台实际恢复。
后继规范Wheel专项已取得三平台实际状态恢复及卸载重装结果，R1/R4整体继续开放。
[Windows复杂业务状态专项](validation/windows-business-state-recovery-2026-09-29-v1/README.md)已把原业务用例接入原生CI，
元数据2/3缺失保持类型化、权限与共享错误仍拒绝；合法错Key负对照不破坏DPAPI格式。
固定修复`dc3692a`的四个原生步骤成功，完整Job未终结部分及原失败均按实际记录；不据此关闭R1/R4或继承旧Wheel结论。
[R3公开预算转换](changes/m09-r3-public-budget-domain-mapping.md)修复SDK显式启动及Retry预算被驼峰误送领域的缺陷。
[固定源码真实stdio验证](validation/public-budget-product-provider-2026-09-29-v1/README.md)的离线两个场景通过；
真实首场景在三次Patch缺少必需mode后失败，未进入审批，第二场景按原计划停止。
7次请求Usage和原预算结算完整，新增估算0.149308元，无未知预留；历史20 Trial成绩不变，R3继续开放。
下一质量整改需公开操作相关必填字段并保留严格解码，不在宿主补mode或改写模型提案以凑成功。
[R3操作Schema整改](changes/m09-r3-workspace-patch-operation-schema.md)已固定在`6a686fd`：
三个互斥分支与正式工具说明公开create/replace/delete的原字段条件，原Validator、审批和事务不放宽。
新旧工具指纹不同，旧批准不可继承；焦点17项及336个字段组合通过，
Python3.12/3.13受影响回归各2890项通过、70项跳过；3.13原导入环境失败单独保留。
[统一验证资料](validation/workspace-patch-operation-schema-2026-09-29-v1/README.md)登记原失败、
Schema/源码/制品身份、原Validator不变及旧持久批准拒绝；本专项新增模型请求和费用均为0。
整改后的[默认产品真实模型合同验收](validation/product-provider-operation-schema-2026-09-29-v1/README.md)固定于`c8033e0`：
一次运行的审批精确修改、等待审批取消两个场景通过，5次请求、22,626/634输入/输出Token、估算增量0.100648元。
原认证Store证明模型显式mode420及正确写前条件；原70元周期累计已知估算0.250024元、预留及未决为0，未重置。
该有限合同GO不计20 Trial、不登记商用支持白名单；原0/20、固定镜像环境和R3整体继续开放。
固定候选执行完整必要回归，开发批次只执行受影响检查；不逐提交等待全矩阵，不以减少检查频率豁免发布失败。
0.4.3c只保留R3所列首发边界；延期的全计价、远端MCP、公网Push、自动更新、通用维护CLI和跨机Key迁移不再阻断1.0。

1.0限定为大量独立本地实例，不宣称多租户、云端高可用、远程执行池或集中服务SLO。
Windows原生核心编码承诺不取消；具体有限OS/架构和模型支持表由[R4及认证白名单](changes/m09-to-v1-release-scope-convergence.md#3-首发产品边界与总体架构)定义，未验收不广告支持。

## 13. 1.x与后续演进

只有1.0单Agent本地产品稳定且真实用户证据充分后，才评估以下方向。

### 云端运行候选

- 远程Sandbox与云任务；
- 身份、租户、配额、限流和滥用治理；
- 云端Secret/KMS、对象存储和数据生命周期；
- 分布式Session、任务调度和Agent Worker；
- 团队策略、集中审计、计费、服务SLO、备份和容灾。

### 产品与能力候选

- 远端MCP Streamable HTTP/OAuth及扩展市场；
- Agent公网HTTPS/SSH Git认证、known-hosts、凭据Helper和Push装配；
- 更多Provider/模型/地域/模式认证、全面计价、供应商账单对账及实时费用硬上限；
- 通用保留/GC与维护CLI、跨机Key导出/轮换和旧无证明历史有权导入；
- 原生安装器、多包管理器、自动更新、正式Agent镜像及额外OS/架构；
- 在线一致性快照、滚动升级、自动回退和扩大Beta/评测生态；
- Subagent、Reviewer和并行任务；
- IDE、桌面客户端和Web；
- LSP、代码索引和大型Monorepo优化；
- 经0.7基准证明必要但未提前落地的Rust Process/Sandbox Sidecar。

这些能力不能提前侵入1.0核心，除非已有真实用户场景、风险分析和评测数据证明必要。

## 14. 每个迭代的统一完成定义

每个开发项只有同时满足以下条件才能勾选：

1. 参考实现研究记录固定提交或产品版本、来源、调用链、失败语义和Harnessix独立决策；
2. 需求、边界、约束和失败语义已写入总体设计、详细设计或ADR；
3. 公共输入输出有类型和版本化Schema；
4. 实现没有绕过既有分层和安全端口；
5. 正常、失败、取消、超时、崩溃和恢复路径按风险完成测试；
6. 日志、Trace、指标、Artifact和诊断资料不包含明文凭据；
7. 数据库及持久Artifact变更包含迁移、兼容、备份恢复和旧Reader测试；
8. 受影响检查通过；固定候选及阶段关闭执行完整必要门禁，局部修复不逐提交等待全部CI；正式发布不豁免失败的安全、许可或核心恢复检查；
9. 相关README、架构、部署、安全、测试和运维文档与实现同步；
10. 至少有一个跨组件集成验证；涉及模型或编码行为的切片还需真实Provider或真实仓库验证；
11. 代码来源、许可证、版权、商标和第三方通知与实际发布物一致；
12. Git Diff仅包含该迭代必要变更，发布声明能够追溯到测试、Eval或运行证据。


## 附录：0.9.4a既有切片证据

以下记录对应各自固定版本，不是新增活动任务。旧段落中的开放项按R1～R6重新分类；延期项不因历史文字重入首发。

### 0.9.4a 原协议帧子切片进展

原相关id与完整封套先于分派准入，完整响应原UTF8字节受协商限额与当前材料保护；
initialize使用纯候选，公开检查通过后原子提交连接状态，关闭Notification不响应。
[详细设计](changes/m09-4a-protocol-frame-publication.md)与[固定证据](validation/protocol-frame-publication-2026-09-28-v1/README.md)
包含旧基线负例、修复后同脚本观察、61项帧测试和1项真实产品CLI子进程验证。
该固定版本中直接Service查询未关闭；后续查询边界进展见下一节。未登记旧历史、全部Provider材料与跨重启Seal仍开放，0.9.4a不勾选完成。


### 0.9.4a 导出查询与同一Session宿主子切片

[查询原DTO详设](changes/m09-4a-query-publication-boundary.md)将六个导出Service查询纳入原Params/DTO/错误保护，
恢复调度后置且关闭不新增Task，构造时Session对象绑定先于Delta订阅。61项查询测试含1开放旧历史观察，
另1项默认产品Root直调验证和2项治理回归；专项181项通过，完整结果以[固定报告](validation/query-publication-2026-09-28-v1/README.md)为准。
未知历史/Seal、全部Provider、内部聚合权限及Request Store物理归属仍开放，0.9.4a保持未完成。


### 0.9.4a 认证历史与跨重启保护实施边界

[源码研究](research/authenticated-history-and-seal.md)、[总体与详细设计](changes/m09-4a-authenticated-history-and-seal.md)和
[ADR-0102草案](adr/0102-authenticated-history-and-event-seal-core.md)覆盖独立Key、原Scope来源、新事件CAS、认证事件链、
派生投影、全部恢复读取、Artifact和备份迁移。Event Seal核心与46项测试已实现，但不代表当前默认Root已启用。
独立实际Root五查询仍可公开未知旧值，无密钥SHA替换仍被接受；SQLite/Root/Key Backend与三平台完整验收必须继续完成，
不得从当前扫描无命中或核心MAC通过推导旧历史授权。0.9.4a及0.9总阶段保持未完成。

认证核心固定实现`829dabf`完成46项核心测试、独立Wheel双OS进程原字节验证、
五幅实际渲染图及5145/32完整回归。相关1128项与专项76项相互重叠。
这仍是前置组件：当前默认Root旧历史和普通投影SHA风险保持开放，
独立Key、SQLite同事务与认证投影、Artifact和三平台部署必须继续完成，发布门禁不变。

### 0.9.4a 显式认证SQLite Session子切片

真实SQLite新事件CAS、原Seal、完整前缀与派生Checkpoint已进入同一事务；
Migration 0029不补签/回写旧历史，原缺失证明失败关闭。实际Runtime/Fork、Artifact混合事务、
资源预算、取消、响应丢失与真实OS进程退出均已验证。
[详细设计](changes/m09-4a-authenticated-sqlite-session.md)区分显式库合同与默认产品。
默认Root/Key Backend、Artifact持久正文/二进制证明与备份/安装仍未接入，0.9.4a和整体0.9不勾选完成。

认证SQLite显式库固定实现`857ce38`新增42项Store和2项证据治理测试，
完整5189/32回归、四幅实际渲染图、候选Wheel双OS消费者已冻结。
[验证目录](validation/authenticated-sqlite-session-2026-09-28-v1/README.md)与正式详设固定原字节和全部来源边界。
默认Root及正式Key Backend由后续托管切片启用；新Artifact正文来源证明由Migration 0030接入。
本段保留原阶段边界，当前仍因旧行、备份/维护、其他出口及三平台验证保持0.9.4a与整体0.9开放。

### 0.9.4a 默认产品托管Session Key子切片

默认Root已强制独立本机Key与认证Session，先验原库再构造Provider和开放Protocol。
缺Key、旧未证明历史不补签/不删除；规范文件/ACL、原候选恢复、超时取消单任务结算有测试。
[总体与详设](changes/m09-4a-managed-session-key-and-root.md)区分本机实际验证、Windows原生测试入口与正式部署。
Key备份/维护CLI、Artifact持久正文、全Provider/Owner/SDK/MCP、12件来源权利及三平台安装仍开放，
不勾选0.9.4a、0.9.5、0.9.6或整体0.9。

默认产品持久Key与强制认证Root固定实现`cef1b17`完成49项新合同（实际macOS43通过/Windows原生6跳过）与2项新证据治理；
完整5234/38回归、相关1448/6、五幅实际渲染图及候选Wheel真实CLI/SDK双进程已冻结。
并发读旧Checkpoint/新Event混读已用确定性双连接测试定位，独立events读事务修复不放宽任何认证。
[正式验证目录](validation/managed-session-key-2026-09-28-v1/README.md)保留Windows、密钥恢复/维护CLI、Artifact正文及整体0.9发布边界。

### R4：原生Windows Git读取实现候选

[固定读取详设](changes/m09-r4-windows-native-git-read.md)对应原生Status/Diff、
原Process Owner整树回收、完整Plan与原备份目录融合及启动只观察恢复。
共享查询同时补齐可执行Filter/Include拒绝与Catalog后EXE绑定漂移拒绝。
原生焦点回归先提供反馈，完整Windows回归保留期限和阻塞诊断；失败不能计作门禁关闭。
实现`4b643f1`修复Windows Owner阻塞控制FD关闭及快速退出后的Job归属误判；
原生焦点34通过、5跳过、1失败，唯一失败进入默认SDK的完整备份Root权限验真。
同Revision两套独立Python环境各1084通过、50跳过，不能替代Windows实际完整备份恢复。
[Git/Owner验证报告](validation/windows-native-git-read-2026-09-28-v1/README.md)保留三轮原生失败及修复证据。
下一主线是R1 Windows默认状态创建与备份私有权限合同统一，随后推进R3真实质量和R4安装；不降低恢复安全契约。
R1/R3/R4/R5仍按真实证据验收，许可证等不阻塞功能研发及内部验证。

### R1：Windows私有状态与完整恢复实现候选

[详细设计](changes/m09-r1-windows-private-state.md)从原生低敏ACL诊断定位创建/验权不一致，
统一产品Root、SQLite、Blob和Process受管目录的私有继承合同；原Key精确合同及原恢复意图不变。
旧式或公开权限只拒绝、不自动改权；默认SDK用例进一步要求完整备份、恢复、原Key保留及重开不重放。
原生实现候选仍需实际焦点与完整回归，R1/R4继续开放；许可证等治理任务不阻挡功能修复。
实现`2c285c0`的原生焦点82通过、5跳过、4失败；权限与父链修复已生效，完整备份仍在叶对象修订检查处失败。
叶修订改用同一原生Handle API，保留FD读前后漂移及Key/ACL拒绝；新增资源回收和字段漂移回归。
该候选尚需原生实际验收，不关闭完整Windows恢复或首发门禁。
实现`8b5944d`原生焦点87通过、5跳过、2失败，前置写链59通过；叶修订和文件Handle回收已通过。
目录发布及默认SDK后继完整备份继续定位，低敏诊断仅补错误类别与数值码，原拒绝不变；两版本本地相关回归各3344通过、81跳过。
后继诊断定位目录发布Win32共享冲突与数据库只读FD同步EBADF；修复候选复用原同目录NT句柄Rename，
SQLite连接关闭后再以原可写私有FD同步。Journal文件、Root切换和拒绝语义保留，实际原生结果另行验收。
实现`a13cec8`原生焦点88通过、5跳过、1失败；目录发布及SQLite同步通过，默认SDK在原排他锁被枚举时仍失败。
枚举改为原元数据访问，继续逐对象验权且原正文读写共享不变；两版本相关回归各3358通过、81跳过。

实现`d615a7b`原生焦点88通过、5跳过、2失败，前置写链59通过；枚举锁自冲突已修复。
默认完整备份进一步发现物理Process输出与原Lease长度/摘要分叉，快速退出码0另有偶发UNKNOWN。
后继实现显式固定输出二进制模式，并复用同目录NT发布让旧Receipt Reader保持原MAC快照；
原生低层IO收敛到Workspace，旧Delivery导入保持，不新增依赖环或放宽安全校验。
[详设第20节](changes/m09-r1-windows-private-state.md#20-原始字节持久化与windows回执并发发布)
保留原失败、字节/共享负对照、实际Owner和原SDK完整恢复；新原生结果未通过前R1/R4继续开放。

实现`f3363f7`原生焦点110通过、5跳过、1失败，前置写链60通过；新二进制实际Owner、
原快速退出重复及默认SDK完整备份恢复均通过。剩余持有旧CRT Reader的共享拒绝保留，
正式Receipt Reader后继改为只读ShareRead/Delete、无Write共享，以原MAC快照协调受控发布；
外部不兼容共享继续拒绝且不自动重放。该增量未替代三平台完整门禁、真实编码及Beta验收。

固定实现`9186cb2`原生事务/审批60通过，Git/Owner/完整备份焦点119通过/5跳过，Session认证88通过。
后继基准223通过/2失败，完整产品重启启动仍失败，后续完整Windows回归未执行；整体Job仍为FAIL。
[统一验证交付](validation/windows-private-state-2026-09-28-v1/README.md)保存七轮原始事实、双Python、
实际图示、Wheel、Manifest、Review Packet及macOS ARM64源码外锁定安装/离线Doctor。
下一功能收口核对重启Runner预建Root与正式私有创建合同，不降低拒绝或重启标准；R1～R6继续开放。

### R1/R4：完整产品重启、发行边界与源码外恢复

`d470ca6`已让原Runner由正式产品创建首次私有Root，Windows真实正反例10项通过，
既有宽ACL仍拒绝且不改权；后继源码制品边界修复不再递归携带验证Wheel，扫描限额保持。
固定`1bc3794`已完成实际构建和2798输入完整Secret扫描、macOS源码外安装与六库/原Key完整恢复。
实际[三平台500 Thread复验](https://github.com/carrie1988/Harnessix/actions/runs/36453376381)
五周期与Thread恢复均完成，但三份原Profile报告唯一`db_growth`越限，均保持FAIL。
增长主要来自新增认证证明表和索引；采用[独立认证负载基线](changes/m09-r1-authenticated-restart-baseline.md)，
保持原500 Thread、五周期及余量规则，先冻结Profile再做第二独立复验。旧FAIL保留，
不新增存储编码或弱化认证。[三平台新基线与Profile](validation/authenticated-restart-three-platform-2026-09-29-v1/README.md)已冻结，
[第一轮独立候选](validation/authenticated-restart-three-platform-candidate-2026-09-29-v1/README.md)Linux/Windows PASS，
macOS因c3-m7与c5-m14资源不匹配保持unverified；执行前准入及版本化Runner已补，
[后继同候选受控复验](validation/authenticated-restart-three-platform-candidate-2026-09-29-v2/README.md)
三平台原报告均PASS，固定认证重启场景关闭。旧FAIL和首轮unverified保留，R1整体及商用门禁仍开放。
[统一交付](validation/product-restart-release-boundary-2026-09-29-v1/README.md)分别记录原生、制品、安装和性能边界，
不把这些增量推导为三平台完整安装、真实质量、Beta或1.0完成；R1～R6仍开放。

R4后继[源码外安装与生命周期验收](changes/m09-r4-installed-product-acceptance.md)复用实际Wheel、
原SDK/CLI及完整状态恢复，增加三平台独立入口和不丢状态的卸载/同Wheel重装。
这是安装验收增量，不代替真实编码、版本升级、消费者OS验收及独立Beta。
固定`e08d248`已取得[三平台原生生命周期成功原件](validation/installed-product-three-platform-2026-09-29-v1/README.md)，
分别完成源码外安装、原CLI/SDK完整恢复、卸载不丢状态及重装会话读取。Windows制品摘要与另两平台不同，
规范单一发行Wheel、消费者目标OS、版本升级和真实编码仍开放；R4及整体1.0不勾选完成。

### R4：固定不同版本停机升级与备份回退

[不同版本详设](changes/m09-r4-different-version-upgrade.md)采用原归档`0.1.0`与内部`1.0.0rc1`候选，
保留唯一规范Wheel构建和三平台源码外运行。每阶段重开隔离解释器，通过原SDK/CLI创建、读取、
备份、恢复和回退，不新增更新平台或迁移算法。包切换不得修改私有状态字节，原Key保持，
升级后Thread必须被旧备份恢复移出当前Root，稳定恢复身份不得回退恢复后新状态。
当前版本标识只用于预发行验收，不创建正式Tag或Release；实际生命周期及原生矩阵结果分别记录，
未取得结果不标记通过。消费者OS核心编码、真实质量、独立Beta及最终同候选R1～R6继续开放。

固定`ec356aa`已取得[三平台不同版本升级、恢复与回退原件](validation/different-version-upgrade-2026-09-29-v1/README.md)：
唯一规范Wheel及Linux/macOS/Windows消费者四Job均成功，原`0.1.0`与`1.0.0rc1`实际离线切换，
原Key、六库恢复、稳定恢复身份及旧包读写验证通过。首轮Windows cp1252失败与本机Apple Git负对照失败均保留。
该固定版本对切片关闭；后续必要主线为真实20 Trial质量、消费者Windows11核心编码、独立Beta及最终同候选发布门禁，
不将内部RC、原生CI平台名称或安装成功外推为正式商用支持。

### R3：固定镜像与显式Engine评测宿主前置

[宿主前置验证](validation/docker-eval-host-2026-09-30-v1/README.md)已恢复原Python/Node RepoDigest、
原8个容器及Daemon配置。默认Desktop容器注册仍Created；同一Engine显式入口完成原Workspace挂载。
无网络验收改为实际接口状态、地址和路由检查，并以真实Bridge负对照拒绝联网环境；
5项真实集成与151项关联离线回归通过，原失效断言FAIL保留。生产代码、Pack及评分器未改变，模型请求0。
该GO只允许后续预注册完整20 Trial验证，不关闭默认Desktop路径、原0/20、消费者Windows11、Beta或商用门禁。

### R3：正式Profile默认参数兼容

固定`629b072`的首次真实Suite在两次模型请求后因评测器误拒绝合法省略Selector停止，
未形成完整Trial/Suite成绩；[失败及修正验证](validation/profile-approval-default-2026-09-30-v1/README.md)
保留原Session和预算事实，不将未完成尝试记为0/20。后继复用产品正式解码器，保持同Profile、零Selector、
额外字段拒绝和原批准指纹；286项关联离线及6项真实容器/录制Provider集成通过。
新候选须重新冻结完整20 Trial，不能跨Revision恢复旧运行。既有CI三个独立失败类别继续开放，
不以参数回归替代真实质量、消费者平台、独立Beta或最终发布验收。

### R3：真实Suite部分结果与费用待核对

固定`0813c58`的新完整20 Trial预注册实际完成4 Case/8 Trial，其中7份invalid、1份failed，无严格通过；
第五Case的第二次模型请求返回`provider_invalid_provider_output`，原预算保护取消Suite并保留未知预留。
[中断证据](validation/provider-suite-interruption-2026-09-30-v1/README.md)记录完整Usage与缺少成功终态的区别，
原70元周期已知估算1.522724元、未知保守占用20.77824元；不自动退款、重试或新建预算周期。
没有完整Suite报告，不登记新的0/20完整成绩；原历史FAIL保留。真实请求待费用核对，离线研发继续。

### R1：直接子进程唯一回收

[唯一回收详设](changes/m09-r1-single-child-reaper.md)定位POSIX后备Transport.kill的Popen.poll与
asyncio Watcher竞争，原生macOS受控延迟实际Watcher复现退出255；后继只发OS信号，不伪造退出码。
五项新焦点通过，进程/Sandbox/Process输入关联271通过、12跳过。Windows取消测试Bootstrap采用
完整PID关闭后独立发布就绪标记，原Lease、停止与EOF断言不变；原生验证及最终同候选门禁仍需实际结果。

同轮077真实容器复验暴露新Workspace创建模式与原恢复合同不一致、录制Oracle模式与Git语义不一致。
[权限详设](changes/m09-r3-eval-workspace-mode.md)仅固定新私有目录FD及离线录制适配器，
旧漂移继续拒绝，正式Patch允许值、真实Provider、Pack及Grader均不变。
[专项交付](validation/single-child-reaper-2026-09-30-v1/README.md)保留每轮FAIL、最终关联回归与实际容器结果；
新同候选原生门禁独立验证，不外推真实质量或商用发布。

固定`0cdad2b`的Windows原生焦点116通过、5跳过、3失败：启动期取消已通过，
另三模式在新测试Bootstrap中提前退出1，具体系统错误尚未从stderr摘要确定。
后继[测试就绪合同](changes/m09-r1-single-child-reaper.md#8-windows测试bootstrap的两阶段就绪合同)
采用PID正文关闭后单独创建空就绪标记，并增加低敏原生发布探针。
生产Job/Share/DACL和原Lease、停止原因、EOF及子进程停止要求不变；新的原生结果需独立取得。

### R3：严格输入的安全字段反馈

9份原Session只读回放确认4次`read_artifact`调用都缺少`artifact_id`；产品编码指令v2实际进入
47次宿主Context准备，Profile已返回Artifact引用，Provider历史包含原失败结果，不归因为未装配指令
或未发布引用。[安全字段反馈详设](changes/m09-r3-safe-tool-argument-feedback.md)在严格拒绝之后，
仅用正式注册模型字段提示必填、缺少及允许字段，不回显模型参数/底层错误，不自动修正或重试。
沿用原分页反馈、批准指纹、Schema、预算、Task Pack及评分器。原真实中断、部分FAIL及未知费用仍保留；
离线SDK修正和回放不能替代后继完整真实20 Trial及商用质量门禁。

专项最终关联1887通过、20平台跳过；18项新回归包含6条双Provider正式SDK修正、实际Artifact读取与
Session重开回放链。原Suite/未知费用保持不变，0次真实模型请求。
完整原件绑定、独立原生状态、治理门禁与Review Packet见
[统一交付包](validation/tool-argument-feedback-2026-09-30-v1/README.md)。

### Chat终态诊断与Windows产品状态夹具

[Chat终态详设](changes/m09-r3-chat-terminal-diagnostics.md)只在原Attempt失败消息记录封闭校验原因，
不保存模型正文、不放宽协议、不改预算或自动重试；原无正文请求的具体根因仍未知。
正式SDK与实际Session关联1376通过、1跳过，不能替代真实20 Trial质量验收。

固定`850c7ba`的Windows输入反馈/Git/四取消模式焦点138通过、5跳过；
完整备份/恢复109通过、1项SETUP错误，Job失败。原生就绪探针旧Rename取得Win32分享冲突32，
新闭文件/独立标记通过，不回推旧未保存stderr。
[测试观测边界修正](changes/m09-r1-single-child-reaper.md#9-原生结果与产品测试观测边界)
去除五秒观察者对后台Turn的取消耦合，复用公开SDK状态；全部恢复断言与产品超时不变。
专项原件、Review Packet与开放门禁见[统一验证包](validation/chat-terminal-diagnostics-2026-09-30-v1/README.md)。
真实费用核对、完整Suite、消费者Windows11、独立Beta、权利及同候选正式发布继续开放。

专项最后受测源码全量5886通过、108跳过；完整治理297项独立通过，范围重叠不累加。
三份Model文件与实际内部RC Wheel字节一致；新候选原生结果尚未取得，不继承旧Job通过。

固定`65d7323`的原生两项观察者实验及原未决恢复均通过，但112项组合步骤在79项通过后被CI进程期限截断。
[完整集合分组](changes/m09-r1-single-child-reaper.md#10-原生验收进程分组与完整性)只调整37/75编排，
所有112项、恢复断言、原步骤保护和产品期限保留；完整原生门禁尚未通过。
[安装手册](operations/installation.md)现行操作使用内部1.0.0rc1及哈希锁定源码外环境，原历史版本事实不改写。

### 固定候选安全锚点、原生分组及宿主对照

固定`df8dc8f`的[原控制锚点复验](validation/v1-safety-coverage-2026-09-29-v1/README.md#7-固定后继候选的原控制锚点复验)
按54个原选择器展开148项，146通过、2原生Windows跳过；不关闭R1完整安全。
[同候选CI事实](validation/chat-terminal-diagnostics-2026-09-30-v1/ci-df8-followup.json)记录37/75原生步骤成功，
完整Windows Job仍活动；双Python各5880通过/116跳过，之后12件许可失败，整体CI不是PASS。

[默认Desktop后继对照](validation/docker-eval-host-2026-09-30-v1/README.md#6-默认desktop启动链的后继对照与宿主回退)
确认无挂载及无attach仍Created，卷注册/gRPC等待有组件证据，但排他根因未证实。
共享后端切换无改善，已回退并核对28个原容器、全部策略及8运行/20停止集合；
回退后的显式同Engine正式录制链6项通过，不替代模型质量。
后继只读观察时Desktop已不运行、两Socket缺失，当前宿主可执行性未经验证；不将历史恢复当作当前健康。
原70元费用未决保护、真实20 Trial、Windows11、独立Beta及最终R1～R6继续开放；不重置账本或自动绕过。

### R1/R4：Rollback原Workspace身份与产品交付接线缺口

[Rollback详设](modules/delivery.md#16-rollback语义)修复原组件API把其他目录、重定位原根或新根对象
捕获为回滚来源的缺陷。读取原Blob前及Planner捕获后均绑定原Workspace ID，拒绝前不保存新事务；
原批准、Schema、Lease、发布状态机及原事务事实不变。文件第三内容的产品选择仍须明确，不混同根身份。

该根身份专项时，产品目录只接通受管Patch和条件Process，Git Commit、Checkpoint及Rollback仍是宿主组件。
后继正式Patch回滚接线见下节；Commit/Checkpoint尚无默认stdio/SDK闭环。[专项证据](validation/rollback-workspace-binding-2026-09-30-v1/README.md)
不关闭R1/R4，也不把0.7组件实现完成推导为首发产品接线完成。
后续沿原R4工作包补产品交付控制、来源绑定、审批及恢复；本地Commit/Checkpoint/Rollback不延期或删减，
公网Push仍延期，真实质量、消费者平台、独立Beta及最终R1～R6保持开放。

### R1/R4：Windows Receipt名称周转候选

固定`a80ea984`的正式SDK Git链因`process_owner_receipt_invalid`失败，原焦点137通过、5跳过、1失败；
后继`6d77b2c`的原NTFS与Git焦点步骤成功，完整Job另行核验，不把一次成功当作排除竞争。
[完整设计](changes/m09-r1-windows-receipt-snapshot.md)不接受无名称旧Handle，
仅在原七次总读预算内重新绑定当前Receipt；原MAC、单链接、安全边界、取消及写端无重试保持。
新增原生Barrier和固定原绑定负对照须取得新候选结果；离线通过不关闭R1/R4，不预先判定原CI唯一根因。
[验证材料](validation/windows-receipt-snapshot-2026-09-30-v1/README.md)保留原失败、模拟/原生差别及受测字节。
产品交付接线、真实编码质量、费用核对、Windows11、独立Beta及最终R1～R6继续开放。

### R1/R4：Git Checkpoint物化前数据保护

[完整设计](changes/m09-r4-git-checkpoint-source-guard.md)在真实Git和SQLite中复现六项原失败：
Checkpoint覆盖受管Worktree的第三内容及计划外跟踪修改，且Lease丢失后继续物化。
后继候选保护原Manifest、完整Index及原生成员镜像，关键写入/保存前复核原Lease；
保留无关未跟踪文件和保存失败后的已知镜像重建，公共签名、Schema、Source HEAD/Index不变。
Checkpoint职责提取到唯一模块，原可读性阈值不放宽，实现摘要绑定新模块字节。
[专项验证](validation/git-checkpoint-source-2026-09-30-v1/README.md)保留原失败和有限结果，
新Windows焦点需固定新候选；组件通过不等于正式Commit/Checkpoint/Rollback接线，R1～R6仍开放。

### R1/R4：正式产品Patch回滚候选

[完整总体与详细设计](changes/m09-r4-product-patch-rollback.md)接入`rollback_workspace_patch`，
以本认证Thread成功原Patch事务为来源，精确匹配原after，新逆向Diff、新批准及独立Action/Transaction。
第三内容、模式/存在性漂移和重复新回滚请求明确拒绝，未知UUID或其他Thread不读取原Blob。
复用原Patch文件执行器、Lease、Artifact与六库备份布局，不新增服务或恢复写入捷径。

[验证材料](validation/product-patch-rollback-2026-09-30-v1/README.md)记录本地真实产品/SDK、
等待后重开、完整备份恢复及两处进程硬退出；ScriptedProvider只代替网络模型，不计作真实模型质量。
新候选Windows焦点保留原NTFS和三分钟保护并增加回滚产品/SDK选择器；原生结果须绑定新提交实际取得。
本地Commit/Checkpoint产品接线、完整真实编码质量、费用核对、Windows11消费者验证、
独立Beta及R1～R6仍开放，内部`1.0.0rc1`不等于正式商用发布。

### R3：编码流程与有效失败观察

后继只读原Session投影按正式Reducer最终Item去重，确认4个Trial先Patch后检查且没有修改前基线；
一个有效失败基线、成功修改和通过最终检查的Trial在汇总前累计Token超限。
[共享编码流程详设](changes/m09-r3-coding-workflow-instructions.md)将这些要求和Profile输出引用取值
明确写入产品/Eval同源v3指令，并保持原指令字节上限；不新增编排器、测试注入或完成豁免。

严格输入反馈、Provider协议与费用保护可独立并行回归，同一源码文件的变更串行合并。
真实Provider仍只允许原持久预算唯一Owner；未决默认拒绝，新Suite仅可使用
[明确单次范围](changes/m09-r3-bounded-reverification-budget.md)，不重置周期或释放未知预留。
[统一验证包](validation/r3-coding-workflow-2026-09-30-v1/README.md)记录焦点22通过、关联2052通过/52跳过、
治理302通过和源码外实际Wheel23通过，集合重叠不相加。9份原Session及原账本读取前后字节一致；
新Wheel440包成员与源码/安装字节一致。0次真实请求，离线装配不是模型遵循率或质量验收。
完整3仓20 Trial、至少12严格成功、每仓成功、零越界、消费者平台、独立Beta及商用R1～R6均继续开放。


### R4：已发布Patch的Git交付来源前置切片

原Git组件以干净来源及修改前Snapshot规划受管Worktree，与默认产品先发布Patch的生命周期不同。
[`正式详设`](changes/m09-r4-product-git-delivery-source.md)明确不可通过放宽脏仓库校验接线；
先实现本认证Thread成功Patch的全集合归属预检、原Route/Transaction证明、连续修改合并和完整当前版本核验。
回滚共用唯一归属Reader，原错误和新批准/恢复保持；来源不读无关正文，不改变文件、Index、HEAD或业务库。

[`来源专项材料`](validation/git-delivery-source-2026-09-30-v1/README.md)记录实际原生文件/SQLite、
正式SDK认证重开、源码外安装与原失败负对照。原Git来源要求、备份六库布局、依赖、预算和R3历史结果不变。
本切片没有广告新的Commit/Checkpoint Tool，不关闭R4；后续依次完成Git基准和备份闭合布局、
完整Diff/独立批准与执行恢复、同候选三平台消费者验收，不删减这些必要项。

### R4：原认证来源到Git HEAD的只读基准

[`基准总体与详细设计`](changes/m09-r4-product-git-baseline.md)绑定原Thread根身份、固定Commit及其Tree、
完整逻辑Index/Status与每条首before的完整blob摘要。选中暂存重叠、flags、非普通对象、修改前用户内容
或前后观察漂移均拒绝；无关用户暂存、未暂存内容不加入交付集合，用户Index/HEAD/Ref不写入。
新增宿主内部读取用途保持原默认绑定与限额，不放宽原干净Git来源、镜像或Review容量。
完整Diff、新批准、Git业务持久化/正式备份闭合与写入恢复尚未接线；该前置能力不关闭R4或商用门禁。

### R4：Git来源根目录原生生命周期验证

固定`b6810c9`的[CI 36731845536](https://github.com/carrie1988/Harnessix/actions/runs/36731845536)
首组Windows实际126通过、1失败、2跳过；共享LF审批/回滚夹具修正已执行，唯一失败发生在
活动产品持有根句柄时测试直接重命名根，系统以`WinError 32`拒绝。后续raw/Git原生步骤跳过。
[生命周期详设](changes/m09-r4-product-git-delivery-source.md#9-windows原生根目录生命周期验证)
保留活动根保护，补退出后相同最终字节的新根在正文读取前拒绝、原文件与账本不变。
[专项材料](validation/git-source-root-lifecycle-2026-09-30-v1/README.md)记录双Python及实际安装Wheel
各69通过、无跳过，集合重叠不相加；新原生结果仍须独立取得。
生产共享标志、根身份、批准和预算不改，R3/R4、消费者Windows11、完整Git交付及商用门禁仍开放。

固定`ece88ad`的[后继原生材料](validation/windows-raw-git-native-2026-09-30-v1/README.md)
已取得首组128通过/2跳过和原Git组166通过/5跳过；新增19个raw/Git用例全部实际通过，属于
第三组232项通过，不重复累加。第三组仍1失败、2错误、5跳过，后继重启/备份/恢复跳过。
长参数名的Windows环境变量超限以短ID保留原负载；普通读端精确8MiB的既有平台比较差异
按原合同验证，并新增共同8MiB+1超限负控。生产上限与比较条件不改；后继`9658402`原生专项步骤成功，
完整Windows Job、消费者环境及商用门禁仍须独立验收。

### R4：Git交付账本只读访问前置

[`只读总体与详细设计`](changes/m09-r4-git-store-readonly.md)在原领域Store增加显式只读模式，
复用原SQLite端口和模型Reader；原六表DDL与版本只读核验，五个公开写入口先拒绝。
默认Writer、原序列化、CAS和版本保持，长类初始化职责唯一提取，不放宽可读性策略。
该模式不是全事件前缀认证或对象归档，不创建默认产品Git目录、不扩大当前六库备份白名单。
完整连续Patch链、Commit/Checkpoint正式接线、对象材料及跨Store恢复闭合仍必须完成，
不将领域Reader增量当作R4或商用发布通过。

[专项交付](validation/git-store-readonly-2026-09-30-v1/README.md)记录新85项真实Git/原v1格式用例，
Python3.12及3.13各537项通过、源码外新Wheel242项通过，集合重叠不相加。
451个包成员、410个源码模块逐字节绑定；治理初始标题缺失FAIL保留，后继同策略302项通过。
四幅设计图实际渲染与检查。后继Windows专项新增只读Selector但不放宽期限；`9658402`实际原生专项成功。
完整Job与消费者OS不从该步骤成功推出，旧失败保留。

### R4：持续Git产品交付、业务备份与新根恢复设计

[完整设计草案](changes/m09-r4-git-delivery-business-backup-closure.md)保留持续多Patch、完整Diff、
Checkpoint/Commit独立批准、持久原生桥接、全事件证明、有界对象材料、共同静默备份及新根重新授权。
原干净来源还校验事务Snapshot的RootIdentity，不能将用户已发布Patch直接传入另一私有根；
当前领域Runner只有同步子进程超时和退出后长度检查，首次产品写接线还必须完成受控IO与进程树回收。
[设计评审资料](validation/git-business-backup-design-2026-10-01-v1/README.md)包含五幅实际渲染图，
明确现有源码与拟新增接口；历史备份范围及材料容量仍为规划合同，本文不开放产品Git写能力。
不得为减少实现量缩成单Patch、删除完整批准、先写后补材料、自动恢复旧注册或回退用户Ref。

[固定候选验证报告](validation/git-baseline-2026-09-30-v1/README.md)记录完整6176通过/111跳过、
源码外实际Wheel132通过、446个包成员字节一致及四份受控变异5失败。
该固定基准中带保护源的Windows原Owner只有脱敏后流证明，旧拒绝证据保留。
后继[原始观察认证与安全发布分离](changes/m09-r4-authenticated-raw-git-observation.md)已接线捕获、v2单一MAC、
终态重验、私有Git消费和完整备份；[专项验证](validation/windows-raw-git-observation-2026-09-30-v1/README.md)
分别记录本机合同、源码外Wheel及原生测试范围。原生Windows、消费者安装及完整产品交付仍须独立验收，
不得通过关闭脱敏或把本地skip计作验收绕过。


### R4：受控Git命令IO与POSIX原始回执

[受控IO详设](changes/m09-r4-git-supervised-command-io.md)提供内部正式端口，
共享原固定命令、环境和物理身份，消费原ExecutionPlan及批准，绑定完整stdin摘要，
整个操作使用同一单调期限；取消后原Owner结算，未知效果不能被普通取消/超时覆盖。

初始真实Git验证发现POSIX仍发布V1，不能满足原始流验真；后继保留严格V2要求，
补齐pipe生命周期原始双流认证，PTY及历史V1语义保持。原Owner捕获和回执构造共用，
不另造进程模型，不放宽治理/额度，不重签历史、不按旧Process ID重放。

此增量仍不是默认产品Commit/Checkpoint接线。8MiB完整业务材料、双受管工作树、
认证关联/全前缀、完整Diff独立批准及Backup v2闭包继续保留为必做项。
原控制输入1MiB拒绝不能解释为缩减业务容量。R3完整20 Trial、消费者Windows11、
独立Beta及同候选最终发布验收仍开放，不因内部端口和离线测试通过而关闭。

### R4：完整固定Git对象读取与原8MiB容量

[固定对象材料详设](changes/m09-r4-git-object-material-read.md)新增原受控IO的正式读取用途，
支持唯一完整OID、blob/tree/commit、SHA1/SHA256及原8MiB正文，原MAC/raw/EOF之后再核对对象OID。
用途和实际结果额度绑定原Plan，普通命令1MiB与控制输入1MiB保持；禁止lazy fetch，不自动补对象。
保护改写和等字节占位符命中均拒绝，Owner与完整返回检查共用同一执行快照；
不拿公开脱敏结果或捕获前缀冒充完整材料。

该读取增量不实现8MiB输入、CAS耐久业务登记、对象目录、默认Git写Tool或Backup v2，
不关闭R3、消费者原生、完整Git产品交付与商用门禁；原完整业务目标不缩减。

### R4：固定响应夹具输入完成与原生失败保留

固定`c64ebb5`的Windows Server原生作业中，原Git受控IO62项全部通过，材料专项208通过、1失败。
原强PID取消与两类身份负例通过；唯一输出保护测试先触发控制通道拒绝，不等于秘密输出已经泄漏。
[响应注入夹具合同](changes/m09-r4-git-object-material-read.md#91-原生响应注入夹具的输入完成合同)
统一有界消费唯一OID及EOF后再输出，保留209个原案例和强断言，补SHA1/SHA256错误OID负例。
另补两个真实Owner终态屏障负例，确定性验证退出后stdin/close_stdin继续拒绝，不冒充历史调度重放。
[专项交付](validation/git-material-fixture-eof-2026-10-01-v1/README.md)区分旧原生失败、本地后继验证和新原生待验；
不重跑覆盖旧日志，不放宽Owner安全或评分合同。8MiB写入、完整Git业务备份、R3、消费者Windows11及Beta仍开放。


### R4：完整对象输入与原 Owner 保护候选

[完整输入总体与详设](changes/m09-r4-git-object-material-input.md)保持三类对象、两种对象格式及8MiB正文，
固定基础解释器子程序沿原小stdin握手，Git前取得完整只读普通文件；不把正文管道前缀当作对象。
完整保护复用原Owner已冻结集合，来源与用途经原Plan批准，过程证明与独立新批准回读分开。
原普通1MiB额度、Owner协议和认证回执不变。快照正文写入前建立取消/死亡生命周期，宿主线程必须排空。
固定1232件源码/测试/脚本/配置输入，关联回归1014通过/43跳过；同一Wheel源码外Python3.12/3.13
分别356通过/2跳过。真实原Owner控制失联复现3失败保留，整个Supervisor退出与实体清理强错误已修复，
五项真实故障回归及独立复核通过；[低敏验证包](validation/git-material-input-2026-10-01-v1/README.md)
登记实际源码、安装字节、结果、旧失败和三幅渲染图。新原生证据仍须独立取得。
本增量不是默认Git写Tool、Commit/Checkpoint、CAS/认证业务账本、阶段崩溃恢复或Backup v2，
不缩减原完整Git交付，不关闭R3、消费者Windows11、独立Beta或商用R1～R6。


### R4：完整对象原 CAS 类型持久化候选

[总体与详细设计](changes/m09-r4-git-material-cas.md)复用原 Workspace CAS，
增加七字段不可变对象引用和完整类型／格式／OID／SHA／长度核验。
原每对象8MiB保持，不建立第二个Blob平台；对象材料写入和完整回读先于返回引用。
原只读 Workspace Store 曾在 SQL 拒绝前落入新正文，现全部写入口先拒绝只读／closed。

真实 CAS → 原批准 Owner 完整输入 → 独立新批准 Git 回读 → 只读 CAS 重开已建立专项。
重启 Soak 的 ACK 最终名在写满前可见缺陷采用同目录不可覆盖原子发布修复，
原消费者单次严格校验、真实硬退出和阈值不变。固定310874c原生失败保留，后继结果另行绑定。
该候选不实现对象图、业务目录／MAC、GitDB／Backup v2或默认Commit／Checkpoint，
不替代完整R3、Windows消费者、Beta和商用R1～R6验收。

### R4：原 CAS 完整普通文件树与直接引用验真

[总体与详细设计](changes/m09-r4-git-tree-closure.md)增加原始 tree／commit 直接引用解析，
并从原 CAS 完整回读根、全部子树和全部普通文件；未修改成员、空文件、二进制和可执行模式均不省略。
对象去重不跳过重复子树的逐路径展开，四项容量必须由可信调用方显式提供；不冻结产品默认容量。
路径复用原 Workspace 合同；缺失、损坏、冲突、链接／gitlink、超限和取消均整体拒绝。

[统一验证包](validation/git-tree-closure-2026-10-01-v1/README.md)绑定1241件完整输入，
新增474项通过，两个无交集关联组共1748通过／58跳过；同一Wheel源码外两个Python各970通过／2跳过。
这些是固定候选的只读材料验真，不证明业务角色授权、目标树来源、批准、数据库MAC或正式备份闭合。
固定37a1f01 Windows首组在原3分钟期限退出；当前仅增加逐项、栈及耗时诊断，保留全部选择器与期限，
不能把macOS焦点通过或诊断增强称为Windows根因修复。

后继主线为完整目标树／Diff、认证对象目录及GitDB全前缀、独立批准、默认Checkpoint／Commit和Backup v2；
R3完整真实20 Trial、消费者Windows11、独立Beta及最终同候选R1～R6继续开放。

### R4：完整目标树纯规划与真实Git差分夹具

[总体与详细设计](changes/m09-r4-git-tree-projection.md)在原CAS完整base、严格首before和必要末after之上，
按路径独立应用净Mutation，保留未修改成员与无关空tree，规范编码全部目标tree正文/OID。
原8MiB对象、32MiB镜像、四项显式宿主限额及两平台路径合同不变；不增加Git写入或产品默认容量。

[统一验证包](validation/git-tree-projection-2026-10-01-v1/README.md)绑定最终1244件输入，新增157通过，
两个不重复最终源码组1909通过/58跳过；同一Wheel源码外两个Python各1131通过/2跳过。
原59e129e CI三个矩阵因研究版本被误作运行版本的精确断言失败；后继夹具使用PATH并记录实际Git，
全部原SHA-1/SHA-256真实差分继续执行，缺失能力仍失败。Windows独立三分钟超时保留，不凭本机结果清除。

后继接通完整Diff及新批准、认证目录/角色与GitDB全前缀、双工作树/新派生事务、默认Checkpoint/Commit、
Backup v2及新根重授权；完整R3、Windows11消费者、独立Beta及同候选R1～R6仍开放。

### R1/R4：Windows材料目录查询权限与真实Git夹具

固定6daff31的CI 36812510928已终结：文档/Container成功，三个macOS/Python矩阵各两项
Git2.55.0与夹具2.53.0精确断言失败；Windows首NTFS组128通过/2跳过、Git读取组成功，
材料组25失败/1260通过/6跳过并达到原五分钟期限。旧FAIL不改写。

[总体与详细设计](changes/m09-r4-windows-material-directory-access.md)修复private终点目录句柄
缺少READ_CONTROL的确定缺陷，保留缓存旧守卫、原ACL、真实读取共享、Owner、MAC及UNKNOWN。
原两格式目标树真实差分复用既有隔离Git夹具，不删除用例或放宽对象能力检查。
[统一验证包](validation/windows-material-directory-access-2026-10-01-v1/README.md)分别绑定
冻结前红测试、关联回归、独立静态审查与后继候选验证；新原生结果仍需实际取得。
不把该单一权限缺陷认定为全部25项根因；R3完整质量、完整Git交付、Windows11消费者及独立Beta继续开放。

### R4：完整目标树与Diff同源内容规划

[总体与详细设计](changes/m09-r4-git-tree-diff.md)复用唯一原Diff算法和原净Mutation严格快照，
完整树与展示内容共用同一深层重建输入；原CAS必要正文二次验真，UTF-8超限整体拒绝。
原Workspace门面、历史展示及身份不变，新Git表示显式区分缺尾LF。
[统一验证包](validation/git-tree-diff-2026-10-01-v1/README.md)保留八项开发失败及全部209项修复后焦点，
并分别绑定最终源码、单一Wheel、源码外双Python、独立审查和四幅实际设计图。
独立审查的两个P2已按新字节闭环，六项分隔符红例保留；最终焦点215通过，源码1422通过／27跳过，
治理302通过；同一最终Wheel源码外两个Python各828通过／4跳过。旧审查前Wheel不混入最终候选。

固定d3175f7原生CI材料组剩2失败、1292通过、6跳过且达到原五分钟期限；原25失败中23本次通过。
该旧候选原生结果不计为新Diff候选通过，也不关闭Windows消费者验收。
后继新批准、对象业务认证、双工作树及派生事务、默认Checkpoint/Commit、Backup v2和新根重授权仍开放；
完整R3、独立Beta及同候选R1～R6继续按原商业退出条件验收。

### R4：Git业务记录有限来源认证

[总体与详细设计](changes/m09-r4-git-record-publication.md)在原Session认证模块增加有限Git记录签发与只验真端口，
复用原独立Key、原冻结Scope、原HMAC和域分离。实际字段集合、UUID/int/str及完整原bytes严格验证，
声明不经先序列化规范化；最终Scope与取消回调之后再次核对Binding开放，关闭候选不得返回。
[统一验证包](validation/git-record-publication-2026-10-01-v1/README.md)保留额外copy字段、标量子类和最后检查点关闭红例，
独立审查P1已闭环；最终焦点193通过。相同105测试文件的源码及同一最终Wheel源码外Python3.12/3.13
各2002通过／33跳过，使用当前锁定71发行版本；不相加为全仓或原生Windows覆盖。
旧迁移生产者夹具仅取精确原归档的深度一对象，安装组补齐原静态Schema/示例，不以当前源码兜底或跳过失败。

固定554618c的[CI 36824593278](https://github.com/carrie1988/Harnessix/actions/runs/36824593278)已终结：
Windows材料组2失败／1289通过／6跳过，303.12秒达到原五分钟期限；两个最低SHA256 Commit场景仍未关闭。
该原生事实不是本新认证候选通过，缺少终态不得计为成功；不调高超时、放宽Owner或重试消除原失败。

来源认证端口没有新增默认工具、数据库、Git写入或备份迁移，不证明对象业务角色、完整前缀及独立尾锚。
后继完整Review Artifact与新批准、双受管工作树、派生事务、默认Checkpoint/Commit、Backup v2和新根重授权继续实施；
R3真实20 Trial、Windows11消费者、独立Beta及同候选R1～R6商用门禁继续开放。

### R1/R4：最低SHA256 Commit单次原生诊断

[详细设计](changes/m09-r4-windows-minimum-commit-probe.md)新增显式pytest插件和manual-only Windows载体，
精确限定原两个失败节点，保留原20秒命令／45秒操作／五分钟步骤、完整材料和pytest退出码。
13接点只调用原函数一次，原返回／异常不改；同调用有界内存投影后，在原清理完成后至多一次同句柄只读补取。
缓存Lease、原MAC终态回执、完整raw、严格proof和整体运行分层记录，缺能力／skipped／UNKNOWN不记通过。
[统一验证包](validation/windows-minimum-commit-probe-2026-10-01-v1/README.md)冻结1255件代码输入，
本机59治理负对照及原两个实际用例通过；单次Windows Run与底层因果必须另取实际记录，不能从本机通过推导。
不修改生产、原测试、默认插件或正式CI，不增加付费模型、预算、Docker或自动重跑；
已发布认证端口与完整Git产品后继按原计划继续，R1～R6商用门禁保持开放。

固定d0ca482的[单次Windows Run36836260240](https://github.com/carrie1988/Harnessix/actions/runs/36836260240)
取得两个原FAIL／零通过／零跳过及两条完整诊断：原输入送达、进程退出二，约1.188秒／1.125秒在exit gate拒绝，
非等待超时；同PID的原MAC终态回执和完整stdout／stderr守卫通过，stdout为空、proof缺失、readback未执行。
stderr正文不出站；实际Windows七模块CRLF字节单独核对，不能以长度／摘要猜内部根因或宣称Git效果已知。
原强UNKNOWN和Windows发布缺口保留，后继定位worker／Git内部错误再做最小修复，不放宽原期限或安全合同。

### R1/R4：最低Commit原认证stderr有限字节信号

[v2诊断设计](changes/m09-r4-windows-minimum-commit-probe.md#11-v2有限stderr信号与兼容设计)
仅在既有后验的MAC／完整raw／protection全部通过后，从内存stderr投影四个求证固定bool。
不增加IO、Git效果、等待、重试或正文持久化；v1历史不改，缺字段与全False分开解释，命中不是根因或业务成功。
[统一验证包](validation/windows-minimum-commit-probe-2026-10-01-v1/README.md#6-v2完整stderr内存字节的有限信号)
固定1255件新代码输入，原59项加28项治理负对照共87通过，完整治理389通过，原两个真实本机用例2通过。
原两份Windows FAIL保留；新v2必须取新的固定原生结果，不以本机或诊断通过关闭Windows／R3／完整Git交付。

固定96aa63c的[单次v2 Run36841600538](https://github.com/carrie1988/Harnessix/actions/runs/36841600538)
终结为两个原FAIL、零通过／跳过；两条完整观察均命中worker固定失败行，三个Git文字消息未匹配。
约1.609秒／1.187秒在原exit gate拒绝；MAC／raw／保护后验通过、成功proof为空。
有限False不能排除原因；原v1 FAIL不变，根因仍需原worker内部结构性证据，不修改权限、断言或期限。

### R1/R4：Git材料Worker有限首失败与原清理观察

[总体与详细设计](changes/m09-r4-git-worker-failure-observation.md)实现固定内部阶段、原wait结果和已求值谓词投影。
清理前首失败冻结与最终捕获类别分开，原Popen／ExitStack／finally资源次数、异常优先级及UNKNOWN不变。
原短路检查顺序保持；新增模块完整进入实现身份，每次源码绑定由六份增至七份，不新增业务材料读取。
[统一验证包](validation/git-worker-failure-observation-2026-10-01-v1/README.md)分别登记失败合同、本机、
同Wheel源码外及原生范围。诊断实现不等于Windows根因修复、默认Git交付、R3或商用门禁完成。

固定4c855c4的[单次新原生Run36853423485](https://github.com/carrie1988/Harnessix/actions/runs/36853423485)
取得两个原FAIL及两条完整v3观察：首失败均在git_validate，Git原wait返回128、worker返回二，
原非零短路保留complete／expected未观察。原成功proof缺失、UNKNOWN不变，不推断具体Git内部原因。
完整Git产品接线、R3、Windows消费者、Beta及同候选R1～R6继续开放。

### R1/R4：Git128固定错误分支低敏观察

[固定分支详设](changes/m09-r4-windows-minimum-commit-probe.md#12-git128固定分支低敏观察设计)
将测试侧车明确升为v4，保留原四字段并增加读取、短读、hash_fd、loose写入和关闭的固定bool。
只有原MAC、双完整raw／EOF及protection通过后才投影，不输出stderr正文或动态后缀。
新增95项先取得78FAIL／17PASS；原95项与新增合并190PASS，两个原本机业务场景通过。
[统一验证包](validation/windows-git-stderr-branches-2026-10-01-v1/README.md)区分未发布并行目录、
同发布候选验证和新原生观察；原469生产文件、两项业务断言、20／45秒及五分钟上限不变。
固定054deb67的[新Run36862927636](https://github.com/carrie1988/Harnessix/actions/runs/36862927636)
已实际终结为FAIL：原两例0PASS／2FAIL，两条v4观察均完整、未截断，各13接点及10模块。
首失败仍为git_validate／Git128／Worker二，Worker字面量命中而其余八信号False，不据此排除未覆盖分支。
十模块实际CRLF字节已与固定Git blob逐件对应；不宣称整个原生checkout已核查或唯一根因已定位。
独立静态增量审查未发现指定范围P0／P1／P2，审查者没有执行函数或业务测试。
原FAIL、完整Git交付、R3、消费者环境及Beta保持开放。

### R4：完整Git对象目录与规范字节契约

[正式详设](changes/m09-r4-git-object-inventory-contract.md)增加七个不可变持久模型、八个纯内存接口，
核对全部声明对象、根／角色闭包、双树逐路径计数、历史parent边界、完整规范JSON及不同用途的内容摘要。
原64MiB记录、8MiB单体与显式图限制保持；原三处复杂度及delivery到session依赖拒绝按职责整改，政策未放宽。
[统一验证包](validation/git-object-inventory-contract-2026-10-01-v1/README.md)绑定完整1263代码输入与单一Wheel，
源码及Python3.12／3.13源码外各1914通过／23跳过，492治理及全430模块类型检查通过。
源码外缺失Schema夹具的两轮原FAIL均保留；补全原259件静态规范后同Wheel通过，没有源码回退或修改断言。
声明内容合同不是实际CAS验真、权威归属或认证尾锚；全前缀、完整Review／新批准、Backup v2、新根恢复及
默认Commit／Checkpoint仍按完整Git产品主线实施，不关闭R3、消费者Windows、Beta或商用R1～R6。

最终职责整改静态独立审查完成，未发现指定增量P0／P1／P2，实际测试执行零；原261通过单独归属。
现行Delivery模块设计已同步七模型、八接口和纯声明／实际CAS／权威边界，原文档拒绝记录保留。

### R4：完整对象目录实际CAS内容验真

[总体与详细设计](changes/m09-r4-git-object-inventory-materials.md)复用原CAS全成员完整回读、
真实Git类型头OID／七字段、原直接tree／commit边、两根完整闭包及全目录并集。
同一checkpoint、原8MiB单体及显式limits保持，无Git／Ref／CAS／SQL写入，不签发授权。
新增主组91项与既有785项共876通过，另4项真实8MiB tree、全部30841叶路径及两路径合同补验通过。
新固定1266代码输入下，源码与同Wheel源码外Python3.12／3.13各2009通过／23跳过；
431模块类型检查、全Ruff／1488格式及修正报告可选元数据后的492治理通过，原失败保留。
内容验真不证明Store／Owner／Key归属、完整认证前缀或新批准；受信装载、默认Commit／Checkpoint、
Backup v2、Windows原生失败及R3／Beta／商用门禁仍按原完整产品目标继续。

### R3：固定7bb候选完整真实结果与原生工具执行整改

固定`7bbce1033925eaf758e295b3c76fc65dee446f30`的3仓10 Case／20 Trial完整真实Suite已经结束，
严格任务成功0/20、必需测试通过1/20，未达到两项至少12/20及每仓严格成功的原门槛。
65次请求均有完整Usage，新增估算1.807932元，无新增未决；原20.77824元全额预留不释放。
估算不是供应商实际账单，旧失败和固定Suite不覆盖、不续跑。
结果、分类、费用、原字段限制及四图详见
[完整真实结果设计](changes/m09-r3-real-suite-result.md)和
[正式验证资料](validation/r3-real-suite-2026-10-02-v1/README.md)。

原20个Session只读求证确认11次单步零工具、10次无实际检查的通过声明及3次文本工具标记；
这些现象不证明唯一模型根因。产品与评测共享指令v4明确原生执行与最终正文格式边界，
两Adapter共用可读前缀加原UTF-8摘要的别名，原内部名称、历史、Schema、批准及严格响应拒绝不变。
正文2749字节不超过原2751限额；窗口、压缩、Token、Task Pack及Grader不改。
没有新增宿主完成门或文本调用执行器，不以提示词整改宣称质量达标。
源码、流程、字段、持久化与测试见
[原生工具执行详设](changes/m09-r3-native-tool-invocation.md)。

同候选179个关联测试文件实际5129通过、57跳过，432个生产源码文件类型检查通过；
该组不是全仓、三平台原生或商业质量验收。独立映射／指令复审660通过，限定范围没有复现P0/P1/P2。
新候选完整真实20 Trial、消费者Windows、独立Beta及最终R1～R6仍开放。

### R1/R4：Git材料显式Trace2诊断与实际Completion缺陷闭环

仅内部完整材料写入允许显式`stderr-event-v1`，固定profile和环境进入原计划与独立批准；
默认`off`保留原命令摘要和wire形状。解释器只在原MAC、PID、完整双流EOF与protection之后
复用已验真的内存，发布七个有限字段；未知不升级，原13 hook和期限不改变。
实际Completion接口负例暴露观察器错误读取`proof`字段，现已改用原`input_proof`并拒绝缺失证明，
原失败完整保留，独立闭环569通过；不是Windows故障根因或原生修复证据。
详见[总体与详细设计](changes/m09-r4-git-material-trace2.md)及
[正式验证资料](validation/git-material-trace2-2026-10-02-v1/README.md)。
完整Git产品接线、Backup v2、R3、消费者Windows和商用门禁仍按既定目标继续。

### R3：同一原复验额度的不可变候选链

后继候选通过显式管理计划追加到原账本v3，保留首次切换及全部旧请求；
前驱摘要、连续序号、完整请求前缀和全部Grant累计费用共同验真，只有末尾Suite可发送请求。
原70元周期、40元复验累计上限及20.77824元旧未决全额预留不变；
任何新增未决继续阻断请求及重开，不新增Grant、不覆盖旧Suite、不自动换绑。
当前实现与原请求宿主联动，关联合同284通过，独立复核120通过；
见[总体与详细设计](changes/m09-r3-reverification-candidate-chain.md)及
[验证资料](validation/reverification-candidate-chain-2026-10-02-v1/README.md)。
首次实际候选登记已完成，原136条请求及其他字段不变；新Suite在首个Case第5次请求后停止，
4次完整Usage估算0.096616元，第5次新增未决保留20.77824元预留，原旧预留仍保留。
见[实际登记与中断报告](validation/candidate-chain-provider-interruption-2026-10-02-v1/README.md)。
没有完成Case或Suite质量报告，不按新0/20评分，不自动重试或释放未决；
完整20 Trial质量及R1～R6商用门禁仍开放。

### R1/R4：Git完整前缀的独立尾锚及最终Scope复核

原Session Binding增加Git尾锚独立用途和HMAC域，仍复用原Key、关闭生命周期、
原64KiB检查点与完整64MiB正文限额；身份／MAC与新用途规范编码先于正文观察。
独立审查发现最终取消检查点关闭Scope后仍返回候选的P1，
正式负例由1失败到1通过，修复后关联合计530通过；原P1及RED不删除。
后继独立复核在相同四SHA下确认原负例10通过、关联530通过及共同关闭优先级2通过。
同一新Wheel的macOS源码外Python 3.12.7/3.13.8各4971通过、57跳过；
初轮管理脚本验证库存缺失造成的两组收集失败保留，仅补齐宿主tracked scripts，不改产品源或断言。
设计、字段、签发／历史验真、持久化边界与失败语义见
[总体与详细设计](changes/m09-r4-git-prefix-publication.md)及
[验证资料](validation/git-prefix-publication-2026-10-02-v1/README.md)。
当前不持久化完整catalog、不迁移GitDB、不新增默认Tool或执行授权；
同事务Writer／Reader、完整Git产品交付、Backup v2及R1～R6仍须继续验收。

### R4：GitDB v2精确结构与正式编码回归

原GitDB v1默认入口不变，后继新增唯一13表结构、组合引用及STRICT约束；
helper复用调用者原事务、逐条DDL，不自行提交/回滚，不修改Schema版本或业务历史。
结构和UTF-8编码核验先于metadata读取，原metadata VIEW提前执行和UTF-16字节边界失败保留；
4项编码回归已进入默认tests选择器。
正式新增363项与原Git/readonly97项共460通过，集成候选435个生产源码文件类型检查通过；
独立复核正式460与私有9项通过，原v1及25个输入零漂移，限定范围无新增P0/P1/P2。
设计见[GitDB v2结构详设](changes/m09-r4-git-store-v2-schema.md)，
当前证据见[集成验证](validation/git-store-v2-integration-2026-10-02-v1/README.md)。
默认产品Writer/Loader、legacy全集迁移、Git交付、Backup v2及新Root重新批准未由结构合同完成；
R3真实质量、消费者Windows、独立Beta及R1～R6商用门禁继续开放。

### R1/R3/R4：同事务认证历史与评测宿主收敛

原Session增加内部完整Thread/事件同读认证接口，复用原MAC、Reducer、容量、绝对期限和资源结算。
真实SQLite回滚拒绝补足Owner异常与实际清理失败优先级；正式62及原关联780通过，独立62+8通过，
两项P2修复保留原RED。普通返回元数据不授Root/当前批准或执行权；
详见[总体与详细设计](changes/m09-r4-authenticated-thread-history.md)及
[验证资料](validation/authenticated-thread-history-2026-10-02-v1/README.md)。

后继Eval宿主复用单一Owner/原Key/Scope，并以此接缝认证恢复历史；
[架构决策](adr/0107-authenticated-eval-host-and-history-read.md)不改评分、Pack或费用规则。
原114通过/1严格预期失败是集成前证据，不是最终通过；原包保持，最终接线与安装须单列实际证据。
正式接线后133项离线回归通过；后继将唯一Thread认证职责从Trial移入原Owner，
保留600/100/20结构阈值并新增3项真实SQLite身份回归。集成29个去重选择器1376通过、
437个生产源码类型与原结构治理通过，原133包和609行结构FAIL保持。
当前[同候选集成报告](validation/authenticated-product-integration-2026-10-02-v1/README.md)
分别记录最终独立复核及源码外新Wheel结果，不以旧候选代替新字节。
独立复核136正式及8私有负例通过，纯格式3文件完整AST相等、两项文档P2闭环；
最终格式源码已重跑1376。唯一新Wheel在macOS两独立Python3.12.7/3.13.8环境各1376通过，
全部478产品成员及各70锁定依赖一致。旧源码只在两固定历史夹具生成子进程加载，
当前及迁移子进程来自新Wheel；首轮22失败/2错误和原准备FAIL保留，不扩大为三平台或质量验收。
真实R3完整20 Trial、费用未决、完整Git产品/Backup v2、消费者Windows和Beta及R1～R6继续开放。

### R1/R4：固定Windows原生分支观察

`730f0846641700c4c697d7cc6ba03cbf1a8364bc`已发布限定硬件执行断点观察，
原Git/PE/PDB/source身份、两条SDK选择器、原Owner/期限及两文件低敏发布合同保持。
[实际Run36969196042](https://github.com/carrie1988/Harnessix/actions/runs/36969196042)首轮终态FAIL，
低敏原件明确`PREFLIGHT_REFUSED`、`execution_performed=false`；未执行Git业务或进入CDB观察。
现有字段不足以区分具体准备原因，历史根因仍UNKNOWN，不能从耗时或工具缺少输出臆定原因。
后继须在同合同内增加有限准备阶段/原因码并取得新的固定原生结果；原FAIL不重跑覆盖。
该诊断不关闭Windows核心编码、完整Git交付、R3、独立Beta或商用门禁。

### R1/R4：固定原生观察的前置诊断与认证发行输入接合

固定观察的前置拒绝补充十二阶段、三十八个有限原因及工具文件存在标志，
未知错误保持`UNKNOWN`，原拒绝状态和非零退出不变。原`730f084`原生运行仍保留
`PREFLIGHT_REFUSED`，没有实际执行分支观察，不能作为SDK成功或根因关闭证据。
诊断源码的限定独立复核为132项正式测试和9项独立负例通过；见
[前置拒绝诊断详设](changes/m09-r4-git-native-branch-preflight-v2.md)。

已发布认证候选`09a386c`的三个精确历史脚本Ruff例外改变了`pyproject.toml`字节，
旧固定合同因此正确拒绝。发行输入接合仅更新基准提交、该输入四个长度/摘要字段及
合同SHA常量；依赖、其余全部合同字段、两项SDK选择器、十三hook与原期限保持不变。
当前两源取得132项正式测试、12项独立负例通过，并实际核验全部十六个固定输入；
不对其他字节漂移或混合换行提供语义容错。设计与证据见
[发布输入接合详设](changes/m09-r4-git-native-authenticated-input-binding.md)及
[限定验证资料](validation/git-native-authenticated-input-2026-10-02-v1/README.md)。
原`730f084`冻结资料保持原件，不能把当前三项输入变化声称为旧清单零漂移。
后继Windows观察仍须固定新的发布提交且仅运行一次；以上离线结果不关闭Windows
核心编码、完整Git产品/Backup v2、真实R3、独立Beta或同候选R1～R6商用门禁。

### R1/R4：固定发布输入后的真实 Git 布局拒绝

固定 `82efa8e` 的 Windows Run36977642845/attempt1 已结束，checkout、锁定依赖及
结果上传通过；观察器在 `selected_git` 以 `selected_git_layout_unrecognized` 拒绝。
[真实结果资料](validation/git-native-layout-refusal-2026-10-02-v1/README.md)保留精确两文件
Artifact、API ZIP摘要和结果摘要。CDB/Python存在标志均为null，不推断工具缺失。
没有执行Debugger或两项SDK用例，也没有重跑旧Run；原Git128根因仍UNKNOWN。
下一步求证实际launcher布局及固定PE身份后作最小适配，不以拒绝诊断或常见路径推断代替原生成功。
完整Git产品、消费者Windows、R3质量、Beta和同候选R1～R6仍开放。

### R1/R4：实际选中 Git launcher 的固定身份布局适配

定点观察器在保留原cmd/core路径的基础上，将同根核心存在的bin/git.exe识别为候选wrapper；
后续完整PE/PDB验真和执行前后重检查始终使用实际selected，不静默替换cmd、不改PATH。
原固定合同、十六件发行输入、两项SDK选择器、十三hook和全部期限保持。
正式回归153通过（原132及新21）；固定官方PE/PDB的项目外验证13通过，
独立复核七例保留基线6通过/1失败，并在修复候选7通过。上述离线证据不替代现场原生验收。
完整接口、角色流程、失败边界及源码映射见
[布局适配详设](changes/m09-r4-git-native-selected-layout.md)和
[冻结验证资料](validation/git-native-selected-layout-2026-10-02-v1/README.md)。
下一步以包含修复字节的唯一固定提交执行一次原Windows观察workflow；
原Run36977642845及其布局拒绝仍保留，实际Python选中映像和原Git128根因未验证。
本适配不证明两项SDK成功，不关闭完整Git交付、消费者Windows、R3、Beta或商用R1～R6。

### R1/R4：固定布局候选已进入原生执行但见证不完整

固定23f12bb的Run36983172137/attempt1已终态FAIL；十六件源码输入、实际选中映像的
原两对完整PE/PDB身份通过，现场CDB/Python存在，且已实际启动调试执行。
有限结果为EXECUTION_INCOMPLETE，debugger_exit与pytest_exit均为1，无超时或日志超限；
两次材料分支见证不完整，案例投影为空，原SDK验收仍为false。
[实际执行资料](validation/git-native-execution-incomplete-2026-10-02-v1/README.md)
保留精确两文件Artifact、API摘要、Run/Job及原始结果；旧两个FAIL均不覆盖或重跑。
单一退出码不能确定Debugger故障、两个SDK的实际失败阶段或原Git128根因；根因继续UNKNOWN。
后继先对已有观察器及帧投影作有限源码和离线复现，不以更多相同运行替代根因求证。
完整Git产品/Backup v2、消费者Windows、R3、独立Beta和同候选R1～R6仍开放。

### R1/R4：有限执行观察与原认证门分离

观察器恢复原Probe单次完整发布前的pytest无换行噪声边界；未完完整标记或尾部partial-prefix
继续粘性拒绝，不从任意文本抽取工具调用。显式v3结果新增独立未认证观察，
只发布原九字段A/B形状及三个0～64语法计数；未知日志为null，64仅表示饱和下界。
原cases、完整branch_gate、SDK验收、退出状态、固定合同和执行期限不放宽。
接口、字段、流程、失败与回退见[完整详设](changes/m09-r4-git-native-execution-envelope-v3.md)，
当前限定证据见[验证包](validation/git-native-execution-envelope-2026-10-02-v3/README.md)。

正式回归227通过（原153及新增74），原本机真实pytest发布链恢复两行但保留失败退出；
该fixture声明不代表Windows或原十三hook的实际执行。独立固定23组投影输入的旧结果与
异常类型保持，未认证形状不能使原false转true，也不因新字段拒绝反向改变原有效门。
固定九源的独立实现复核81项通过；后继详细设计解释性增量及正式文档、Secret检查见
[发布复核记录](validation/git-native-execution-envelope-2026-10-02-v3/PUBLICATION.md)，
原55成员实现封存记录保持，不将其待检查状态冒充发布检查通过。
上述证据不补写历史Run36983172137，不确定原Git128根因；后继原生验证须使用新的固定候选。
完整Git默认Commit／Checkpoint、Backup v2、R3真实质量、消费者Windows、独立Beta及
同候选R1～R6商用退出条件继续开放。

### R1/R4：v3固定原生结果恢复两案例失败投影

固定6e440ff的[Run37077033494](https://github.com/carrie1988/Harnessix/actions/runs/37077033494)
仅执行attempt1并终态FAIL；原十六件输入与selected完整PE/PDB通过，调试执行已启动。
原A/B有限投影均为call failed、worker返回2、Git返回128、proof ABSENT及操作未正常返回；
原v5 raw字段验证不代表独立MAC验真，SDK验收仍false，完整branch见证仍缺失。
新增观察明确FINITE_AB且source invalid=false，日志已测得三个语法计数均0。
这些计数不证明没有创建Git进程、没有附加或回调未发生；Git128根因保持UNKNOWN。

[实际结果资料](validation/git-native-v3-result-2026-10-03-v1/README.md)保留精确两文件Artifact、
API ZIP及结果摘要，Windows CRLF回执不转换。旧三个FAIL不重跑、不覆盖，未新增模型请求。
后继先求证原调试脚本/分支回调与Git材料失败接缝，不重复同一已失败候选；
完整Git产品/Backup v2、R3真实质量、消费者Windows、Beta及商用R1～R6仍开放。

### R1/R4：主映像创建事件布防候选

实际生成字节确认原三处printf为正确单反斜线换行，未修改格式串。
原bootstrap只监听ld:git.exe并忽略cpr；依据Microsoft分离的创建/加载事件合同，
限定改用唯一cpr:git.exe回调，原同名wrapper/core的size、四处机器码、三处PDB符号、
两硬件断点、寄存器谓词及自动继续均保留。未放宽原解析或身份门，也不认定历史Git128唯一根因。

[完整详设](changes/m09-r4-git-native-process-arming.md)及
[限定验证包](validation/git-native-process-arming-2026-10-03-v1/README.md)记录原新例1失败/7通过，
修复后原227与新增8共235通过；16件固定输入、原两个SDK selector、13 hook及全部期限不变。
后继仅以新固定提交验证一次原生执行；旧Run和FAIL保留，离线证据不等于CDB/SDK实际成功。
完整Git默认交付/Backup v2、真实R3、消费者Windows、Beta及同候选商用R1～R6仍开放。

### R1/R4：创建事件候选的现场结果未闭环

固定10d58c5的[Run37080708999](https://github.com/carrie1988/Harnessix/actions/runs/37080708999)
仅attempt1并终态FAIL，checkout、锁定依赖和结果上传通过；原16件输入、selected完整PE/PDB
及工具存在/启动均通过，未超时或日志超限。A/B仍各为Git128、Worker2、proof ABSENT；
三个marker完整语法数仍为0，完整见证及SDK验收false，根因UNKNOWN。

[精确现场资料](validation/git-native-cpr-result-2026-10-03-v1/README.md)保留原两文件ZIP/API/摘要
及Windows CRLF回执。入口合同修复和离线235项通过没有消除此现场失败，不能宣称回调或guard成功，
也不能由0匹配推断没有Git进程。后继先补齐既有调试脚本/材料接缝的实际定位，不重复该候选或旧Run。
完整Git产品、Backup v2、R3、消费者Windows、Beta及同候选R1～R6仍开放，未新增模型请求。

### R1/R4：既有材料失败事实的有限观察接合

原v5已发布的九项stderr bool、Worker失败状态和七字段Trace2，经独立精确sibling
进入原结果的未认证观察；原九字段案例、完整gate、16源、13 hook及期限不变。
[完整详细设计](changes/m09-r4-windows-material-fault-projection.md)与
[现行模块](modules/windows-git-native-observation.md)明确字段、归属、持久化及失败边界。
原观察器235、新专项289、补充266共790项离线通过；独立289专项加一个真实Completion
在明确的支持SHA256 Git宿主通过，和原Apple Git2.24.3前置FAIL分列，不相加为全仓或Windows通过。
精确兼容差分保持280次原结果/门零差异；历史Git128、Worker2、SDK false、Root UNKNOWN仍保留。
未启动新原生Run或模型请求。后继固定现场定位、完整Git效果/Backup v2、R3、消费者Windows、
独立Beta与最终R1～R6仍按原退出条件完成。

### R4：Git IO借用原产品共享宿主

内部Git IO新增原ProductStateOwner、Process Supervisor、Execution Plan Store与冻结
SecretPublicationScope的显式同对象借用；不创建第二套共享资源，不修改原审批、能力、环境、
期限、原始回执或完整8MiB材料合同。端口关闭仅排空自身调用，不关闭产品级资源。
实现、字段、完整流程/时序/数据流、失败、安全及部署见
[共享宿主总体与详细设计](changes/m09-r4-git-shared-process-host.md)。
启动交接异常只停止本次新登记句柄，原同ID重放和其他并行调用不能被停止；
停止结算失败保持强未知且保留原异常与结算异常，不关闭共享宿主掩盖故障。
最终主仓同候选实际169项通过，覆盖共享宿主14项、六项完整8MiB格式/类型写入回读、
133项旧合同、2项实现身份反例及14项真实Owner启动交接/UNKNOWN/取消超时组合；
438件生产源码和450件完整执行输入零漂移，原结构阈值不放宽。
初版155项、直接回收版159项与最终169项分阶段保留，不继承旧字节成绩或叠加重复结果。
该内部接缝不表示默认完整Git父意图/效果配方、A/T/D、Checkpoint/Commit或业务Backup v2完成；
Windows原生故障、R3真实质量、独立Beta和同候选R1～R6继续开放。


### R1/R4：Windows Trace2角色接线与新固定输入

原探针在hook安装前复用固定PE/PDB验真，wrapper的预期Trace2首项精确为git.exe；
原Worker命令、后21项、批准、Owner、MAC/raw/EOF、SID与预算不改。
角色只读内存绑定到实际命令和请求身份，失败不发布部分来源，结算后清空。
16件源码成员保持，四件已知源码更新精确LF/CRLF身份；原九件整体字节门禁保留。
完整六件相关治理测试实际767通过、无失败/错误/跳过，507件有限执行输入零漂移；
其中479件生产成员含438件Python源码，不是全仓或Windows现场验收。
详见[总体与详细设计](changes/m09-r4-windows-trace2-role-input-binding.md)及
[限定验证资料](validation/windows-trace2-input-binding-2026-10-03-v1/README.md)。
旧冻结失败及Run37089114490仍保留；新原生固定结果、完整Git产品/Backup v2、R3真实质量、
消费者Windows、独立Beta和同候选R1～R6仍开放。


### R1/R4：角色接线后的真实对象插入失败观察

固定9a0d84a的原生Run37099316276、attempt1已终态failure，无超时或重跑。
16源码/固定PE/PDB合法，两Case首次均有ENTRY_START_MATCHED、DISPATCH_HASH_OBJECT、REPO_EVENT_SEEN
及MATCHED_128；仍UNCLASSIFIED_FORMAT/UNKNOWN，已知HASH_OBJECT_ADD_AGGREGATE。
Git128/Worker2、proof ABSENT、SDKfalse、RootUNKNOWN及缺branch见证保持失败，
详见[固定结果](validation/windows-trace2-role-native-2026-10-03-v1/README.md)。
后继从固定官方错误格式及对象插入链求证，不忽略UNKNOWN或重启同一Run；
完整Git/Backup v2、真实R3、Windows消费者、独立Beta和商用R1～R6仍开放，费用预留不变。

### R1/R4：精确静态格式与有限失败发布接合

固定官方源码确认三处普通error模板未登记；静态目录由6项增至9项，仅精确完整匹配，
动态errno尾部、近似模板和未知事件仍UNKNOWN。原失败Sibling同步三个固定ID，字段不扩张；
目录缺口不是Git128唯一根因，有限分类不改变branch/proof/SDK成功门。
原16件发行输入逐字节保留并追加profile及失败发布器两件完整源码，共18件；
角色阶段17叶类型敏感历史回归、原九件整体guard、预算、selector、PE/PDB及旧现场FAIL保持。
总体结构、接口、字段、流程/时序/数据流、伪代码、安全和验证边界见
[详设第12节](changes/m09-r4-windows-trace2-role-input-binding.md#12-精确静态错误目录与失败发布接合)。
新候选八件文件实际944项通过、811件完整输入零漂移；独立审查的原selector覆盖缺口以1项RED及9项定向闭环，原成功标准不变。
详见[限定验证](validation/windows-trace2-static-formats-2026-10-03-v1/README.md)，离线与原生分别登记，不累加历史成绩。
完整Git/Backup v2、真实R3、Windows消费者、独立Beta及商用R1～R6继续开放，未新增模型请求。

### R1/R4：精确静态目录候选的原生失败保留

固定9b9e52f的Run37103867851、attempt1终态failure；18源码/PE/PDB合法，两个SDK仍Git128/Worker2、
proof ABSENT、RootUNKNOWN，未观测三个新增静态ID。Trace2仍UNCLASSIFIED_FORMAT，CDB arm/branch均0。
结果与原件摘要见[固定现场验证](validation/windows-trace2-static-native-2026-10-03-v1/README.md)。
该结果排除以本次静态目录扩展宣称现场根因已修复；不自动重跑、扩张诊断或降低原成功条件。
完整Git/Backup v2、R3、Windows消费者、独立Beta及商用R1～R6继续开放，未新增模型或费用。
