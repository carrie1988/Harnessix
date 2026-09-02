# 0.3 Agent Runtime Kernel 实施设计

- 更新日期：2026-09-03
- 当前交付：0.3.1 核心 + 0.3.2 持久审批 + 0.3.3 契约/可观测性/存储验收
- 状态：0.3 范围实现与本地验收完成；后续进入 0.4
- 依赖：[ADR 0006](adr/0006-thread-turn-item-event-model.md) 至 [ADR 0013](adr/0013-kernel-contracts-and-telemetry.md)

## 1. 范围

0.3 不接真实模型，不执行 Shell、不修改 Workspace，也不开放 MCP 或项目 Hook。它提供正式的进程内 Kernel，用离线 Provider 先验证持久化和生命周期。

已实现：

- Thread、Turn、Item、AgentEvent 与版本化 Schema；
- UserMessage、AssistantMessage、ToolCall、ToolResult；Reasoning Summary 数据类型保留，但本轮不驱动其流；
- 纯函数 Reducer，在线提交与离线 Replay 复用同一不变量；
- SQLite 事件日志与聚合快照的事务提交；
- 客户端 request_id 幂等、Event ID 去重、Thread sequence CAS；
- FakeProvider、ScriptedProvider 与多步模型/只读工具循环；
- 步数、报告 Token 用量、墙钟时间、模型/工具输出大小边界；
- 用户 Cancel、调用方 Task Cancel 与宿主关闭；
- 进程重启后的保守恢复，以及审批检查点的持久保留；
- ApprovalRequest、答复、取消、显式继续与工具契约指纹；
- Agent Event v3、新旧事件混合 Replay 和 Session v1/v2→v3 升级；
- Plan/Compaction/Error 持久语义 Item 与统一错误 category；
- Turn/Model/Tool/Approval/Cancel/Recovery Trace、低基数 Metrics 与可观测性故障隔离；
- SessionStore 共享契约与损坏/只读/磁盘满等存储故障测试；
- Thread/Turn 到既有 ActionContext 的关联映射；
- 独立 Schema 和离线验收脚本。

未实现：

- 自动规划、自动 Context Compaction、Compaction 对 Model View 的替换；
- Provider 原始 Chunk、Tool Arguments Delta 和完整 Usage 归一化；
- Provider 自动重试、缓存/推理 Token 明细和成本预算；
- 自主重跑中断 Turn、Fork、Archive（审批检查点外不可 resume 原 Turn）；
- 真正的 Coding Tool、Process、Sandbox 或外部 Action 路由；
- App Server 和 CLI/TUI 的 Agent 命令；
- Context 压缩、分页投影、Artifact 存储和完整 Secret Redactor；

这些能力仍按路线图进入 0.4 及后续版本，不能从当前 Kernel 的测试结果推导为已经具备。

## 2. 模块

~~~text
harnessix.agent.models        领域契约和生命周期
harnessix.agent.reducer       纯函数事件投影与不变量
harnessix.agent.runtime       进程内 Loop、取消、恢复
harnessix.agent.cancellation  可协作取消的异步 I/O
harnessix.agent.approvals     审批指纹、查询与持久截止时间
harnessix.agent.errors        统一失败类别、KernelError 与 AgentFailure
harnessix.agent.telemetry     安全遥测包装与运行片段诊断
harnessix.agent.ports         ToolRuntime 端口
harnessix.models.contracts    ModelProvider 端口和归一化事件子集
harnessix.models.scripted     Fake / Scripted Provider
harnessix.session.ports       SessionStore 端口
harnessix.session.sqlite      SQLite 事务、迁移与宿主锁
harnessix.session.errors      存储驱动错误归一化
~~~

复用现有 ContractModel、EffectClass、ToolDescriptor、TraceContext 和 ActionContext，不重定义第二套 Action Plane 契约。

## 3. 状态机

~~~text
ACCEPTED → PREPARING_CONTEXT → CALLING_MODEL
                                   ├→ EXECUTING_TOOLS → PREPARING_CONTEXT
                                   └→ FINALIZING → COMPLETED
EXECUTING_TOOLS → WAITING_APPROVAL → EXECUTING_TOOLS

非终态 → CANCELLING → CANCELLED | INTERRUPTED
非终态 → FAILED | INTERRUPTED
~~~

约束：

1. 同一 Thread 最多一个活跃 Turn；
2. 用户输入、TurnStarted 和 UserMessage 终值在同一个事务中接受；
3. Tool Call 先持久化，整个 Provider 响应通过验证后才执行；
4. Tool Result 提交后才能构造下一模型请求；
5. Result 不允许缺失引用、重复或乱序；本切片按 Call 顺序串行执行；
6. Turn 进入终态前所有 Item 必须结算，所有完整 Tool Call 必须有 Result；
7. CancelRequested 以 CANCELLING 状态事件表示，之后不调度新模型或工具；
8. UNKNOWN 结果不能被写成成功或已取消，Turn 转为 INTERRUPTED；
9. 终态不可改写；
10. 同一 Transcript 的重放保持原 ID/时间，得到完全一致的快照。

