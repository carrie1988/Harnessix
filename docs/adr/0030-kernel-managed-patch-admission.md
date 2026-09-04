# ADR 0030：Kernel 写准入、持久审批与双账本恢复

- 日期：2026-09-04
- 状态：0.5.3b2b 详细设计已固化；下述 Kernel 接入、Agent v6 与 migration 7 **尚未实现**
- 代码核对基线：`8832dd7`，CI `33836437879` 四项通过

## 1. 本次源码核对结论

| 已有代码 | 现行约束 | 集成必须解决的问题 |
| --- | --- | --- |
| `agent/runtime.py::_drive/_execute_tool` | 只广告、执行 READ_ONLY | 只能增加显式 Patch 端口，不能让通用注册表任意写入 |
| `agent/models.py::ApprovalRequestContent` | kind 与 kernel-read-only/v1 固定 | 新增独立写审批，不给旧审批隐式增加授权 |
| `agent/reducer.py::_start_item` | 只读审批；成功结果限 EXECUTING_TOOLS | 取消后已完成替换需要单独的、有证据的恢复结算规则 |
| `agent/runtime.py::_finish` | 所有未配对非只读 Call 都记 unknown | 先查询可信 Patch 事实；不能把已确认成功/未应用统一抹为 unknown |
| `agent/execution.py` | for_pending_call 只接受活跃 EXECUTING_TOOLS | 等待答复、取消、进程恢复不是执行授权，不能靠放宽现有工厂解决 |
| `session/sqlite.py` | 投影最高 v5，新写为 v5；migration 到 6 | 新事件/投影与最低 reader 版本必须一起升级 |
| `models/_history.py::messages_for` | ToolResult 只传 outcome/output/error | 私有写证据须保持在白名单之外，并做两个 SDK 的实际 wire 检查 |
| `patches/agent_bridge.py` | 宿主级绑定、取消排空、历史核对 | Kernel 才负责活跃状态、持久审批、原始时限与消费恢复边界 |

本次核对还发现一个现有恢复遗漏：仅提供 ApprovalRecord 而未提供 plan 时，如果账本缺失，原桥接返回 failed。这个分支忽略了先前审批证据，不能证明未发生效果。本次先增加失败复现，再修复为 unknown；不借此宣称 Kernel 集成完成。

## 2. 专用端口与生命周期

新增显式 `patches` 构造参数，默认 None。端口复用 ManagedPatchBridge 的 definition/prepare/review/execute/recover，不新增替换器、Worker、HTTP 或副本所有权协议。现有 tools/scoped_tools 的互斥及 Artifact 绑定规则保持原样。

准入时校验唯一工具名 apply_patch、NON_IDEMPOTENT_WRITE、强制审批、支持核对及契约指纹；与只读工具重名直接拒绝。只有专用端口的定义可以突破广告过滤；从旧 ToolRuntime 注册同名或其他写定义都不可执行。模型只能传 PatchProposal，不传 scope、plan_id、actor、approved 或 ApprovalRecord。

宿主按 `受管副本 → 只读工具/桥接 → AgentRuntime` 顺序进入，逆序关闭。读取工具与 Patch 必须使用同一个规范副本根；不把用户源目录当作模型写工作区。Kernel 关闭先取消并等待活动 Turn；桥接关闭排空，最后由宿主释放副本锁和 FD。

已有 ToolExecutionScope 的执行工厂和摘要格式不变。等待审批与恢复使用独立的**核对上下文**，校验当前 Thread/Turn/未配对 Call 后建立；核对上下文本身不得被解释为执行授权。新执行入口只有在 Session 已消费 WAITING_APPROVAL、决定已持久化、调用仍活跃且未过期时才生成调用许可。

## 3. 持久契约：计划态与效果态分开

拟新增 `PatchApprovalRequestContent`：

~~~text
kind = "patch_approval_request"
policy_version = "kernel-managed-patch/v1"
approval_id, call_id
plan: ManagedPatchCallPlan
request_fingerprint = plan.approval_fingerprint
decision: ApprovalRecord | None
~~~

新 kind/策略和计划整体校验共同构成准入规则。计划中的 Thread/Turn/Call 必须与事件归属完全一致，call_fingerprint 必须匹配原调用，manifest.proposal_sha256 必须匹配严格提案。不能只比较表面的 call_id 或一个传入哈希。旧 ApprovalRequestContent 与 kernel-read-only/v1 不改义。

