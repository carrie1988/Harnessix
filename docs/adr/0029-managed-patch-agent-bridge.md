# ADR 0029：受管 Patch 的调用绑定与 Agent 接入顺序

- 日期：2026-09-04
- 状态：0.5.3b2a 已实现并通过本地验收；完整 Kernel 接入留到 b2b

## 1. 决策与范围

基于 `20b28d2` 的受管执行后端，先交付不改变默认 Kernel 的宿主桥接层，再升级 Agent 事件和审批协议。原因是当前持久审批固定为 `kernel-read-only/v1`，Session 与副本账本又不在同一事务，不能把通用写工具放行当作集成完成。

本片新增 `ManagedPatchBridge`，复用 ToolCallContent、ToolExecutionScope、ApprovalRecord、PatchProposal 和受管后端。它提供单一 `definition()`，不实现旧 ToolRuntime/ScopedToolRuntime，不注册模型工具，也不自行驱动模型、Session 或审批 UI。宿主仍负责验证活跃调用、持久批准、时间预算及审批消费边界；调用归属摘要不是密码学凭证或活跃租约。

## 2. 调用、计划与审批

模型提案只含相对路径、expected_revision 和精确编辑。Thread/Turn/Call、副本 ID、plan_id、actor、审批指纹一律不是模型输入。定义摘要绑定实现版本、副本身份、Workspace scope、输入输出契约，换副本或契约即不能沿用旧调用。

`ManagedPatchCallPlan` 是宿主契约，包含调用归属、完整 manifest、后端指纹和桥接审批指纹。稳定 request_id 由策略版本与调用归属/调用摘要计算；桥接审批指纹绑定整份不可变计划。后端已有指纹保持原义。宿主批准时必须使用桥接指纹，不能混用只读审批或后端指纹。

`prepare` 先按稳定请求查找既有计划；仅不存在时准备并保存。已有计划只复核，不重新计算或覆盖私有前后镜像。参数/调用/副本/manifest 任一不匹配即失败。已消费计划不能重新准备或执行。`review` 只核对计划及当前前镜像，不记录决定或改文件；拒绝不要求当前文件仍匹配前镜像。

`execute` 要求宿主传入指纹匹配的 ApprovalRecord，核对调用及保存的计划，随后镜像批准/拒绝到副本账本。拒绝不写文件；批准复核后走既有一次性执行器。返回宿主私有计划/账本证据及独立的有界模型结果，私有 ID、指纹、完整前后镜像不进入 ToolResult.output。

## 3. 两个持久域的目标顺序

下列 Session 行为属于后续 b2b，不是本片已实现能力：

1. Session 持久化模型 Call；Kernel 建立可信调用作用域。
2. 副本保存完整计划；Session 随后保存新的写审批请求和 WAITING_APPROVAL。
3. 答复时先复核计划，再只持久化 Session 审批决定，不立即写文件。
4. Resume 先持久离开 WAITING_APPROVAL，消费恢复边界，再调用桥接执行。
5. 副本先持久 started，再执行/核对文件效果，最后 Session 发布 ToolResult。

计划已保存但 Session 未提交时，可用稳定请求找到原计划；重启不能自动重做提案。文件已改但 Session 结果未提交时，只能加载/核对已有计划，不再次执行。必须新增 Agent 事件/投影版本与最低 reader migration，旧 Schema/migration 保持字节不变；旧只读审批语义不变。

## 4. 取消、恢复与不确定性

桥接所有磁盘操作在串行后台线程中运行。CancelToken、Task.cancel 和外层 timeout 均通过 ReadOperation 请求停止，等待线程收尾后才返回；重复取消也不能遗留正在写的线程。桥接关闭等待本桥接活动操作，但不关闭或删除宿主拥有的副本；宿主必须先退出桥接再关闭副本。不可中断的系统 I/O 不承诺硬实时退出。

`recover` 只查找、读取与 reconcile 已有计划，绝不 prepare/save/reply/execute。核对可能追加观察账本事件，但不修改目标文件。未找到计划或 pending/approved 说明尚未消费写意图；failed/rejected/observed_before 为已知未成功；started/uncertain 必须先核对。applied/observed_after 只有与宿主批准和调用计划匹配才返回 succeeded，否则 unknown。第三种内容、缺失、不可读、无充分归因或账本不可用均保持 unknown。执行抛错不等于无效果，宿主必须走恢复路径，不能一概转换为 failed。

桥接返回的是历史账本事实，不承诺已 applied 的文件后来没有外部变化。取消后文件可能已成功修改；取消不是回滚。恢复得到 observed_before 也不产生重试许可。实际活跃 Session/时限检查属于 Kernel，不能把本片宿主声明的 ApprovalRecord 当作已完成持久授权验证。

## 5. 分片验收门禁

### b2a：本片

- 稳定调用绑定、计划找回/复核、严格模型参数和冻结独立契约。
- 批准/拒绝、错绑/篡改、重复执行拒绝、源文件不变、私有证据不进入模型输出。
- Task/协作取消/超时/重复取消和关闭排空，替换前后结果诚实分类。
- 真实子进程退出后，用原调用找回原计划；恢复不准备/批准/写入，目标 inode/时间戳保持不变。
- 既有 Kernel 默认只读、全部旧 Schema/迁移/工具定义保持不变；无真实 API 或中间件操作。

### b2b：下一片

- 新 Agent 写审批/恢复结果契约、兼容迁移、专用 Kernel 写端口和恢复结算。
- 真实 SDK 离线 HTTP：读→提案→审批重开→执行→读回→回答。
- Session × 副本账本的真实崩溃矩阵、旧只读审批升级、Kernel 取消与副作用结果结算。

只有 b2b 通过全量回归、独立 wheel 和 Linux/macOS CI，才标记模型 Patch 接入完成。Process、多文件修改和自主 Coding Eval 不在本 ADR 的完成声明中。
