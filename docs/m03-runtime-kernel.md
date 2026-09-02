# 0.3 Agent Runtime Kernel 实施设计

- 更新日期：2026-09-03
- 当前交付：0.3.1 核心 + 0.3.2 持久审批
- 状态：两个切片已实现；0.3 整体尚未完成
- 依赖：[ADR 0006](adr/0006-thread-turn-item-event-model.md) 至 [ADR 0012](adr/0012-durable-approval-checkpoint.md)

## 1. 范围

0.3.1/0.3.2 不接真实模型，不执行 Shell、不修改 Workspace，也不开放 MCP 或项目 Hook。它提供正式的进程内 Kernel，用离线 Provider 先验证持久化和生命周期。

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
- Agent Event v2、新旧事件混合 Replay 和 Session v1→v2 升级；
- Thread/Turn 到既有 ActionContext 的关联映射；
- 独立 Schema 和离线验收脚本。

未实现：

- Plan/Compaction 等完整 Item 生命周期；
- Provider 原始 Chunk、Tool Arguments Delta 和完整 Usage 归一化；
- Provider 自动重试、缓存/推理 Token 明细和成本预算；
- 自主重跑中断 Turn、Fork、Archive（审批检查点外不可 resume 原 Turn）；
- 真正的 Coding Tool、Process、Sandbox 或外部 Action 路由；
- App Server 和 CLI/TUI 的 Agent 命令；
- Context 压缩、分页投影、Artifact 存储和完整 Secret Redactor；
- 完整 Agent OTel Spans/Metrics；当前只提供持久 TraceContext 与关联 ID。

这些能力仍按路线图进入 0.3.3 及后续版本，不能从当前 Kernel 的测试结果推导为已经具备。

## 2. 模块

~~~text
harnessix.agent.models        领域契约和生命周期
harnessix.agent.reducer       纯函数事件投影与不变量
harnessix.agent.runtime       进程内 Loop、取消、恢复
harnessix.agent.cancellation  可协作取消的异步 I/O
harnessix.agent.approvals     审批指纹、查询与持久截止时间
harnessix.agent.ports         ToolRuntime 端口
harnessix.models.contracts    ModelProvider 端口和归一化事件子集
harnessix.models.scripted     Fake / Scripted Provider
harnessix.session.ports       SessionStore 端口
harnessix.session.sqlite      SQLite 事务、迁移与宿主锁
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

Provider 自动重试尚未启用。失败类别保留，原始异常文本不默认持久化。

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
- 支持空数据库初始化到 v2、幂等初始化和真实 0.3.1 Transcript 的 v1→v2 升级；历史事件不重写；
- 原 v1 Schema 文件冻结，新写事件默认 v2；旧程序遇到 Migration 2 会拒绝启动，不支持原地降级。

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

## 10. 下一切片：0.3.3 契约补齐与整体验收

按以下顺序实施，不提前接入真实写工具：

1. **Item/错误契约**：依据 ADR 0006 增加 plan、context_compaction、error 的语义模型和生命周期，区分“支持记录”与“已能自动规划/压缩”；统一 Provider、Kernel、Tool 的错误分类和可重试声明。
2. **可观测性**：复用现有 Observability/NoOp/OTel 端口；覆盖 Turn 执行片段、Model Step、Tool、审批命令和恢复；持久暂停不得悬挂跨进程 Span。指标使用低基数标签，不包含 prompt、arguments、审批 reason 或凭据。
3. **存储契约**：抽取 SessionStore 共享契约套件，保留 SQLite 特有迁移/锁/事务注入测试；补足 event_id 冲突、缺口、投影损坏、不可写存储和取消提交边界。
4. **统一门禁**：新旧 Schema、Replay、失败矩阵、取消清理、离线示例、构建产物与文档一致性通过后，才能标记 0.3 完成并进入 0.4 Model Runtime。

边界：0.3.3 不实现 Context Engine 的自动压缩算法（0.6），也不以添加 Item 类型宣称已有自动规划或真实编码能力。0.4 的真实模型验证需要单独受控 Smoke Test，不能用当前离线测试代替。

当前风险/待办：

- 聚合快照和启动扫描随历史增长，分页与体积基准留待 0.6；
- 本地 Python Tool 仍是可信代码，硬进程取消与 OS 隔离尚未实现；
- 审批墙钟预算包含人工等待，暂未拆分独立审批 TTL；
- Kernel 到 Action Plane 的正式写执行路由尚未实现；
- 完整 Agent OTel、共享 Store 契约和剩余 Item 尚未通过整体验收。