拟给 ToolResultContent 增加可选私有证据 `patch`：计划引用/调用绑定、后端状态以及 `origin = execution | recovery`。最终字段在实现时按最小可回放事实定稿；不能保存完整 before/after，不以重复整份账本替代最小事实。它不是模型输出，也不是签名或文件系统授权能力。

Reducer 必须验证：证据绑定当前未配对 Call、状态与 outcome 一致、成功对应已持久完成且批准的新写审批。未开始的孤立计划可以结算未成功，不凭空补一份已批准请求。普通只读成功仍仅允许 EXECUTING_TOOLS；只有类型化的 Patch 恢复成功可以在取消/恢复状态记录，且绝不能因此把 Turn 改成 completed。

## 4. 正常时序与答复幂等

~~~text
模型产生提案
  → Session 持久 Call
  → Kernel 进入 EXECUTING_TOOLS，校验专用定义和活跃调用
  → bridge.prepare：找到原计划，或首次准备并持久保存
  → Session 原子写入 PatchApprovalRequest + WAITING_APPROVAL
  → 宿主 reply_approval：核对归属/指纹/时限，批准前复核前镜像
  → Session 仅保存决定，不调用后端 reply/execute
  → resume：Session 先持久离开 WAITING_APPROVAL
  → bridge.execute：镜像决定 → started → 文件效果 → 后端结果
  → Session 持久 ToolResult → 后续模型读取/回答
~~~

重复答复只有 fingerprint/outcome/actor/reason 全部相同才幂等；冲突拒绝。重复答复不再次复核或执行已经决定的计划。拒绝不要求旧前镜像仍存在，但仍核对当前调用、原计划和拒绝对象，不将指纹失配当拒绝成功。

审批等待、离线和重启不重置 Turn 的原截止时间。复核可能花费时间，因此决定的 occurred_at 和 decided_at 必须在复核之后生成，再由同一个 Reducer 校验截止时间；不能仅凭复核开始前“尚未过期”就提交迟到的批准。

副本先保存完整计划、Session 后发布审批，两者不是一个事务。稳定 request_id 已由 b2a 绑定调用；任何恢复都只找回该请求对应的不可变计划，不能改 proposal 或创建新的 plan_id 偷换待批准对象。

## 5. 恢复决策表

恢复先检查持久 Session 状态和当前受信工具配置，再只调用 bridge.recover。配置缺失/契约漂移/不可读不得退回通用工具或重建计划。

| Session / 副本事实 | 工具结果 | Turn / 后续动作 |
| --- | --- | --- |
| 未过期 WAITING_APPROVAL，尚未答复或已答复 | 暂不产生结果 | 保留等待，仅允许显式答复/resume |
| Call 已保存；无计划、无审批证据 | 已知未成功 | 中断恢复，不继续模型、不 prepare |
| pending/approved，尚未 started | 已知未应用 | 中断恢复，不复用该调用重新执行 |
| rejected/failed/observed_before | 已知未成功 | 结算历史事实；若要再试必须新调用/计划/批准 |
| applied，或核对得到可归因 observed_after；Session 有匹配批准 | succeeded + recovery 证据 | 进程重启仍 interrupted；取消仍可 cancelled，不暗示回滚 |
| 后镜像相同但 inode 归因不足 | unknown | interrupted，禁止继续模型 |
| diverged/missing/unavailable/uncertain | unknown | interrupted，等待宿主检查 |
| 账本缺失，但有 plan 或 approval 任一证据 | unknown | 不能把存储丢失解释为没有效果 |
| 后端成功，但没有匹配的 Session 批准 | unknown | 不用宿主历史写记录伪造当前 Call 已获授权 |
| 调用、计划、定义或工作区不匹配 | unknown 或结构化入口拒绝 | Kernel 结算时保持不确定，不能回退重试 |

applied 是历史记录，不代表当前文件永远没有被外部修改。read_file 的新 revision 才对应新的内容观察。未知结果必须停止当前模型循环，不把“请模型再试一次”当作恢复策略。

## 6. 取消与结果发布窗口

Kernel 先持久 CANCELLING，再触发 token；桥接负责线程排空。取消请求到达时文件可能尚未替换，也可能已经替换，不能单凭 asyncio 异常分类。

