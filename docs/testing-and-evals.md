# Harnessix Code 测试与 Eval 规范 v1

- 状态：0.2 架构基线
- 日期：2026-09-02

实施进展（2026-09-03）：0.3 范围本地验收完成。tests/agent 覆盖语义 Item、持久审批、统一错误、SQLite 事务、取消、混合版本 Replay、真实 v1/v2→v3 升级和 OTel 内存导出；进程矩阵包含 7 个核心、10 个审批、9 个语义 Item 边界。tests/contracts/session.py 提供 SessionStore 共享契约；真实模型有效性和真实编码 Evals 仍在后续阶段；详情见 [Kernel 实施设计](m03-runtime-kernel.md)。

## 1. 目标

Harnessix Code 的测试必须回答两类不同问题：

1. **Runtime 是否正确**：状态、持久化、取消、权限和恢复是否满足契约；
2. **Agent 是否有效**：在真实仓库任务中能否以合理成本完成正确修改。

不能用几个成功 Demo 代替 Runtime 正确性，也不能只靠单元测试宣称 Coding Agent 有用。

## 2. 质量属性

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