两次独立执行会生成不同 ID 和时间；“确定性 Replay”不是承诺重新调用模型会生成相同标识。

## 4. Provider 与工具端口

### Provider

ModelProvider.stream 接收供应商中立 ModelRequest，返回异步事件流：

~~~text
response_started
text_started → text_delta* → text_completed
tool_call_completed
response_completed | response_failed
~~~

当前契约接收已经归一化、参数已经组装完成的 Tool Call。0.4 的 Adapter 负责供应商原始 Chunk/参数 Delta 解析。

Runtime 校验：

- 响应开始/终止顺序；
- 文本块 ID 唯一和开始/终值配对；
- 文本终值与非空 Delta 缓冲一致；
- Tool Call ID 在单次响应内唯一；
- Stop Reason 与 Tool Call 是否存在一致；
- EOF 前必须有终态，终态后不得继续输出；
- 没有语义内容的响应不是成功；
- response_failed 可在 response_started 之前报告认证、传输等失败。

Kernel 不自动重试 Provider。0.4.1 的 OpenAI Adapter 只在首事件前执行有界重试，见 ADR 0014。失败 category 与 retryable 声明保留；retryable 只是诊断提示，不等于允许重放 Tool。非成功终态与 Error Item 原子提交，底层异常原文不默认持久化。

### ToolRuntime

Tool 元数据由宿主注册表提供，不取信于模型。只有 READ_ONLY Tool 才会向模型公开。requires_approval 的只读 Tool 在持久批准并显式继续后才能执行。

未知或写入 Tool 即使被模型调用，也只产生模型可见失败，不进入 Handler。需要审批的只读调用会暂停 Turn；拒绝审批产生 approval_rejected 结果，不执行 Handler。

本切片信任进程内 Tool 实现遵守其声明，并不提供 OS 隔离。不能把任意第三方函数标成 READ_ONLY 后当作受限执行；真正的能力隔离在 0.5/0.7 落地。

## 5. SQLite 存储

数据库与 Effect Journal 分离，使用独立 application_id，拒绝在其他应用数据库中初始化。

当前表：

| 表 | 内容 |
|---|---|
| agent_migrations | 版本和已应用 SQL 的 SHA-256 |
| agent_events | 完整 AgentEvent，唯一 event_id 和 Thread sequence |
| agent_threads | 聚合快照、最后 sequence、快照校验值、projection_version |

每次写入：

~~~text
BEGIN IMMEDIATE
  → 检查重复事件及载荷
  → 读取快照并校验 sequence
  → 纯 Reducer 校验每个事件
  → 追加 Event
  → 保存新快照
COMMIT
~~~

- WAL、foreign_keys、busy_timeout、synchronous=FULL；
- Session 数据库和宿主锁使用 0600 权限；新建的专用数据目录使用 0700；
- Event 和投影任一阶段失败，事务整体回滚；
- 同批次同 ID 同载荷重试不重复追加；
- 部分重复批次、载荷冲突或旧 sequence 被拒绝；
- 数据库 Schema 高于当前版本、Migration 校验变化时拒绝启动；
- 投影损坏可从 Event Log 重建；事件本身损坏则拒绝猜测；
- 支持空数据库初始化到 v3、幂等初始化和真实 0.3.1/0.3.2 Transcript 的 v1/v2→v3 升级；历史事件不重写；
- 原 v1/v2 Schema 文件冻结，新写事件默认 v3；旧程序遇到 Migration 3 会拒绝启动，不支持原地降级。

当前使用完整聚合快照，而不是最终的 Thread/Turn/Item 分页查询表。它保持事务和 Replay 语义，但读写成本随历史增长；长会话产品化前必须完成规范化投影、分页和体积基准，不能据此宣称支持无限长历史。

## 6. 宿主与恢复

使用 async with AgentRuntime 管理生命周期。单数据库的 Runtime 宿主使用本地 macOS/Linux advisory lock；OS 在进程退出时自动释放锁。

锁只用于防止两个合法宿主互相接管，不是对同用户恶意进程的安全隔离。数据库应位于本地文件系统，不能通过硬链接、NFS 或不同锁路径共享为多实例运行。

启动顺序：

