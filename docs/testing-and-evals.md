# Harnessix Code 测试与 Eval 规范 v1

- 状态：0.2 架构基线，随 0.4 实现持续更新
- 更新日期：2026-09-03

实施进展（2026-09-03）：0.3 范围本地验收完成。tests/agent 覆盖语义 Item、持久审批、统一错误、SQLite 事务、取消、混合版本 Replay、真实 v1/v2→v3 升级和 OTel 内存导出；进程矩阵包含 7 个核心、10 个审批、9 个语义 Item 边界。tests/contracts/session.py 提供 SessionStore 共享契约；真实模型有效性和真实编码 Evals 仍在后续阶段；详情见 [Kernel 实施设计](m03-runtime-kernel.md)。

0.4.2b1 收口快照（2026-09-03）：尝试账本领域/Kernel、累计用量去重、未知值与完整性、身份绑定、失败/取消结算、预算与 OTel 差额、v1/v2/v3→v4 混合升级及历史 Schema 冻结。当时新增 15 个模型尝试子进程崩溃切点，共 41 个。

0.4.2b2 收口快照（2026-09-03）：两个真实 SDK 的尝试/缓存/推理/失败用量映射、HTTP 前持久意图、累计值与迟到分项、取消和合法观测保留。新增 8 个 SDK 子进程切点，全项目合计 49 个；每次恢复均验证不重发请求。当时 `make check` 为 546 passed、1 skipped；异步调试下 Kernel + Provider 为 510 passed，详见 [ADR 0017](adr/0017-provider-attempt-usage.md)。

0.4.3a 收口快照（2026-09-03）：新增 96 项价格/成本测试，覆盖严格十进制字符串、整数定点精度、未知与显式零、计费上下文/模式/TTL/生效期/输入阶梯、失败尝试与重试去重、跨币种、旧步骤、JSON 重算与内容错绑、双 SDK → Kernel/SQLite → 报告 Replay。`make check` 为 **642 passed、1 skipped**（本地 PostgreSQL 未配置），异步调试回归 **606 passed**；新增两个独立 Schema，不改变历史 Agent/Provider Schema。真实价格、计费上下文自动采集与平台验证未验收，见 [ADR 0018](adr/0018-versioned-token-cost.md)。

0.4.3b1 收口快照（2026-09-03）：`tests/smoke/` 新增 **94 项**，包括两个实际 SDK × 三场景、私有临时 Session、Kernel 重开/审批/Replay、默认门禁、配置/预算、错误/不重试、超时/Task 取消、CLI 参数/正文 canary 和真实 SIGINT 子进程。全量 **736 passed、1 skipped**；异步调试下 Kernel + Provider + Smoke **700 passed**。注入传输验收不代表真实平台通过；报告白名单不承诺对不可信语义内容做通用 DLP。详情见 [ADR 0019](adr/0019-controlled-model-smoke.md)。

0.4.3b2 收口快照（2026-09-03）：新增 **69 项**，覆盖原生响应计费元数据、迟到/去重/漂移、严格 TTL 分项、原子提交、直接平台映射与价格绑定冲突、真实 v4 升级；全量 **805 passed、1 skipped**，异步调试 **769 passed**。新增 5 个硬崩溃切点，全项目 **54 个**，另有 2 个 SIGINT 用例。旧 Schema 冻结、旧读者拒绝、独立 wheel 与六个离线入口通过。设计见 [ADR 0020](adr/0020-observed-billing-context.md)。

真实验证与默认 CI 分开：百炼北京首次工具解析失败，定位确认空 ID 增量兼容问题；修复后文本/内存工具/审批重开均有真实通过证据。兼容修复新增 6 项回归（先复现 2 failed，再全部通过），随后并发初始化暴露 WAL 忙锁并完成根因修复，另补 8 项存储回归（见 [ADR 0021](adr/0021-session-wal-initialization.md)）；当前全量 **819 passed、1 skipped**，异步调试 **783 passed**；不将固定场景通过等同于全模型兼容或 Coding Eval，见 [验证记录](validation/bailian-2026-09-03.md)。

## 1. 目标

0.4.1 增量（2026-09-03）：新增 `tests/contracts/provider.py` 共享行为契约与 `tests/models/` 的实际 OpenAI SDK + MockTransport 测试，覆盖流分片、协议错误、重试/取消、错误 body 清理、凭据边界、Kernel 多步骤与审批重启。默认测试不访问真实平台；真实模型有效性和成本验收尚未完成（Anthropic 已在 0.4.2a 完成离线验收）。具体状态见 [Model Runtime](m04-model-runtime.md)。

