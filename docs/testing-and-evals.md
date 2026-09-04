# Harnessix Code 测试与 Eval 规范 v1

- 状态：0.2 架构基线，随 0.4 实现持续更新
- 更新日期：2026-09-03

实施进展（2026-09-03）：0.3 范围本地验收完成。tests/agent 覆盖语义 Item、持久审批、统一错误、SQLite 事务、取消、混合版本 Replay、真实 v1/v2→v3 升级和 OTel 内存导出；进程矩阵包含 7 个核心、10 个审批、9 个语义 Item 边界。tests/contracts/session.py 提供 SessionStore 共享契约；真实模型有效性和真实编码 Evals 仍在后续阶段；详情见 [Kernel 实施设计](m03-runtime-kernel.md)。

0.4.2b1 收口快照（2026-09-03）：尝试账本领域/Kernel、累计用量去重、未知值与完整性、身份绑定、失败/取消结算、预算与 OTel 差额、v1/v2/v3→v4 混合升级及历史 Schema 冻结。当时新增 15 个模型尝试子进程崩溃切点，共 41 个。

0.4.2b2 收口快照（2026-09-03）：两个真实 SDK 的尝试/缓存/推理/失败用量映射、HTTP 前持久意图、累计值与迟到分项、取消和合法观测保留。新增 8 个 SDK 子进程切点，全项目合计 49 个；每次恢复均验证不重发请求。当时 `make check` 为 546 passed、1 skipped；异步调试下 Kernel + Provider 为 510 passed，详见 [ADR 0017](adr/0017-provider-attempt-usage.md)。

0.4.3a 收口快照（2026-09-03）：新增 96 项价格/成本测试，覆盖严格十进制字符串、整数定点精度、未知与显式零、计费上下文/模式/TTL/生效期/输入阶梯、失败尝试与重试去重、跨币种、旧步骤、JSON 重算与内容错绑、双 SDK → Kernel/SQLite → 报告 Replay。`make check` 为 **642 passed、1 skipped**（本地 PostgreSQL 未配置），异步调试回归 **606 passed**；新增两个独立 Schema，不改变历史 Agent/Provider Schema。真实价格、计费上下文自动采集与平台验证未验收，见 [ADR 0018](adr/0018-versioned-token-cost.md)。

0.4.3b1 收口快照（2026-09-03）：`tests/smoke/` 新增 **94 项**，包括两个实际 SDK × 三场景、私有临时 Session、Kernel 重开/审批/Replay、默认门禁、配置/预算、错误/不重试、超时/Task 取消、CLI 参数/正文 canary 和真实 SIGINT 子进程。全量 **736 passed、1 skipped**；异步调试下 Kernel + Provider + Smoke **700 passed**。注入传输验收不代表真实平台通过；报告白名单不承诺对不可信语义内容做通用 DLP。详情见 [ADR 0019](adr/0019-controlled-model-smoke.md)。

0.4.3b2 收口快照（2026-09-03）：新增 **69 项**，覆盖原生响应计费元数据、迟到/去重/漂移、严格 TTL 分项、原子提交、直接平台映射与价格绑定冲突、真实 v4 升级；全量 **805 passed、1 skipped**，异步调试 **769 passed**。新增 5 个硬崩溃切点，全项目 **54 个**，另有 2 个 SIGINT 用例。旧 Schema 冻结、旧读者拒绝、独立 wheel 与六个离线入口通过。设计见 [ADR 0020](adr/0020-observed-billing-context.md)。

真实验证与默认 CI 分开：百炼北京首次工具解析失败，定位确认空 ID 增量兼容问题；修复后文本/内存工具/审批重开均有真实通过证据。兼容修复新增 6 项回归（先复现 2 failed，再全部通过），随后并发初始化暴露 WAL 忙锁并完成根因修复，另补 8 项存储回归（见 [ADR 0021](adr/0021-session-wal-initialization.md)）；该次全量 **819 passed、1 skipped**，异步调试 **783 passed**；不将固定场景通过等同于全模型兼容或 Coding Eval，见 [验证记录](validation/bailian-2026-09-03.md)。

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

本片新增 **70 项**测试，全量 `make check` **890 passed、1 skipped**，Ruff/Mypy 通过；Agent/Model/Smoke/Tools 开启 `PYTHONASYNCIODEBUG=1`、`-W error` 共 **854 passed**。本地 PostgreSQL 未配置而跳过的测试保留，远端 PostgreSQL Job 独立执行。

- `tests/tools/test_files.py`：严格参数、真实目录/文件、UTF-8、控制字符/二进制、长行/扫描/字节限制、分页漂移、路径拒绝和错误脱敏；
- `test_workspace.py`：根/中间目录/目标替换、同 inode 修改、stat/open 竞争、链接与 FIFO、停止/期限、FD 释放；
- `test_runtime.py`：固定工具契约校验、输出模型、关闭、Token/Task 取消、重复取消及排队调用不启动；额外覆盖关闭等待中重复取消也必须回收根 FD（修复前明确失败）；
- `test_kernel.py`：实际 SDK + HTTP 替身 → Kernel → 真实文件 → SQLite 重开/Replay，审批重开与根/策略变化失效，文件读取中的用户/Task 取消持久化，生成 Schema 校验；
- `test_recovery.py`：真实子进程分别在工具执行前、读取后/结果提交前、终态前退出，重开不重新调用工具或 Provider。

新增 3 个进程崩溃切点后全项目为 **57 个**，另有 2 个 SIGINT 用例。`uv build`、独立基础 wheel 和无供应商 SDK 的 `examples.kernel_files` 入口通过；仅验证只读能力，不属于 0.5.5 自主编码 Eval。Linux 完整测试与新增 macOS 只读 CI 的最终结果应查看对应提交，默认 CI 不使用真实模型凭据。