~~~text
取得唯一宿主锁
  → 初始化/验证/迁移数据库
  → 扫描非终态 Turn
  → 未过期 WAITING_APPROVAL：保留请求或已持久决定
  → 过期 WAITING_APPROVAL：结算为 FAILED
  → 其他活跃状态：结算缺失 Item/Result 并追加 INTERRUPTED
  → 接受新请求或显式继续审批检查点
~~~

恢复不调用 Provider 或 ToolRuntime。已有结果保留；缺失的只读 Result 标记中断；可能涉及外部写的未决结果标记 UNKNOWN。审批检查点使用 resume_turn 继续；其他已中断状态应以新 request_id 提交新 Turn，而不是重新打开原终态。

若存储不可写，Runtime 不能伪造已经持久化的终态：错误向调用方传播，进程退出后通过已提交事实恢复。

## 7. 预算、取消与资源

Budget 当前包括：

- max_steps：默认 16；
- max_tokens：默认 100000；
- timeout_seconds：默认 120；
- max_output_chars：默认 65536；
- max_tool_calls_per_step：默认 32。

另有单模型步骤 10000 事件、128 文本块硬上限，防止无限空 Delta/空文本块绕过输出限制。

审批等待、离线和重启均计入原 Turn 的墙钟时间预算；暂停不挂后台计时任务，启动恢复或再次操作时检查过期。宿主正常退出保留暂停状态，取消接口可以直接结算暂停 Turn。详细边界见 ADR 0012。

Token 预算是 Provider 报告后的记账与下一步调度限制，不是预付费或精确费用硬限。真实请求侧输出配额、Tokenizer 和 Cost Accounting 在后续实现。

CancelToken 在模型读取和 Tool await 期间竞争取消信号，并在退出时取消、等待子任务。当前只支持可协作取消的可信 Python I/O；吞掉 CancelledError 的恶意进程内插件无法被 Python 强制杀死，不属于该端口支持范围。Process Runtime 将通过独立进程提供硬终止边界。

Delta 不写数据库，进程崩溃后不会伪造丢失 Delta。当前 ItemDelta 的序号范围是单个模型步骤，携带 model_step；公共连接事件序号由后续 App Server 分配。

## 8. 验证

~~~bash
make check
uv run pytest tests/agent
uv run python examples/kernel_replay.py
uv run python examples/kernel_approval.py
make spec
~~~

进程级故障注入使用独立子进程和 os._exit，覆盖：

1. Event 写入后、投影前；
2. 投影写入后、Commit 前；
3. Turn 与用户输入接受后；
4. Tool Call 提交后；
5. Tool 执行前；
6. Tool 返回后、Result 提交前；
7. Turn 终态提交前。

0.3.2 新增 10 个审批边界：请求事务 Event 后/投影后、请求提交后、决定事务 Event 后/投影后、决定提交后、决定消费后、执行前、执行后和终态前。

0.3.3 新增 Plan、Compaction、Error 各 3 个边界：Event 后、投影后、Commit 后；另外验证真实 SQLite query_only 和 SQLITE_FULL、事件缺口/坏 JSON/索引错配/孤儿投影、导出器接口异常等路径。

测试断言数据库事实、工具调用计数、缺失结果结算和宿主锁释放，而不只检查日志或最终文本。

UUIDv7 使用标准库实现 [RFC 9562 的 UUIDv7 布局](https://www.rfc-editor.org/rfc/rfc9562.html#section-5.7)，不依赖 Python 3.14；排序权威仍是 Thread sequence。

### 0.3.2 本地验收记录（2026-09-03）

- make check：130 passed，1 skipped（未配置 PostgreSQL 测试连接）；
- PYTHONASYNCIODEBUG=1、warnings 视为错误：94 项 Kernel 测试通过；
- 两个离线示例通过，真实模型调用为 0；
- sdist/wheel 构建通过；从解包后的 wheel 独立导入，验证 Migration 2、审批恢复与旧 v1 事件导出；
- 当前验证环境为本地 macOS / Python 3.12；远端 CI 结果单独以 GitHub Actions 为准，不把本地通过等同于跨平台验收。

## 9. 0.3.2 持久审批接口

~~~python
turn = await runtime.run_turn(thread_id, prompt, request_id="stable-id")
# turn.status == WAITING_APPROVAL 时，从 Item 读取 approval_id 和 request_fingerprint。
await runtime.reply_approval(
    thread_id,
    turn.turn_id,
    approval_id,
    fingerprint=request_fingerprint,
    decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="可信宿主用户"),
)
turn = await runtime.resume_turn(thread_id, turn.turn_id)
~~~

这是进程内调用示意，完整可运行入口为 examples/kernel_approval.py。reply 不执行工具；resume 尚无答复时原样返回，终态重复 resume 也不会重跑。