Harnessix Code 的测试必须回答两类不同问题：

1. **Runtime 是否正确**：状态、持久化、取消、权限和恢复是否满足契约；
2. **Agent 是否有效**：在真实仓库任务中能否以合理成本完成正确修改。

不能用几个成功 Demo 代替 Runtime 正确性，也不能只靠单元测试宣称 Coding Agent 有用。

## 2. 质量属性

0.4.2a 收口快照（2026-09-03）：两类 Adapter 分别通过同一 Provider 契约。Anthropic 使用实际 SDK + HTTPX2 Transport，覆盖原始 SSE 强类型校验、未知事件、缓存总量、取消/错误 body，以及会话和审批边界切换 Provider。其后明细/失败用量已在 b2 补齐离线验收；真实 API 未验收，不以 Mock 通过替代。

优先级：

1. 副作用安全；
2. 状态和恢复正确；
3. 代码修改正确；
4. 安全边界；
5. 可重复和可诊断；
6. 延迟、Token 与成本；
7. 交互体验。

质量指标出现冲突时，不用更高任务成功率换取未知副作用自动重试。

## 3. 测试分层

### 3.1 Unit

覆盖纯领域逻辑：

- ID、状态转换和终态保护；
- Provider Stop/Error/Usage 映射；
- Tool 参数规范化与输出边界；
- Permission Rule 与 Approval Fingerprint；
- Token Budget 和 Context Fragment 选择；
- 路径、Effect Class、Retry/Reconcile 决策；
- Redaction。

要求无网络、无真实模型、毫秒级执行。

### 3.2 Contract

每个可替换端口都有共享测试套件：

| 端口 | Contract |
|---|---|
| ModelProvider | 流事件顺序、Chunk 组装、Usage、Error、Cancel |
| Tool | Schema、生命周期、超时、取消、输出、Effect |
| SessionStore | 事务、sequence、幂等、Migration、Replay |
| SandboxBackend | 文件、进程、网络和资源能力 |
| ActionExecutor | 幂等、UNKNOWN、Reconcile |
| Agent Protocol | JSON-RPC Schema、版本、顺序、重放、背压 |

新增 Adapter 必须通过已有 Contract，不允许为某 Provider 修改 Runtime 测试期望。

### 3.3 Integration

真实组合但尽量不使用公网：

- Agent Runtime + SQLite + Scripted Provider；
- Tool Runtime + 临时 Git Workspace；
- Process Runtime + 实际子进程树；
- App Server + stdio JSONL Client；
- Action Plane + SQLite/PostgreSQL；
- Context Compaction + Fake Summarizer。

### 3.4 End-to-End

在隔离临时仓库运行：

~~~text
读取问题 → 搜索代码 → 修改 → 执行测试 → 查看 Diff → 最终回答
~~~

E2E 断言最终 Git Diff、测试状态、事件序列、Tool 次数和遗留进程，而不是只检查自然语言回答。

### 3.5 Fault Injection

在每个持久边界注入：

- Python 异常；
- Task Cancel；
- Provider 断流；
- Tool 超时；
- 进程强制退出；
- 数据库 busy/磁盘满；
- 网络超时与响应丢失；
- Sandbox 启动失败。

### 3.6 Security

与[威胁模型](threat-model.md)逐项对应：

- Prompt Injection；
- path/symlink/TOCTOU；
- Shell/进程树；
- 禁网；
- Approval bait-and-switch；
- Secret canary；
- 恶意 MCP/Hook；
- 协议 Fuzz；
- UNKNOWN 无重复对账。

## 4. 确定性测试基础设施

### 4.1 FakeProvider

最小同步/异步 Provider，用于返回固定最终响应和错误。

### 4.2 ScriptedProvider

输入为 Provider Event 脚本，支持：

- 任意文本和 Tool 参数 Chunk；
- 多 Tool Call；
- Delay 和 Barrier；
- 指定事件处抛错；
- 指定事件处等待 Cancel；
- Usage/Stop Reason；
- Context Overflow 和 Rate Limit。

### 4.3 Transcript Replay

读取已脱敏 Event Transcript，验证：

- 相同输入产生相同 AgentEvent；
- Snapshot 重建一致；
- 客户端关键事件顺序一致；
- Provider/Tool 不被真实执行。

Delta 分块可不同，但 Item 终值和领域终态必须一致。

### 4.4 FaultPoint

正式实现中的关键事务边界使用可测试 FaultPoint 标识，不在业务代码散落测试专用条件：

