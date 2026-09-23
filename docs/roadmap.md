---
doc_type: roadmap
status: current
version: 77
code_revision: 823ceac0c7ec250bb36cd0009946949ad8f094d2
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

状态：**核心运行能力已交付，整体0.4仍待0.4.3c计价适用性验收后关闭**。0.4.1 / 0.4.2a / 0.4.2b1/b2 / 0.4.3a/b1/b2已完成离线验收，双Adapter、尝试账本、SDK用量/计费元数据映射、显式价格绑定的成本报告和受控Smoke/白名单诊断已经实现；百炼文本、内存工具和审批重开已实测通过。0.4.3c作为跨版本发布证据继续跟踪，不阻断已经独立验收的0.5/0.6，但必须最迟在0.9发布候选阶段关闭，未关闭时不得发布1.0。见[0.4实施计划](m04-model-runtime.md)。

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

状态：**进行中**。0.9.0、0.9.1、0.9.2及DOC-1.0～DOC-1.6已完成，当前26/26个生产源码包均有独立现行模块设计，仓库内文档受版本化元数据、职责、生命周期、链接、追踪和差异同步门禁约束。0.9.1f3物理删除独立HTTP/Worker体系；0.9.2完成3仓10 Case/20 Trial离线与真实Provider基线，关闭Revision `6dd391a`由[CI 35492831821](https://github.com/carrie1988/Harnessix/actions/runs/35492831821)完成六实例验收，真实Suite的0/20严格结果已[冻结](validation/provider-engineering-2026-09-20-v1/README.md)。0.9.3a本地传输可靠性已由[CI 35494960166](https://github.com/carrie1988/Harnessix/actions/runs/35494960166)关闭；0.9.3b实现Revision `cb3f3ea`已由[CI 35498012926](https://github.com/carrie1988/Harnessix/actions/runs/35498012926)完成六实例验收；0.9.3c修复版Revision `33fcf02`已完成双层Owner、Operation Deadline、只对账恢复、跨Store扫描并由[CI 35691402329](https://github.com/carrie1988/Harnessix/actions/runs/35691402329)六实例关闭；0.9.3d及0.9.4～0.9.6未完成，因此0.9阶段整体仍保持进行中。

### 目标

从“功能完整”进入“代码可长期维护、真实用户可长期使用、问题可诊断、质量可回归、发布声明有证据”的候选版本状态。

### 纵向切片

- [x] **0.9.0 代码可读性、可维护性与结构治理**：建立可复现的源码规模、文档字符串、复杂度、依赖和公共API基线；制定简体中文注释、命名及模块边界规范；按Agent核心状态机、副作用与恢复、产品运行时与扩展、模型/Context/Eval的优先级补齐模块、类、函数和关键不变量说明；在独立ADR和行为保持测试约束下治理超大文件与过重职责；渐进建立新增及变更代码的可读性防退化门禁。不得以机械注释覆盖率替代语义质量，不得把功能开发、公共契约变更或无关重构混入本切片；
- [x] **0.9.1 CLI/TUI产品体验**：完整交互、流式消息、计划、工具进度、Diff、审批、成本、会话管理、配置向导、环境检查和错误自助；完成Windows原生只读Coding Tool Runtime与统一Action的产品装配，不以WSL兼容替代原生端口；a～f全部子切片已经对应全矩阵CI验收；
- [x] **0.9.2 Eval与Transcript基线**：覆盖Bug Fix、Feature、Refactor、Test和Review的多仓库任务集，记录任务成功率、测试通过率、人工干预率、Token、成本和延迟；离线20/20执行链与真实Provider 0/20严格质量基线均已冻结；
- [ ] **0.9.3 可靠性与性能**：长会话Soak、进程/数据库/客户端故障注入、并发与锁、内存、启动时延、Artifact和数据库增长基准；
- [ ] **0.9.4 安全、许可证与供应链**：攻击测试、AGPL/商业双许可权利链、依赖和许可证扫描、SBOM、Secret扫描、安装脚本与扩展来源审查；为Trusted Action Runtime补齐Policy/Executor/Reconcile异常的统一公开错误清洗和泄漏回归测试；远端MCP Streamable HTTP/OAuth须在本切片建立独立目标身份、凭据生命周期和受管出口；
- [ ] **0.9.5 安装、升级与Dogfooding**：macOS/Linux/Windows发行物、全新安装、跨版本升级、备份恢复、卸载、诊断包、受控Beta和缺陷关闭；公网Git认证须在本切片完成独立Secret作用域、known-hosts/凭据Helper和三平台验收；
- [ ] **0.9.6 Provider发布证据**：关闭0.4.3c计价适用性，完成受控真实Provider Smoke、能力矩阵、成本适用边界和脱敏验证。

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
- [ ] **0.9.3d 长会话Soak与发布阈值**：用固定场景和环境记录启动时延、操作分位数、峰值RSS、数据库/Artifact增长、
  清理水位和故障计数，冻结低敏Manifest与独立阈值验证。源码核查、ADR和专项详细设计处于评审阶段；
  已实现严格样本/Manifest、Run提交与独立重算、三平台RSS适配、长会话真实Runtime及多Thread应用服务
  缩小负载Runner，并补齐多Thread启动恢复时延指标合同。已归档[macOS 500 Thread单次诊断事实](validation/soak-macos-2026-09-23-v1/README.md)，
  但该Revision的跨平台基准Job失败，不能用于冻结Profile；后续拆分启动/分页期限并先排空超时SQLite任务的修复版已由[CI 35804642232](https://github.com/carrie1988/Harnessix/actions/runs/35804642232)六实例验收，旧Run仍仅供诊断。现有四个Runner已在负载前持久写入Attempt开始事实，异常保留失败终态，硬退出保留未完成事实；
  [macOS单Thread连续1000 Turn规模诊断](validation/soak-macos-2026-09-23-v2/README.md)已在干净Revision完成并经Run/Attempt双重重算，对应六实例CI通过；该旧Run尚未专门断言Context/Compaction。新的[长会话Context/Compaction v2详设](changes/m09-3d-long-session-context-proof.md)已落地逐Turn事件Proof和v1/v2独立Reader；[macOS一次v2千Turn规模基线](validation/soak-macos-2026-09-23-v3/README.md)已完成1000次正式Context检查、199次压缩摘要/窗口及Run/Attempt重读，实现Revision六实例CI通过。新的[500 Thread正式负载诊断](validation/soak-macos-2026-09-23-v4/README.md)完成Run/Attempt和45条样本重算；对应Revision的Windows Product UI首次尝试两项超时、第二次重跑成功，原因未明，不能升级为可冻结Profile的基线。Artifact真实Runner已有本地合同与失败回归；Action恢复与重启两个场景Runner及其失败事实、Linux/Windows正式负载、其余场景正式Threshold Profile冻结和三平台独立复验仍未完成；
  [单平台阈值独立复验内核](changes/m09-3d-threshold-verification.md)已实现冻结Profile与完整Attempt双重核验；候选在负载前将Profile ID/摘要持久写入STARTED v2，最终Manifest必须同值；显式工程余量、全部分位数/文件增长比较及不可覆盖报告已具备。[Artifact增长场景详设](changes/m09-3d-artifact-growth-soak.md)进入评审，v3低敏Proof、Manifest、独立Reader和真实Agent/Tool/Store Runner已实现，包含混合件、全页读取与逻辑到期清理；[首次macOS规模Run](validation/soak-macos-artifact-2026-09-23-v1/README.md)与[第二次规模Run](validation/soak-macos-artifact-2026-09-23-v2/README.md)均因各自Revision的Windows Benchmark句柄清理失败仅归档为诊断；两个独立缺口分别为同步只读连接关闭和异步连接取消收尾，[修复后macOS单平台Artifact规模基线](validation/soak-macos-artifact-2026-09-23-v3/README.md)已在干净Revision重跑、独立重算并由[CI 35824543623](https://github.com/carrie1988/Harnessix/actions/runs/35824543623)完成六实例验收。Linux/Windows正式负载、复合故障和工程阈值仍未验收。除SDK容量场景三平台Profile外，尚未冻结其他经工程评审的正式数值，除SDK容量固定场景外也没有三平台独立复验，Action恢复与重启两个未实现Runner场景仍被拒绝冻结。SDK容量场景已新增[真实SDK/stdio容量Soak详设](changes/m09-3d-sdk-capacity-soak.md)、v4低敏Proof、独立Reader和64容量/迟到Response全路径回归；[macOS单平台正式规模基线](validation/soak-macos-sdk-2026-09-23-v1/README.md)已在干净Revision完成并独立重算，对应[CI 35833762475](https://github.com/carrie1988/Harnessix/actions/runs/35833762475)六实例成功。[三平台固定负载采集通道](changes/m09-3d-sdk-cross-platform-evidence.md)已完成真实Linux/macOS/Windows Job；[SDK容量三平台一次正式基线](validation/soak-sdk-three-platform-2026-09-23-v1/README.md)及原始Run/Attempt与摘要已复核，[CI 35838258049](https://github.com/carrie1988/Harnessix/actions/runs/35838258049)六实例成功。基线阶段各平台各有一次正式Run；[SDK冻结Profile与候选详设](changes/m09-3d-sdk-frozen-profile-candidate.md)及签封原件已建立；[三平台候选Run及PASS报告](validation/soak-sdk-three-platform-candidate-2026-09-23-v1/README.md)已完成三平台第二Run与独立PASS报告，对应Revision的[CI 35843734178](https://github.com/carrie1988/Harnessix/actions/runs/35843734178)六实例成功，SDK容量固定场景门禁关闭。既有Windows Product UI超时低敏诊断仍不等于偶发超时根因关闭；[新增退出期限诊断](validation/product-quit-windows-2026-09-23-v1/README.md)记录归档Revision的Windows首次失败与重跑成功，[20秒预算修复详设](changes/m09-3d-product-quit-close-budget.md)须由新Revision跨平台CI验收。单次基线和缩小负载均不得充作发布PASS。

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
- 多仓库任务集覆盖五类软件工程任务，并为不同难度、语言生态和工具链输出可复现分层结果；
- 每次关键变更能够输出与固定基线比较的Eval报告，不以单一任务或单次模型结果宣称质量；
- 连续故障注入和Soak后无Session损坏、孤儿进程、未归因文件和重复外部副作用；
- 受控Beta发现的发布阻塞缺陷已关闭或有明确降级边界；
- 新用户仅依据正式文档即可完成安装、配置、首个真实任务、恢复和卸载。

## 12. 1.0：本地优先正式商用发布

### 发布范围

- [ ] 稳定的Agent、Tool、Provider、Context、Session和Protocol v1契约；
- [ ] OpenAI-compatible与Anthropic Provider，以及明确的能力和计价适用边界；
- [ ] 读取、搜索、通用进程、多文件修改、测试、Diff、Checkpoint、Rollback和Git交付闭环；
- [ ] 持久Session、Resume、Fork、Retry、Archive与Context Compaction；
- [ ] Host/Container执行、安全策略、审批、网络和Secret；
- [ ] MCP、项目指令、Skills和Hooks；
- [ ] CLI/TUI、Headless App Server和Python Agent SDK；
- [ ] Trusted Action Runtime外部副作用治理；
- [ ] macOS、Linux和Windows安装、升级、恢复、卸载和诊断；
- [ ] 可复现Eval、质量报告、安全文档和运维资料。

### 发布门禁

- [ ] 所有公共Schema有版本、兼容窗口和废弃策略；
- [ ] 所有数据库及持久Artifact变更有向前迁移、备份恢复和回滚说明；
- [ ] macOS、Linux和Windows全新安装、跨版本升级、失败回滚和卸载验证通过；
- [ ] 用户数据导出、删除、保留和诊断脱敏策略经过测试；
- [ ] 安全文档、Threat Model v2、Sandbox、网络、Secret及扩展供应链完成审查；
- [ ] 默认CI完全离线，受控真实Provider门禁独立且可审计；
- [ ] 0.4.3c及所有跨版本发布债务已经关闭；
- [ ] 不存在未分类或未处置的发布阻塞级可靠性与安全缺陷；
- [ ] README中的每项当前能力声明都有可运行证据；
- [ ] 发布物可复现并具备版本、校验摘要、SBOM、Changelog、迁移说明和支持矩阵；
- [ ] AGPL社区许可证、商业授权边界、贡献权利链、商标规则和第三方通知完成发布审查；
- [ ] 0.9固定Eval、Soak和受控Beta达到预先冻结的发布阈值，不在看到结果后降低标准。

1.0的规模声明限定为大量相互独立的macOS、Linux和Windows本地实例。该版本不宣称多租户隔离、云端高可用、远程执行池或集中式服务SLO。

## 13. 1.x与后续演进

只有1.0单Agent本地产品稳定且真实用户证据充分后，才评估以下方向。

### 云端运行候选

- 远程Sandbox与云任务；
- 身份、租户、配额、限流和滥用治理；
- 云端Secret/KMS、对象存储和数据生命周期；
- 分布式Session、任务调度和Agent Worker；
- 团队策略、集中审计、计费、服务SLO、备份和容灾。

### 产品与能力候选

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
8. `make check`通过；
9. 相关README、架构、部署、安全、测试和运维文档与实现同步；
10. 至少有一个跨组件集成验证；涉及模型或编码行为的切片还需真实Provider或真实仓库验证；
11. 代码来源、许可证、版权、商标和第三方通知与实际发布物一致；
12. Git Diff仅包含该迭代必要变更，发布声明能够追溯到测试、Eval或运行证据。