初始实现 `0a0f68f` 的 [CI](https://github.com/carrie1988/Harnessix/actions/runs/33742500047) 已通过 Linux Python 3.12/3.13、macOS 只读套件与 PostgreSQL。之后的关闭取消硬化增加 1 项回归，已重新完成上述本地全量、异步调试与独立 wheel 验收；其远端结果以最新提交 CI 为准。

## 15. 0.5.2a 有界搜索验收（2026-09-03）

基线 `993720b` 的 [CI](https://github.com/carrie1988/Harnessix/actions/runs/33742942261) 已通过后，再增量实施搜索。0.5.2a 交付 glob/字面量 grep，**不包含 Artifact、写入、Shell、自主编码 Eval 或新的真实模型调用**。

本片新增 **80 项**测试，只读工具套件累计 **150 项**。本地 `make check`：Ruff/Mypy 通过、**970 passed、1 skipped**（未配置本地 PostgreSQL）；`PYTHONASYNCIODEBUG=1 uv run pytest -W error tests/agent tests/models tests/smoke tests/tools`：**934 passed**。基线阶段的旧测试数字保留历史含义。

- `test_search.py`：路径段通配/globstar、大小写/点文件、严格参数、排序与数量截断、grep→revision 读取；负向验证完整性、截断原因、排序、去重和空截断输出契约；
- `test_search_boundaries.py`：链接/FIFO/拒绝路径、性能忽略与权限区分、字面量而非正则、CRLF/未终止 CR 原文、非法 UTF-8/二进制/超大文件/长行缺口、Unicode 片段边界、枚举/深度/名称/累计读取硬预算、输出字节截断、对象替换/读取中变更、I/O 脱敏、取消等待 FD 释放与期限；
- `test_search_kernel.py`：真实 OpenAI SDK + MockTransport → glob → grep → revision 读取 → 回答；四个 HTTP 替身请求全部关闭，SQLite 重开/Replay 一致；新旧四工具的审批重开、搜索规则变化与版本隔离；四份新 v1 Schema 的内容/哈希冻结；
- `test_recovery.py`：复用只读故障夹具，为 glob/grep 增加执行前、效果完成/结果提交前、终态前共 **6 个真实进程崩溃切点**，恢复不重搜、不请求模型；全项目硬崩溃切点累计 **63 个**，原 2 个 SIGINT 用例保留。

独立安装/兼容证据：

1. `uv build` 成功；新建基础依赖环境安装 wheel，以 `python -I` 从仓库外运行 `examples/kernel_search.py`，无 OpenAI/Anthropic SDK 也可完成固定搜索闭环和 Replay。
2. 用上一片独立安装的 `993720b` wheel 创建真实 list_files/read_file 待审批会话；用新 wheel、同一工作区和 SQLite 重开，旧工具完整定义相同，两项旧审批均可批准、执行、完成和 Replay。不是仅比较同一新进程的哈希。
3. 在 Harnessix 自身 `src`（不是临时示例仓库）执行独立 wheel 只读探测：glob 找到 **80 个 Python 文件**；grep `ModelAttempt` 得到 **40 个命中行**，读取 80 文件、393287 字节，两次 scan_complete=true。该次 macOS 观测约 0.024/0.067 秒，仅为离线可用性证据，不是性能 SLA 或完整 Coding Eval。

新搜索示例已加入 Linux Python 3.12/3.13 与 macOS CI；macOS 工具回归和 PostgreSQL 独立 Job 保留。推送后的具体远端结果以对应提交 CI 为准，不用基线成功替代新提交验收。

## 16. 0.5.2b1 可信执行作用域验收（2026-09-03）

基线 `9a70ce4` 的 [四项 CI](https://github.com/carrie1988/Harnessix/actions/runs/33748311220) 通过后继续实施。本片新增 **46 项**测试：24 项 Kernel 作用域测试、10 项 Coding Scoped 测试和 12 项既有集成/恢复路径扩展；工具套件累计 **172 项**。

- 本地 `make check`：Ruff/Mypy 通过，**1016 passed、1 skipped**（本地未配置 PostgreSQL）。
- `PYTHONASYNCIODEBUG=1 uv run pytest -W error tests/agent tests/models tests/smoke tests/tools`：**980 passed**。
- `test_execution_scope.py`：入口互斥、不自动发现新签名、持久归属而非模型参数、不可变性、完整调用摘要漂移、活跃/未完成调用工厂、TypeError 不降级/不重试、审批等待/拒绝/重开、未知/写工具/版本门禁、跨 Thread 并发/连续 Turn/多 Call 隔离。
- `test_scoped_runtime.py`：四工具旧入口待审批会话切换 Scoped 入口继续，定义完全相同；规范根/别名/参数/摘要不匹配时不进行目标 I/O；Coding 参数不能注入 Thread 归属。
- 实际 SDK 离线 glob→grep→revision 读取同时测试旧/新入口，作用域摘要不出现在 HTTP 请求中；实际文件读取消同样覆盖两种入口。
- 真实子进程故障夹具新增 Scoped read/glob/grep 的 9 个切点，总计 **72 个硬崩溃切点**，原 2 个 SIGINT 用例保留；中断恢复不调用 Provider 或重新执行工具。

独立兼容验收从 `git archive 9a70ce4` 构建旧 wheel，在无 OpenAI/Anthropic SDK 的基础环境中创建四个真实待审批会话，再安装新 wheel，以 Scoped 入口重开同一 SQLite/规范工作区：工具完整定义一致，四项旧审批均可批准、执行、完成并 Replay。该流程实际跨版本安装，不是仅在同一新版本内计算两个相等摘要。随后以 `python -I` 从仓库外运行新 Scoped 搜索和旧只读两个示例均通过。

未修改旧输入/输出、Agent/Action Schema 或 Session Migration，也未新增依赖、真实 API 请求或远程中间件。现有 Linux 全量、macOS 工具/示例及 PostgreSQL CI 继续运行，新提交结果以对应 CI 为准。

**0.5.2b1 本片未交付**：Artifact 内容/manifest、引用发布、配额、过期和孤儿回收。后续交付见第 17 节。作用域不是发布租约；终态后的历史 scope 不能单独证明仍有发布权限。0.5.2b2 必须补齐持久事务及故障验证，才能关闭 0.5.2b/0.5.2。

## 17. 0.5.2b2 事务 Artifact 验收（2026-09-03）

基线 `0c39c39` 的 [四项 CI](https://github.com/carrie1988/Harnessix/actions/runs/33755778266) 通过后实施。本片交付有界 JSONL Artifact，不包括 Patch、进程日志或真实编码 Eval；未调用真实模型 API、使用凭据或部署中间件。

- `tests/artifacts/` 新增 **102 项**：严格契约/六份冻结 Schema、300 条中文搜索预览外读取、完整性与缺口、跨归属/策略/根身份、配置错绑、配额及并发竞争、过期/损坏/清理游标与活跃引用保护、审批漂移/拒绝/重开、取消与故障恢复。
- 本地 `make check`：Ruff/Mypy（85 个源文件）及 **1118 passed、1 skipped**；本地未配置 PostgreSQL，真实 PostgreSQL 由现有 CI 服务验证。
- 异步调试 `PYTHONASYNCIODEBUG=1 uv run pytest -W error tests/agent tests/models tests/smoke tests/tools tests/artifacts`：**1082 passed**。
- 真实 OpenAI SDK + MockTransport 三次离线请求完成 grep→read_artifact→回答；原始 300 条正文不回灌 HTTP，宿主归属字段不进入模型参数/请求，流均关闭。
- 新增 glob/grep × 7 个真实 `os._exit(77)` 切点：捕获后、Artifact 插入后、Session 事件后/投影后、提交前/后、Turn 终态前。全项目累计 **86 个硬崩溃切点**及原有 **2 个 SIGINT 用例**；不把进程退出当作所有断电/磁盘故障模拟。
- 提交前硬崩溃、异常或 Task 取消：正文/引用同时缺席；提交后失败：正文/引用同时保留。重开均 INTERRUPTED、不重新调用工具或 Provider、SQLite 完整性检查及 Replay 通过。用户取消与发布在 Thread 锁上线性化，允许先原子提交再取消。
- 注入 SQLITE_FULL 映射为 storage_full 并回滚；清理事务失败可重试，过期墓碑不变成缺失。该项是驱动错误注入，不宣称真实灌满磁盘验证。

**独立安装与升级**：从 `git archive 0c39c39` 构建旧 wheel，仓库外无 OpenAI/Anthropic SDK 的基础环境创建四个真实待审批会话（migration 5）。新 wheel 升级到 migration 6，以 Scoped 入口批准并继续四工具；旧工具完整定义和历史事件原始字节不变，Replay 一致。旧 wheel 再次打开升级库明确报 schema_too_new。新 wheel 以 `python -I` 在仓库外运行 files/search/artifacts 三个离线示例通过。

**兼容与 CI**：仅新增 migration 6，事件/投影仍为 Agent v5；未改写旧 migration 或八份默认工具 Schema。Linux Python 3.12/3.13 全量 CI 和 macOS 工具 CI 已纳入新 Artifact 测试与示例，PostgreSQL 作业沿用。CI 结果以本片对应提交为准。

范围内 0.5.2b/0.5.2 已完成。当前上限为单件 1 MiB/10000 条 JSONL，不是任意 blob 服务；逻辑内容/manifest 配额不限制整个 Session/WAL 物理大小，保留的墓碑最终需要宿主按保留策略轮换 Session。具体组合与生命周期见 [0.5 设计](m05-coding-tools.md#15-052b2-当前交付与使用)、[ADR 0026](adr/0026-transactional-artifacts.md)。

## 18. 0.5.3a 只读 Patch 准备验收（2026-09-03）

在 `4054e1d` 的 [四项 CI](https://github.com/carrie1988/Harnessix/actions/runs/33760516486) 全绿后开始。专项核对冻结 Codex/OpenCode 的 Patch 入口、实际文件写操作和失败证据；明确用户态内容复核不等于跨进程 CAS，见 [专项研究](research/patch-runtime.md) 和 [ADR 0027](adr/0027-prepared-patch-and-write-admission.md)。

- `tests/patches/` 新增 **69 项**：精确/非唯一/重叠锚点、同一前镜像坐标、顺序绑定、无实际变化、严格参数与字节/编辑数限制、完整 SHA、JSON manifest、私有载荷篡改、根/文件/拒绝策略/权限漂移、链接/特殊文件、完整尾部编码检查及两份冻结 Schema。
- 完整前后镜像各最多 1 MiB；边界测试包含正好上限、超限哨兵、读取期间增长及后镜像超限，不把预览前缀作为完整内容。
- UTF-8 中文、组合字符不归一化、BOM、CRLF、混合换行与无末尾换行均测试；未涉及字节保持不变。关闭并重开同一 Workspace 后仍可复核计划。
- 读取期间文件/根替换、I/O 异常和协作取消均验证 FD 回收；宿主线程被停止后等待其退出，不把“取消 asyncio 等待”当作停止底层文件 I/O。超时不会误报为计划损坏。
- 本地 `make check`：Ruff/Mypy（88 个源文件）通过，**1187 passed、1 skipped**；本地无 PostgreSQL，沿用 CI 实库作业。
- `PYTHONASYNCIODEBUG=1 uv run pytest -W error tests/agent tests/models tests/smoke tests/tools tests/artifacts tests/patches`：**1151 passed**。
- 从新 wheel 建立仓库外基础环境，无 OpenAI/Anthropic SDK，以 `python -I` 运行 `examples/patch_plan.py` 和既有 Artifact 示例通过；默认 CI 同时运行 Linux Python 3.12/3.13 和 macOS Patch 测试/示例。

本片没有修改 Kernel、默认工具定义、Action/Agent Schema 或 Session migration 6，没有新增依赖、真实模型请求或服务器操作。原 **86 个硬崩溃切点、2 个 SIGINT** 保持，不把本片只读测试虚报为新的写崩溃恢复测试。

**0.5.3a 当时未交付（后续见第 19 节）**：模型可调用的 apply_patch、持久计划/写意图、写审批、文件提交、单文件写恢复及多文件效果。0.5.3a 的 manifest/私有字节不是授权凭据；verify_prepared 不会把计划变成已批准或已提交的状态。下一片 0.5.3b 的独占工作副本、持久意图、计划审批和效果核对通过后才开放写入，0.5.3 整体仍未完成。

## 19. 0.5.3b1 受管单文件 Patch 执行验收（2026-09-04）

在 `b0622cb` 的 [四项 CI](https://github.com/carrie1988/Harnessix/actions/runs/33762318938) 全绿和远程基线同步后实施。进一步核对固定的 kernel-read-only/v1 审批契约，将 b 拆为宿主执行后端 b1 与 Kernel 模型接入 b2，见 [ADR 0028](adr/0028-managed-patch-execution.md) 和 [下一片实施顺序](m05-coding-tools.md#下一片-053b2-的实施顺序)。

- 本片新增 **87 项**测试，Patch 套件累计 **156 项**；新增两份独立 v1 Schema，旧 Schema 字节不变。
- 本地 `make check`：Ruff/Mypy（92 个源文件）通过，**1274 passed、1 skipped**；本地无 PostgreSQL，真实 PostgreSQL 作业保留在 CI。
- `PYTHONASYNCIODEBUG=1 uv run pytest -W error tests/agent tests/models tests/smoke tests/tools tests/artifacts tests/patches`：**1238 passed**。
- 完整宿主链路：真实文件导入→副本读取/计划→保存→批准→写入→重开/核对；副本内容与预期一致，源文件始终不变，重复执行拒绝。
- 覆盖拒绝/错绑/幂等冲突、计划和导入预算、非登记路径、特殊文件/链接/编码、根/锁/数据库替换、前镜像/目录/权限漂移、损坏私有载荷/来源基线/事件/版本、查询只读库和结果写库失败。
- 替换前各阶段取消进入 failed 并消费审批；替换后取消先完成效果与 applied 记账。两个线程只能消费一次审批，close 必须等待活动写结束；不是 Kernel/asyncio Task 写取消验收。
- 验证短写循环、零写、注入 ENOSPC/EIO、实际 fsync 调用抛错和临时文件清理；错误不携带原始路径/SQL。属于故障注入，未灌满真实磁盘或进行断电测试。
- 原生元数据检查覆盖可见扩展属性；Darwin 额外建立实际扩展 ACL 验证拒绝，不通过忽略属性接口错误兼容平台。允许系统 provenance 标记的窄策略有明确文档，不声称通用元数据保留。
- `test_managed_crash.py`：根级/嵌套目标各 9 个真实 os._exit 切点（started、临时创建、临时刷盘、临时证据、替换前/后、目录刷盘、结果前/后），另有 building 导入的 2 个切点，共 **20 个**。重开不重写；恢复前后 inode/mtime/ctime 不变，源文件不变，building 拒绝执行。全项目累计 **106 个硬崩溃场景及 2 个 SIGINT 用例**。
- 后镜像相同但临时 inode 不符仍 uncertain，不能仅凭字节归因；前镜像、第三种内容、缺失和不可读分别观察。observed_before 后只有新请求/新计划/新审批才可再次尝试。

**独立交付**：新建仓库外基础 wheel 环境，确认没有 OpenAI/Anthropic SDK，以 `python -I` 运行 managed_patch、patch_plan 和 kernel_artifacts 三个示例通过。未新增依赖、真实 API 请求或服务器操作。默认 CI 已增加 Linux Python 3.12/3.13 和 macOS 的 managed_patch 示例及新测试，PostgreSQL 服务作业保留；远端结果以本片对应提交为准。

**仍未完成**：模型可调用的写工具、Agent 写审批/结果兼容、Session 与副本账本组合恢复、源目录 Diff 合入、多文件部分效果、Process 与自主编码 Eval。Kernel/Agent v5/Action v1/Session migration 6 和默认工具清单未修改。b1 的宿主审批不是 kernel-read-only/v1 的写授权，不能据此勾选整体 0.5.3b/0.5 完成。

## 20. 0.5.3b2a 调用绑定桥接验收（2026-09-04）

在 `20b28d2` 与远端同步、[四项 CI](https://github.com/carrie1988/Harnessix/actions/runs/33779154455) 全绿后，按 [ADR 0029](adr/0029-managed-patch-agent-bridge.md) 交付宿主桥接，不提前放开模型写工具。

- 新增 **95 项**测试，Patch 套件累计 **251 项**。本地 `make check`：Ruff/Mypy（94 个源文件）通过，**1369 passed、1 skipped**；跳过项为本地未配置 PostgreSQL，沿用 CI 实库作业。
- `PYTHONASYNCIODEBUG=1 uv run pytest -W error tests/agent tests/models tests/smoke tests/tools tests/artifacts tests/patches`：**1333 passed**。Patch 单独的异步调试模式 **251 passed**。
- 新增 ManagedPatchCallPlan、ManagedPatchOutput 两份冻结 v1 Schema；运行 Schema 生成器后，全部旧 Schema 字节不变。未修改 Agent v5、Action v1、Session migration 6、副本账本 schema v1 或既有默认工具契约。
- 调用/计划归属：Thread/Turn/Call、工作区、工具版本/指纹、提案、后端及桥接指纹的错绑/篡改均覆盖；模型注入授权字段拒绝；相同请求找回原计划，不再 prepare；已有请求的不同提案不能借壳使用。
- 宿主批准/拒绝、只读和后端指纹混用、无效 actor/reason、持久审批冲突、旧 revision、重复/并发执行、两个桥接共享副本等路径覆盖；最多一次应用，源文件不变。拒绝不要求旧前镜像仍存在。
- 协作取消、Task.cancel、外层 timeout、重复取消分别覆盖替换前/后；后台写收尾后才完成取消。关闭（含重复取消关闭）等待活动线程并拒绝排队调用，但不关闭宿主副本。批准镜像后、写意图前取消保留 approved，恢复报告未成功，不自动写入。
- 恢复覆盖无计划/孤立计划、已知计划丢失、损坏计划/索引、根身份失效、缺少批准/批准不匹配；后镜像、第三种内容、缺失、不可读、相同字节不同 inode 分别归类。不把执行抛错当作没有效果，不把缺证据当作成功。
- `test_bridge_crash.py` 新增 **12 个真实 os._exit 场景**：计划保存后、后端答复后、9 个既有执行切点以及桥接返回后。以宿主文件夹具保存调用归属，重开找回原计划并核对；恢复禁用 prepare/save/reply/execute，目标 inode/mtime/ctime 和源文件保持不变。累计 **118 个硬崩溃场景及 2 个 SIGINT 用例**。这是桥接崩溃证据，不是尚未实现的 Session 写审批组合恢复或断电测试。
- Kernel 集成边界反向验证：即使宿主误把该写定义放入旧通用注册表，模型请求仍不广告它，Kernel 仍返回 tool_not_enabled，不执行桥接。没有将通用 NON_IDEMPOTENT_WRITE 放行。

**独立 wheel**：在仓库外新建基础环境，安装当前 wheel 与锁定的默认依赖（无 OpenAI/Anthropic SDK），以 `python -I` 运行 patch_bridge、managed_patch、kernel_artifacts 通过。新示例串联真实只读工具→精确提案→原计划找回→宿主批准→副本写入→读回→重开不重写。Linux Python 3.12/3.13 与 macOS CI 均增加该示例；远端验收结果以对应提交为准。

**下一片 b2b**：版本化 Agent 写审批/恢复结果、最低 reader 迁移、专用 Kernel 准入、真实 SDK 离线 HTTP 闭环及 Session × 副本账本崩溃矩阵。本片 ApprovalRecord 是受信宿主声明，未核验活跃 Turn、预算或 Session 审批消费；宿主夹具不等于自主编码 Eval。无新增依赖、真实模型请求、服务器登录或中间件部署，不关闭整体 0.5.3b/0.5。

## 21. b2b 设计审查与桥接恢复修正（2026-09-04）

基于 `8832dd7` 的 [四项 CI](https://github.com/carrie1988/Harnessix/actions/runs/33836437879) 全绿结果，完成 [ADR 0030](adr/0030-kernel-managed-patch-admission.md)：逐项核对 Runtime、Reducer、作用域、Session 投影/迁移和模型结果白名单，明确拟定 v6 写审批、专用准入、持久答复/消费顺序、取消后的效果结算及 KWP-01～10 组合验收矩阵。上述 Kernel 接入仍待实现，未写入新 Schema 或 migration。

设计审查发现：恢复只带 ApprovalRecord、未带 plan，而账本计划缺失时，旧桥接忽略了审批证据并返回 failed。增加无证据/批准/拒绝 **3 项回归**，修改前批准和拒绝两项稳定失败；修改后只要 plan 或 approval 任一证据存在就返回 unknown，避免把缺证据解释为未发生效果。恢复仍不 prepare/save/reply/execute，也不新增重试权限。

- `make check`：Ruff/Mypy（94 个源文件）通过，**1372 passed、1 skipped**；本地 PostgreSQL 跳过项仍由 CI 实库作业验证。
- 异步调试全范围：`PYTHONASYNCIODEBUG=1 uv run pytest -W error tests/agent tests/models tests/smoke tests/tools tests/artifacts tests/patches`，**1336 passed**。
- Patch 测试累计 **254 项**；真实硬崩溃仍为 **118 个场景及 2 个 SIGINT**，不把这次普通回归计为新增崩溃场景。
- 仓库外建立基础 wheel 环境，使用锁定的默认依赖，不安装 OpenAI/Anthropic SDK，`python -I patch_bridge.py` 通过。
- 全部旧公开 Schema、Agent v5、Session migration 6、副本账本 schema v1、默认 Kernel 工具及依赖保持不变。无真实模型请求或远程服务器操作。

本次交付是 b2b 的设计基线和一处现有桥接修正，不标记 b2b/0.5.3b 完成。下一步按 ADR 0030 开始契约/Reducer/最低 reader 迁移，再接通专用端口与 SDK 离线闭环。

## 22. 0.5.3b2b Kernel 受管写闭环验收（2026-09-04）

基于 `45b2b10` 的 [四项 CI](https://github.com/carrie1988/Harnessix/actions/runs/33838299601) 全绿和远端同步结果，实际实现 [ADR 0030](adr/0030-kernel-managed-patch-admission.md)，不再停留在设计或宿主桥接夹具。

- 本片新增 **56 项**测试：Kernel Patch 套件 **55 项**，旧 wheel 导出的 v5 transcript 升级 **1 项**；Patch 套件累计 **309 项**。
- `make check`：Ruff/Mypy（95 个源文件）通过，**1428 passed、1 skipped**；本地未配置 PostgreSQL，该跳过项继续由 CI 的真实 PostgreSQL 服务验证。
- `PYTHONASYNCIODEBUG=1 uv run pytest -W error tests/agent tests/models tests/smoke tests/tools tests/artifacts tests/patches`：**1392 passed**。
- 专用端口拒绝错误名称/效果/审批/幂等/核对属性和定义重名；旧通用注册表写门禁回归保留。错误工作区不降级执行，严格参数拒绝模型注入批准标志。
- Session 持久写审批暂停、重开、同答复幂等、冲突拒绝、陈旧来源、拒绝不复核旧来源、复核中预算过期均覆盖；答复不调用后端决定或修改文件。Reducer 验证归属/审批/效果，旧事件标签拒绝新字段。
- 真实 OpenAI SDK 与 Anthropic SDK 各以 MockTransport 完成四次离线模型 HTTP 交互：读文件→提案→持久审批重开→写入→读回→回答。目标副本确实改变、源目录保持不变，所有流关闭，两个模型 wire 均不含计划/副本 ID、审批摘要或私有 patch 证据。
- 替换前/后分别覆盖 token、Task.cancel、重复取消和 deadline；另验证 Runtime 关闭在写线程/审批复核阻塞时保持 Session 所有权，重复取消关闭也必须等排空。替换后的工具成功与 Turn cancelled/failed 分别结算，不把取消描述为文件回滚。
- 文件写完但 Kernel 回调失败或公开结果超限：不再执行，核对并保留私有成功事实，Turn 失败而非假完成。250 字符预算时核对后的公开 output 被舍弃，归因字段完整；1 字符预算在模型提案阶段停止，尚未准备/审批。
- `test_kernel_patch_crash.py` 新增 **23 个真实 os._exit 场景**：20 个 Session × Patch 组合切点（Call、计划、请求、决定、消费、后端批准、9 个文件执行窗口、工具返回、Session 结果和终态前），另有缺失端口/定义变化/第三种内容三个重启场景。恢复禁用 Provider/prepare/save/reply/execute，已知效果诚实结算，不充分证据为 unknown；重复打开幂等，恢复前后 inode/mtime/ctime 与源文件不变。全项目累计 **141 个硬崩溃场景及 2 个 SIGINT 用例**，不声称模拟所有断电/硬盘故障。

**版本/升级**：只新增 Agent Event/Thread v6、migration 7；旧 v1–v5 Schema、旧 migration 校验和、Action/Provider/工具/桥接 Schema 和副本账本 v1 不变。无 patch 的旧结果序列化不增加 null 字段。使用 `git archive 45b2b10` 在隔离源码目录构建真正旧 wheel，旧基础环境创建真实 WAITING_APPROVAL；新基础 wheel 重开、答复并完成旧只读审批，旧事件原始字节不变、Replay 一致；旧 wheel 再开新库明确报 schema_too_new。旧 wheel 完成的 v5 transcript 冻结到 `tests/agent/fixtures/session-v5.json`，持续覆盖 v1–v5 升级；包外探针和步骤见 [部署文档](deployment.md#当前-session-v6--migration-7-升级053b2b)。

**包外交付**：仓库外基础 wheel 环境确认未安装 OpenAI/Anthropic SDK，以 `python -I` 运行 kernel_files、kernel_search、kernel_artifacts、patch_plan、managed_patch、patch_bridge、kernel_patch 共七个示例通过。新 Kernel 示例是真实文件/数据库与离线决策，不是自主编码 Eval。Linux Python 3.12/3.13 全量和 macOS Patch CI 均增加该入口；PostgreSQL 作业保留，远端结果以本片对应提交为准。

未新增依赖、真实模型请求、服务器登录或中间件。b2b/0.5.3b 的受管单文件范围完成；多文件部分效果、结构化 Diff、Process、源目录合入、Agent CLI 与自主 Coding Eval 仍未交付，整个 0.5.3/0.5 不标记完成。

## 23. 0.5.3c1 只读整组计划与结构化 Diff 验收（2026-09-04）

基于 `3f42130` 的 [四项 CI](https://github.com/carrie1988/Harnessix/actions/runs/33840907282) 全绿与远程同步结果，按 [ADR 0031](adr/0031-patch-batches-and-structured-diff.md) 实施 c1，不把准备/展示视作已完成多文件写入。

- 新增 **79 项**：整组准备24项、Diff 51项、冻结 Schema 4项；Patch 套件累计 **388 项**。
- `make check`：Ruff/Mypy（99个源文件）通过，**1507 passed、1 skipped**；本地 PostgreSQL 跳过，CI 实库作业保留。
- 异步调试全范围 `PYTHONASYNCIODEBUG=1 uv run pytest -W error tests/agent tests/models tests/smoke tests/tools tests/artifacts tests/patches`：**1471 passed**。
- 真实多文件只读准备/重开复核：路径唯一和顺序绑定、严格参数/授权字段注入、不同工作区、提案重排、成员/镜像/manifest 篡改均拒绝。准备晚文件时更改早文件，最终整组复核拒绝；缺失/链接/来源漂移不修改前面的文件。共享操作取消/截止时间不因下一个文件重置。
- 预算边界使用真实内容：提案 UTF-8 合计恰好512 KiB及多1字节；4个各1 MiB文件的完整前后镜像恰好8 MiB，再加一个文件即拒绝。原单文件完整读取、长行、编码、链接/FD、取消、审批和写恢复测试保持通过。
- BOM、中文、多字节 emoji、组合字符、CRLF/混合前缀/无末尾换行均覆盖；反向提案顺序、长度变化和删除片段的前/后字节坐标配合前镜像可重建目标，不按字符索引替代字节索引。
- Diff 预览0/1/2/3/4/1024/4096字节不切断 UTF-8 码点，完整长度/SHA与截断独立校验。JSON 引号、反斜杠、换行/制表符转义计入总量，恰好预算可返回、少1字节则截断前缀；256字节可只保留摘要/总量和 truncated。16文件×32编辑的512项报告在1 MiB内完整返回，默认64 KiB明确返回前缀。
- Diff 只校验计划内部事实；来源随后改变时仍可以展示原计划，但 verify 拒绝陈旧来源。测试禁止 Diff 重新 open 文件，避免将展示误写成实时工作区或已提交结果。
- 仅提取既有精确区间解析供准备器/Diff共用；新增四份独立 v1 Schema，生成后全部旧 Schema 字节不变。Agent v6、Session migration 7、副本账本 v1、既有 apply_patch 定义与依赖均未修改。

**独立基础 wheel**：仓库外新环境安装锁定默认依赖，确认无 OpenAI/Anthropic SDK，以 `python -I` 运行 files/search/artifacts/patch_plan/managed_patch/patch_bridge/kernel_patch/patch_batch 共八个入口通过。新示例只准备和展示两个真实文件，校验磁盘字节不变，没有批准/执行整组写入或发布 Artifact。Linux Python 3.12/3.13 与 macOS CI 均增加新示例；远端结果以本片对应提交为准。

本片未新增真实模型请求、服务器操作、数据库迁移或硬崩溃场景；全项目仍为 **141 个真实硬崩溃场景及2个 SIGINT用例**，不将只读计划回归计为多文件写崩溃验收。c1 范围完成，c2 的持久组预留/批准/部分效果及 c3 的 Kernel/模型/Artifact 仍待开发；整体0.5.3c/0.5尚未完成。

## 24. 0.5.3c2a 整组预留、持久审批及迁移验收（2026-09-04）

基于 `09cb6d6` 及 [四项通过的 CI](https://github.com/carrie1988/Harnessix/actions/runs/33842477262) 实施 [ADR 0032](adr/0032-durable-batch-reservation-and-approval.md)。c2a 是 c2 的预留/审批切片，不包含组文件写入。

- 新增 **99 项**：整组宿主与边界81项，真实崩溃/迁移16项（其中11个真实退出），冻结 Schema 2项；Patch 套件累计 **487 项**。
- `make check`：格式/Ruff/Mypy（103源文件）通过，**1606 passed、1 skipped**；跳过项为本地无 PostgreSQL，远程实库 CI 保留。
- `PYTHONASYNCIODEBUG=1 uv run pytest -W error tests/agent tests/models tests/smoke tests/tools tests/artifacts tests/patches`：**1570 passed**。
- 真实三个文件按顺序整组保存/批准/拒绝/重开，源与副本字节、inode、mtime、ctime 不变；相同请求/载荷幂等、内容/顺序冲突、组/成员指纹错绑、无效字段/决定、来源漂移、未登记路径、错误副本与关闭句柄均覆盖。
- 待审批/批准/拒绝 × 三个成员位置：旧单文件 save/reply/execute 全部拒绝拆分消费；额外覆盖清空 owner 列后的旧接口拒绝。组成员始终 pending，批准不隐式产生单文件批准事件。四线程八次同请求保存只产生一组，竞争批准/拒绝只有一个持久决定。
- 与旧单文件双向共享计划数量和镜像配额，容量检查在写事务中完成；组元数据 UTF-8 实际预算恰好可用、少1字节拒绝。决定预留覆盖最长 actor/reason 的 JSON 转义，其他组不能挤占已预留决定空间；超长持久载荷拒绝。
- 组行、三个成员插入位置及提交前的存储/取消/超时共15种异常均完整回滚；决定提交前/后丢失确认共6种异常通过只读 lookup 判断是否已提交，不凭异常推断没有持久事实。所有公开入口共用操作预算，未知/缺失/损坏记录不默默新建或修复。
- **11 个真实 os._exit 场景**：预留组行/三个成员/提交前后共6个，决定提交前后2个，迁移版本标记前/提交前/提交后3个。未提交时无半组成员或决定，已提交时全量可见；迁移中断只有完整 v1 或完整 v2，旧 metadata/baseline/镜像/事件字节及目标文件状态不变，数据库 inode 不变。另有5项旧账本损坏/未来版本/DDL冲突拒绝，失败不先推进版本。

**真实旧 wheel 验收**：从 `git archive 09cb6d6` 单独构建旧 wheel，不从当前源码伪造旧版本；仓库外旧基础环境实际创建 pending/approved/applied 三类 v1 计划。新基础 wheel 升级至 v2，旧事件/镜像原字节、三类状态、副本文件字节/inode/mtime/ctime、源目录与数据库 inode 全部保留；旧 wheel 再次打开明确返回 patch_wrong_database，新 wheel 随后再次重开仍一致。可复现探针与步骤见 [部署说明](deployment.md#副本账本-v2-升级053c2a)。单元测试中的 v1 表形夹具仅用于故障注入，不替代上述旧包证据。

**基础发行包**：独立环境安装锁定默认依赖，未安装 OpenAI/Anthropic SDK；`python -I` 运行 kernel_files、kernel_search、kernel_artifacts、patch_plan、managed_patch、patch_bridge、kernel_patch、patch_batch、managed_batch_approval 共九个入口通过。新示例仅预留、审批、重开和验证旧接口拒绝；Linux Python 3.12/3.13 与 macOS CI 均增加该入口。新增两份独立 Schema，全部旧 Schema 字节不变；Agent v6、Session migration7、Provider v3、依赖与单文件工具定义不变。副本账本独立升级为 v2。

全项目累计 **152 个真实硬崩溃场景及2个 SIGINT 用例**。新增11个场景只证明组持久事务/迁移，不冒充多文件写效果恢复。本片没有真实模型请求、服务器操作或中间件部署。c2a 范围完成，下一片 c2b 实现顺序一次性消费、部分/未知效果和每成员写前后崩溃核对；c2/c3/0.5 均未标记完成。远端跨平台结果以本片提交 CI 为准。

## 25. 0.5.3c2b 顺序执行与部分效果恢复验收（2026-09-04）

在 `f0adddc` 及 [四项全绿 CI](https://github.com/carrie1988/Harnessix/actions/runs/33860637921) 基础上实现 [ADR 0033](adr/0033-batch-consumption-and-effect-recovery.md)。新增独立组运行/效果契约、真实顺序消费和只核对恢复，副本账本升级 v3；不改变模型工具入口。

- 新增 **182项**：执行/故障边界131项、崩溃/迁移49项（44个真实退出场景及5项旧数据拒绝）、冻结 Schema 2项。Patch 套件累计 **669项**。
- `make check`：Ruff/Mypy（107源文件）通过，**1788 passed、1 skipped**；本地缺 PostgreSQL，仅该项跳过，远程实库 CI 保留。
- 异步调试全范围 `PYTHONASYNCIODEBUG=1 uv run pytest -W error tests/agent tests/models tests/smoke tests/tools tests/artifacts tests/patches`：**1752 passed**。
- 三文件成功闭环和最大16成员严格有序执行、重复执行拒绝、未批准/拒绝/错指纹、取消/超时前置检查、组消费/结果事务失败均覆盖。源目录始终不变，审批完成本身不写文件，升级也不消费旧批准。
- 三个成员位置 × 九个单文件切点 × 存储/取消/超时共81项：检查实际成功前缀、失败或未知的当前成员、pending 后缀。取消发生在最后文件替换之后时可以 all-applied + cancelled/timeout，不能伪装未写入；恢复保留既有终止原因。
- 全部成员位置的整组来源/元数据漂移均在首次文件修改前拒绝，批准仍消费。两个后续成员位置在前一成员完成后发生来源变化时停止，当前成员可保持 approved/未启动意图，不伪装执行失败。
- 单独注入替换前/后 fsync 调用失败6项、成员意图/应用/不确定结果记账失败6项；记账失败导致成员仍 started 时返回 unknown，恢复只观察归因，不调度后续文件。另验证相同后镜像但不同 inode 的三个成员位置始终 unknown，以及 missing/diverged/unavailable 不谎称未发生效果。
- 查询和恢复校验组运行事件、完整审批绑定、成员决定与顺序；覆盖校验和/指纹/副本错绑、开始事件缺失、成员越序、审批被替换、虚假效果摘要。恢复中途取消/超时保留已落库观察，之后仍只核对，不解锁批准。

**44个真实进程退出场景**：38个组/文件执行窗口（组提交前后、整组复核后、每成员批准/完成、每成员九个替换/结果切点、组终态提交前后）；3个观察/终态提交中再退出场景；3个 v2→v3 迁移版本标记/提交切点。核对阶段禁止调用执行/保存/批准入口；恢复前后目标文件 inode、mtime、ctime 与源目录一致。全部文件已应用但组终态未提交时，恢复为 applied + interrupted，不自动补跑或改称正常完成。另有5项损坏旧组/成员/外键/组ID/DDL冲突拒绝，失败保持v2。

**真实旧包与两级升级**：从 `git archive f0adddc` 构建实际旧 v2 wheel，在隔离基础环境创建 pending/approved/rejected 三类组。新 wheel 升 v3，旧 metadata/baseline/plans/events/batches/batch_approvals 原字节、文件时间/inode、源目录及数据库 inode 保留，所有运行记录仍不存在；旧 v2 reader 明确拒绝 v3。随后只在新环境显式执行原 approved 组并只核对，旧 reader 再次拒绝。另用 `09cb6d6` 的真实 v1 wheel 创建单文件 pending/approved/applied，验证 v1→v2→v3 和旧 reader 拒绝。步骤见 [部署说明](deployment.md#副本账本-v3-升级053c2b)，不以修改版本标记的单元夹具代替旧 wheel 证据。

**复用与基础发行包**：归一化 AST 审查确认原单文件 execute/reconcile 核心与 `f0adddc` 一致，仅提取内部方法并维持公开组成员拒绝。旧 Schema、原 v1→v2 迁移实现、Agent v6/Session migration7/Provider v3、模型工具定义和依赖不变；新增 run/result 两份 Schema。基础 wheel 无 OpenAI/Anthropic SDK，仓库外 `python -I` 运行 files/search/artifacts/patch_plan/managed_patch/patch_bridge/kernel_patch/patch_batch/managed_batch_approval/managed_batch 共十个示例通过；Linux 3.12/3.13 和 macOS CI 增加新多文件示例。

全项目累计 **196个真实硬崩溃场景及2个 SIGINT 用例**，不宣称覆盖全部硬件断电。c2 范围完成，c3 的 Kernel 批量审批/结果、模型闭环与 Diff Artifact 尚未实现；当前效果报告是历史归因，不是实时文件完整性证明，也未新增实际效果 Diff 自动发布。本片无真实模型请求、SSH 或中间件部署。远端结果以本片提交 CI 为准。