~~~text
after_turn_started
after_provider_event
after_tool_call_committed
before_tool_effect
after_tool_effect
before_tool_result_committed
after_cancel_requested
before_turn_terminal
~~~

生产默认 NoOp；测试 Harness 注入异常或进程退出。

## 5. Agent Loop 场景矩阵

| 场景 | 期望 |
|---|---|
| 单轮无 Tool | Assistant 完成，Turn COMPLETED |
| 单 Tool | Call 先于 Effect，Result 先于下一 Model Step |
| 多 Tool | ID 与 Result 不串线，按并发策略执行 |
| 未知 Tool | 模型可见失败，不执行任何效果 |
| 非法参数 | Tool FAILED，不进入 Handler |
| 最大步骤/Token/时间 | 结构化预算终止 |
| Provider 限流且无 Tool | 有限退避重试 |
| Tool 已提交后 Provider 失败 | 不重复 Tool |
| 等待审批取消 | Approval/Turn 明确终态 |
| 本地写中崩溃 | Reconcile Workspace，不盲目 Patch |
| 外部写结果丢失 | Action UNKNOWN，重复效果为 0 |

## 6. Crash Recovery Matrix

每种 Effect 至少测试以下切点：

| 切点 | PURE/READ_ONLY | LOCAL_WRITE | EXTERNAL_WRITE |
|---|---|---|---|
| Call 提交前 | 无调用，可重试命令 | 无调用 | 无调用 |
| Call 提交后、Effect 前 | 可建恢复 Attempt | 可建恢复 Attempt | 可安全重新调度 Action |
| Effect 中 | 按定义重试或中断 | 检查文件/Git | UNKNOWN/Reconcile |
| Effect 后、Result 前 | 可重新观察 | pre/post hash + Diff | 幂等查询/Reconcile |
| Result 提交后 | 不重复 | 不重复 | 不重复 |

断言不仅是状态正确，还包括外部效果计数、文件内容和事件因果链。

## 7. Protocol 测试

- JSON-RPC Golden Request/Response；
- initialize 前置和版本不兼容；
- requestId 同载荷幂等、异载荷冲突；
- Event sequence 单调与缺口恢复；
- Delta 合并、丢弃和 Snapshot 回补；
- 服务端 Approval Request 与 Response 关联；
- 慢消费者和有界队列；
- stdout 无非协议污染；
- 断线重连不重复 Turn。

## 8. Coding Eval 数据集

### 8.1 任务类别

首版维护可复现的小型真实仓库集：

- 定位并修复单元测试缺陷；
- 跨文件行为修复；
- 增加小功能并补测试；
- 类型/静态检查修复；
- 重构但行为保持；
- 文档与代码同步；
- 脏工作区保护；
- 无法安全完成时正确停止。

每个任务固定：

- 起始 Git Commit；
- 用户 Prompt；
- 允许的工具、网络和预算；
- 隐藏测试；
- 预期行为与禁止修改；
- 评分器版本。

### 8.2 不使用单一 Golden Patch

正确实现可能有多个 Diff。评分按：

1. 隐藏测试/行为；
2. 未破坏基线测试；
3. 禁止文件未修改；
4. Diff 范围和代码质量；
5. 最终回答与真实状态一致；
6. 安全与预算约束。

## 9. 指标

### 9.1 正确性

- task success rate；
- test pass rate；
- regression rate；
- invalid/forbidden edit rate；
- final-answer factual consistency。

### 9.2 Runtime

- terminal-state correctness；
- replay determinism；
- duplicate effect count；
- orphan Tool Call/Result count；
- cancellation latency；
- recovery success/interrupt rate。

### 9.3 效率

- wall-clock time；
- model input/output/cache token；
- estimated cost；
- model steps；
- Tool calls 与重复读取；
- compaction count；
- approval count。

### 9.4 安全

- unauthorized effect count；
- secret leakage count；
- sandbox escape count；
- approval mismatch count；
- network policy violation count。

## 10. 0.3 质量门禁

Agent Runtime Kernel 合并前：

- 所有 Unit/Contract/Integration 测试通过；
- ScriptedProvider 核心场景覆盖正常、失败、取消和预算；
- Event Replay 的终态和 Snapshot 100% 一致；
- Crash Matrix 中不存在无法解释的 RUNNING；
- Tool Call/Result orphan 数为 0；
- duplicate effect count 为 0；
- CI 不需要任何真实模型 API Key；
- Schema、ADR、README 与实现同步。

