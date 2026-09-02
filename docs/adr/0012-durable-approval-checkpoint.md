# ADR 0012：持久审批检查点与显式继续

- 状态：已接受并实现
- 日期：2026-09-03
- 范围：0.3.2 Kernel 切片
- 前置：ADR 0007、0010、0011

## 背景与目标

0.3.1 只允许无审批的可信只读工具。需要先验证暂停、答复和进程恢复的语义，才能在后续引入真实 Coding Tool、外部效果和双向客户端。

验收不以“出现一次审批提示”为准，而以数据库事实为准：请求先持久化，答复不可改写，未批准不得执行，恢复不得重复执行。

## 决策

### 1. 等待是一种持久状态，不是悬挂的协程

- `run_turn` 执行到首个需要审批的只读调用，原子追加 ApprovalRequest Item 与 `WAITING_APPROVAL`，随后返回 Turn；
- `reply_approval` 只持久记录决定，不执行工具、不唤醒后台任务；
- `resume_turn` 在同一 Turn、同一模型步骤和同一 Tool Call 上显式继续；
- 尚未答复时继续，返回原等待状态，不重复询问 Provider；
- 一次只审批顺序执行中的当前调用；同一步有多个调用时逐个处理；
- 没有未决审批时，原来的直接执行路径保持不变。

进程内接口面向可信宿主。`actor` 是审计字段，不是身份认证。客户端认证、连接恢复和审批 UI 属于 0.8。

### 2. 请求内容不可变，决定只提交一次

ApprovalRequest 是 Item，开始时没有决定，完成时附加现有 Action Plane 的 `ApprovalRecord`，复用 `ApprovalDecision` / `ApprovalOutcome`。

指纹绑定：

- Thread ID、Turn ID、Runtime 生成的 Call ID；
- Workspace 绝对路径；
- 工具名称、版本、副作用类型、审批要求；
- 整个 ToolDescriptor 的规范化 SHA-256，包含输入 Schema、风险等元数据；
- 完整调用参数；
- 固定只读策略版本 `kernel-read-only/v1`。

请求身份、调用引用、指纹和策略版本在 Item 完成时不可改写。完全相同的答复（结果、actor、reason）幂等返回当前 Turn；冲突答复拒绝，终态也不能改写已有决定。

审批不进入模型 History，避免将 actor/reason 等宿主控制面数据当作用户或工具消息。

### 3. 继续前重新检查工具契约

注册表在宿主构造时冻结。答复和继续时验证持久调用与当前注册表的一致性；版本、Schema、副作用类型、审批要求或工具是否存在发生变化，均不能沿用旧审批。

该指纹不绑定文件内容、Git revision、环境变量或 OS 能力，也不是签名凭证。当前仅适用于可信只读工具。0.7 必须扩展到规范化 cwd、环境摘要、Sandbox Profile 与正式 Policy，不能直接套用于写执行。

### 4. 持久消费边界，宁可中断，不盲目重放

| 崩溃时持久事实 | 重启处理 |
|---|---|
| 请求事务尚未提交 | 原执行步骤标记 INTERRUPTED |
| WAITING_APPROVAL，尚无决定 | 原样保留，允许答复 |
| WAITING_APPROVAL，决定已提交 | 原样保留，允许显式继续 |
| 已持久转回 EXECUTING_TOOLS | 标记 INTERRUPTED，不再次进入 Handler |
| Result 或 Turn 终态已提交 | 保留既有事实，不重写结果 |

`WAITING_APPROVAL → EXECUTING_TOOLS` 是消费检查点的持久边界，在 Handler 前提交。即使工具尚未真正开始，只要越过该边界后进程崩溃，也保守中断。这不是 exactly-once 执行承诺。

拒绝审批产生 `approval_rejected` Tool Result，不进入 Handler，模型可以继续解释或采用其他方法。批准后仍须满足原预算和工具契约。

### 5. 取消与时间预算

- 暂停 Turn 没有后台任务；取消直接持久结算审批、配对未完成 Call，并进入 CANCELLED；
- 执行中的 Turn 继续使用 CancelToken，取消后等待 I/O 子任务清理；
- Task 取消与宿主关闭只取消正在执行的任务；正常关闭宿主保留已暂停的审批；
- `created_at + timeout_seconds` 是原 Turn 的墙钟截止时间，审批等待、离线与重启均计入，不在每次 resume 时重新给预算；
- 暂停时不持有计时任务；过期在启动恢复、reply 或 resume 时检查。reply 拒绝过期决定，resume/启动将过期 Turn 结算为 FAILED；
- 若需要较长人工等待，调用方明确配置更长 Budget；本切片暂不拆分执行预算与人工审批 TTL；
- 时间策略依赖可信系统时钟；跨重启时钟回拨防护尚未实现。

### 6. Schema 与迁移

- 新写 Agent Event 使用 v2；读取同时支持 v1、v2；
- 新审批特性不允许伪装成 v1 事件；
- v1 JSON Schema 保留为历史契约，新增 v2 Schema，不覆盖旧文件；
- Session Migration 0002 增加 `projection_version`；旧快照默认 1，更新或重建后为 2；
- 0001 SQL 和历史事件 JSON 均不改写；
- 旧程序发现 Migration 2 后拒绝启动，禁止原地降级；
- Runtime 先获得唯一宿主锁，再初始化或迁移数据库，避免第二宿主先改表再发现锁冲突。

升级前停止旧宿主并备份数据库；回退需要恢复升级前备份，不能删 Migration 记录强行使用旧程序。无新运行时依赖，无额外中间件。

## 验证证据

- `tests/agent/test_approvals.py`：持久顺序、拒绝、多审批、重复/冲突答复、取消、并发、契约漂移、时间预算、Reducer 防绕过；
- `tests/agent/test_approval_crash_recovery.py`：10 个真实子进程强制退出边界；
- `tests/agent/test_session_upgrade.py`：真实 0.3.1 Transcript 升级、旧事件字节不变、未来投影拒绝、先持锁再迁移；
- `tests/agent/fixtures/session-v1.json`：由提交 `7c9443f0c6a3e50157ebdd7d31b8104bd2f26745` 的原始代码生成，不通过当前模型伪造旧快照；
- `examples/kernel_approval.py`：暂停、关闭、重启答复、显式继续与 Replay 一致性。

## 明确未交付

- 写工具、OS 隔离、审批凭证签名或远程多用户认证；
- Kernel 到 Action Journal 的正式执行路由；
- 任意中断点的自动恢复执行；
- 全部 0.3 功能：剩余 Item/错误契约、Agent OTel 和共享 Store 契约仍需下一切片验收。