指纹、答复幂等、预算、拒绝语义、迁移和崩溃消费边界详见 [ADR 0012](adr/0012-durable-approval-checkpoint.md)。本接口不替代 0.8 App Server 的用户认证与双向审批协议。

## 10. 0.3.3 契约与可观测性

### 语义 Item

Plan/Compaction 只能在 PREPARING_CONTEXT 开始，内容在开始和终值之间不可改写。Plan 修订通过新 Item 引用最新完成的计划。Compaction 只引用已终结旧 Turn 的完成消息/工具 Item，并验证工具调用和结果成对引用。

这些是可信宿主提交的语义事实，不是自动规划器或压缩算法；Kernel 当前不以 Compaction 摘要替换模型 History。自动驱动与 Token 数量实测在 Context Engine 阶段落实。

Error Item 与 v3 非成功终态的 error 必须一致；恢复时不覆盖已有错误事实。存储损坏、不可写、磁盘满和忙状态统一返回公开 KernelError；无法提交终态时仍向调用方抛出存储失败。

### Trace 与指标

~~~python
async with AgentRuntime(store, provider, tools, observability=observer) as runtime:
    turn = await runtime.run_turn(thread_id, prompt, request_id="stable-id")
~~~

observer 由宿主构造并负责关闭，默认使用 NoOp。自定义 ObservabilitySpan 需提供 set_attribute 和 set_error；set_error 的输入为受控错误类别。

| 信号 | 范围 |
|---|---|
| harnessix.agent.turn | 每次 run/resume 的有限时长执行片段 |
| harnessix.agent.model | 单个模型步骤 |
| harnessix.agent.tool | Handler 执行与返回值校验；Result 持久提交由 Turn 负责 |
| harnessix.agent.approval / cancel / recovery | 控制命令与启动恢复 |
| harnessix.agent.operations | 操作次数，按 operation/outcome/category 分类 |
| harnessix.agent.operation.duration | 操作耗时，单位秒 |
| harnessix.agent.tokens.input / output | 完整响应报告的 Token 增量 |
| harnessix.agent.turns.finished | 本进程确认的新终态提交次数 |

Thread/Turn/Call ID 只进入 Trace；指标无用户、会话、参数或任意工具名标签。暂停的 Span 已结束，重启后的片段以持久 TraceContext 关联。幂等重试不重复统计终态。

Kernel 不将业务异常传入第三方 Span 的 exit，不发送 prompt、Workspace、工具内容、审批 actor/reason 或异常堆栈。可观测性接口异常只产生一次固定降级日志，不掩盖业务错误，也不重新执行工具。

这不是通用 Secret Redactor：第三方导出器自身的内部日志不由 Kernel 字段过滤器控制；真实 Provider/系统级日志脱敏仍须后续安全验收。指标是尽力而为的诊断数据，不能代替 Event Log 审计和计费。

离线 OTel 集成验收：

~~~bash
uv run --extra observability python -m examples.kernel_observability
~~~

该入口在真实 OTel 内存导出器中验证 7 个已结束 Span、一次工具调用、一个跨重启 Trace 和 5 类低基数指标，不连接 Collector 或模型 API。

### 0.3.3 本地验收记录（2026-09-03）

- make check：179 passed，1 skipped（未配置 PostgreSQL 测试连接）；
- 异步调试模式、warnings 视为错误：143 项 Kernel 测试通过；
- 26 个真实子进程强制退出场景，覆盖核心、审批和语义 Item 提交边界；
- v1/v2 真正旧版本生成的 Transcript 可升级、混合重放与原格式导出；旧 JSON 不改写；
- OTel 内存导出验证关联、取消、错误分类与内容隔离；
- 三个离线示例通过；真实模型请求为 0；
- sdist/wheel 构建通过；解包后的独立 Wheel 验证 Migration 3、OTel 可选依赖隔离、旧事件导出/Replay 和真实内存 OTel 集成；
- 当前本地验证为 macOS / Python 3.12；GitHub Actions 独立验证 Linux/Python 3.12、3.13 与 PostgreSQL，状态以对应提交的运行记录为准。

## 11. 0.3 收口与下一阶段

0.3 的 Kernel 验收已完成，下一阶段进入 [0.4 Model Runtime](m04-model-runtime.md)，先求证官方接口、SDK 与秘密配置方式，再开发 Provider Adapter 和共享契约。

持续边界：

- 聚合快照和启动扫描随历史增长，分页与体积基准在 0.6；
- Python Tool 仍是可信代码；硬进程取消与 OS 隔离尚未实现；
- 审批墙钟预算包含人工等待，暂未拆分独立审批 TTL；
- 正式写执行路由、Sandbox 和端到端凭据脱敏尚未完成；
- 本里程碑完成不代表已具备真实代码修改或生产发布资格。