- 替换前停止：加载账本，确认未消费/未应用后记录失败或取消事实。
- 替换后停止：先等待文件与账本收尾；若有充分证据，保留工具成功，Turn 的取消不表示文件回滚。
- 线程已返回但 Session 尚未发布：走同样的只读核对路径，不再次 execute。
- 重复 Task.cancel、超时、Runtime.__aexit__：都不得在写线程仍运行时释放副本/Session 所有权。

新证据会增加 ToolResult 序列化长度。预算很小时，不能把“文件已写，但结果太大”转换成未应用。优先保留最小私有事实、限制公开 output；如果预算规则无法容纳证据，终止 Turn 并明确结果发布失败，保留后端事实供核对，不截断授权/归因字段或重新写入。

## 7. 版本与升级门禁

拟新增 Agent Event/Thread v6 与 `0007_managed_patch.sql` 最低 reader 标记。旧 v1–v5 Schema、旧 migration 校验和不改动，副本账本 schema v1 和 b2a 计划/输出 Schema 不因 Kernel 集成被覆写。

新代码继续读取旧事件；旧事件导出必须删除新字段的默认空值，保持原 JSON 形状，不能把 patch 证据或新审批标成 v5。旧快照保持原始存储字节，直到新事件追加或显式 rebuild 才升级投影；缺失新字段只补兼容默认值。

旧 wheel 必须在看到 migration 7 时明确拒绝打开，而不是误解新写审批。升级验证包括旧 wheel 创建真实 WAITING_APPROVAL → 新 wheel 重开旧只读审批并完成 → Replay 一致 → 旧 wheel 再开升级库拒绝。升级备份使用 SQLite backup 或停机一致备份，不能只拷贝仍有 WAL 的主文件。

## 8. 必须实现的验收矩阵

| 编号 | 测试范围 | 必须证明 |
| --- | --- | --- |
| KWP-01 | 默认/错误注册/定义重名 | 只有显式 Patch 端口可广告和调度；其他写工具仍拒绝 |
| KWP-02 | 请求/计划/策略/副本/调用错绑 | 错误批准不写文件；旧只读审批不授予写权限 |
| KWP-03 | 严格参数、旧 revision、配额、准备失败 | 结构化失败，不重建旧计划；源文件保持不变 |
| KWP-04 | 答复重复、冲突、拒绝、迟到答复 | 答复不执行；重复不重复产生后端效果 |
| KWP-05 | 真 SDK + 离线 HTTP 读→提案→批准重开→写→读回→回答 | 不是仅 Mock Tool；模型 wire 不含 plan_id/审批指纹/完整私有证据 |
| KWP-06 | token、Task、重复取消、deadline、关闭 | 写线程已排空；工具效果与 Turn 状态分别诚实结算 |
| KWP-07 | 事件 Reducer、Replay、篡改证据、极小输出预算 | 不放宽只读规则，不以结果发布失败隐去已发生效果 |
| KWP-08 | 旧 wheel 与新 wheel 的真实数据库升级 | 旧等待只读审批可继续；旧 reader 拒绝新库；旧 JSON 字节不变 |
| KWP-09 | Session × 副本真实进程退出 | 无 Provider 重放、无重复 execute、目标 inode/时间戳不被恢复修改 |
| KWP-10 | Linux 3.12/3.13、macOS、独立基础 wheel | 默认测试不需要真实模型 API；包外示例通过 |

KWP-09 至少包括：Call 提交后、计划保存后/Session 请求前、审批请求提交后、决定前/后、等待边界消费后、后端决定后、started、临时 inode 证据后、replace 前/后、后端结果后、Session 结果前/后、Turn 终态前。旧 b1/b2a 的文件后端/宿主夹具崩溃测试不能抵扣这一组合矩阵。

## 9. 实施顺序和当前完成声明

1. 新契约、Reducer 约束、Schema 冻结与向前迁移；先保留默认 Kernel 只读。
2. 专用端口与审批时序，接通单文件 SDK 离线闭环。
3. Kernel 取消/恢复结算、组合崩溃、旧 wheel 升级和输出预算门禁。
4. 全量回归、包外验证、中文文档、提交推送和跨平台 CI。

本次只固化以上详细设计并修复桥接的缺证据误判；**Agent 仍为 v5、migration 仍到 6，模型尚不可调用写工具**。上述四步全部验收后才关闭 b2b/0.5.3b。多文件、Process、源目录合入和自主 Coding Eval 仍按后续路线图交付。