真实 Provider Smoke Test 在 0.4 加入，使用显式环境开关，永不作为默认 CI 前提。

## 11. Eval 运行与报告

每次基线运行记录：

- Harnessix commit；
- Provider/Model 与配置摘要；
- Eval 数据集和评分器版本；
- Sandbox/Platform；
- 成功率、成本、时延和安全指标；
- 每个失败的分类，不保存明文 Secret。

报告比较同一数据集的前后版本，并将差异分成：

- Runtime regression；
- Provider variance；
- Prompt/Context regression；
- Tool/Sandbox regression；
- Eval infrastructure defect。

没有完成失败分类的单次成功率变化，不作为架构决策依据。

## 12. 非目标与后续

- 0.2 不追求大型公开 Benchmark 排名；
- 0.3 不调用真实模型验证 Runtime；
- 0.4 建立 Provider Smoke 与成本基线；
- 0.5 建立第一个真实 Coding Eval 集；
- 0.7 增加安全红队与隔离后端 Contract；
- 0.9 增加跨平台、长时间 Soak、性能和版本回归 Dashboard。

## 13. CI 低速 Runner 的超时测试边界（2026-09-03）

代码提交 `1c11449` 的 Python 3.12/3.13 与 PostgreSQL CI 均通过。后续纯文档提交在较慢的 Python 3.12 Runner 上暴露 Smoke 测试假设：测试给整个 Turn 0.4 秒，却断言必然已经产生一个 HTTP 请求；实际上预算可能在持久化/准备期间耗尽，正确结果是零请求。

测试改为在真实 SDK 使用的 MockTransport 响应流读完有效分片后，确定性抛出对应 HTTP 库的 ReadTimeout，使用正常 Turn 预算，严格验证 provider/transport 分类、一次请求、零重试、连接关闭和 Replay。没有放宽运行时预算，也不是简单增加 0.4 秒阈值。真实 deadline 行为继续由 Kernel 和双 Provider 的原有超时测试覆盖；测试总数仍为 819，默认 CI 不访问模型 API。

随后 Python 3.13 暴露同类的 Kernel 测试假设：给整个 Turn 0.1 秒，却要求已打开模型流。现已统一按执行阶段驱动超时测试：通过局部测试代理捕获真正的 `asyncio.Timeout`，等待模型流/用量持久化检查点后调用 `reschedule()` 推进期限，再验证真实取消与清理。没有替换全局时钟或生产实现。同步排查并修复尝试账本的 0.3 秒以及 SDK 用量收据的 1 秒前置速度假设；保留 Provider 原有真实总 deadline 测试。

另增加“进入 Provider 前预算已耗尽”的独立用例，明确零请求/未开流不需要关闭不存在的流。最终本地全量 **820 passed、1 skipped**，异步调试 **784 passed**；原 819 项阶段快照保持历史含义。

## 14. 0.5.1 只读编码工具验收（2026-09-03）

本片新增 **69 项**测试，全量 `make check` **889 passed、1 skipped**，Ruff/Mypy 通过；Agent/Model/Smoke/Tools 开启 `PYTHONASYNCIODEBUG=1`、`-W error` 共 **853 passed**。本地 PostgreSQL 未配置而跳过的测试保留，远端 PostgreSQL Job 独立执行。

- `tests/tools/test_files.py`：严格参数、真实目录/文件、UTF-8、控制字符/二进制、长行/扫描/字节限制、分页漂移、路径拒绝和错误脱敏；
- `test_workspace.py`：根/中间目录/目标替换、同 inode 修改、stat/open 竞争、链接与 FIFO、停止/期限、FD 释放；
- `test_runtime.py`：固定工具契约校验、输出模型、关闭、Token/Task 取消、重复取消及排队调用不启动；
- `test_kernel.py`：实际 SDK + HTTP 替身 → Kernel → 真实文件 → SQLite 重开/Replay，审批重开与根/策略变化失效，文件读取中的用户/Task 取消持久化，生成 Schema 校验；
- `test_recovery.py`：真实子进程分别在工具执行前、读取后/结果提交前、终态前退出，重开不重新调用工具或 Provider。

新增 3 个进程崩溃切点后全项目为 **57 个**，另有 2 个 SIGINT 用例。`uv build`、独立基础 wheel 和无供应商 SDK 的 `examples.kernel_files` 入口通过；仅验证只读能力，不属于 0.5.5 自主编码 Eval。Linux 完整测试与新增 macOS 只读 CI 的最终结果应查看对应提交，默认 CI 不使用真实模型凭据。
