---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [trusted_actions]
related_adrs:
  - docs/adr/0069-unified-coding-action-risk-route.md
  - docs/adr/0091-action-runtime-fencing-and-bounded-reconciliation.md
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/trusted_actions/test_router.py
  - tests/delivery/test_trusted_action_patch.py
  - tests/product_config/test_stale_approved_plan.py
  - tests/product_config/test_parent_closure_product_sdk.py
  - tests/product_config/test_server_and_cli.py
supersedes: []
---

# ADR 0142 批准后过期的计划以确定失败结束

- 状态：评审中
- 日期：2026-11-12
- 修订：[ADR 0069](0069-unified-coding-action-risk-route.md)的Route状态机（`ready`只能进入`running`）

## 背景

Beta用户在0.10.0b20的真实仓库复测中报告（B20-02）：批准一个新建测试文件的修改后，界面显示“审批：已批准”，
紧接着整轮以`uncertain_effect`中断，提示先检查工作区再继续；获批的文件没有写入，用户要手工核对工作区后重新发起。

复现与定位（0.10.0b20，真实终端界面）：

1. 计划在规划时冻结了Workspace Snapshot，其中包含工作目录以及目标所有上级目录的成员清单。审批窗口打开期间，
   这些目录里出现任何一个文件（编辑器临时文件、测试缓存目录、另一个终端写的日志），Snapshot就不再相等。
2. 批准后Router在执行前复核Snapshot（`_prepare_execution`），发现不相等，抛出`execution_plan_stale`。此时Route仍是
   `ready`：没有Claim Operation，没有调用Executor，工作区确定没有被改动。
3. 这次调用因此没有结果。Turn终结时把“非只读且没有结果的调用”一律当作未知效果，整轮中断。

补丁审批和命令审批都是这样：命令的Snapshot同样包含工作目录的成员清单。用户审批得越仔细（耗时越长），越容易遇到。

第1点（绑定范围过宽）另行处理（ADR 0143）。本ADR处理第2、3点：一个确定没有执行的操作被报告成了“结果未知”。

## 决策

1. **Route状态机增加`ready → failed`。** 只用于一种情况：执行前复核发现批准时绑定的Workspace事实已经变化
   （错误码`execution_plan_stale`），且Route仍是`ready`。迁移是一次CAS，写入一条审计事件
   （`from_state=ready`、`to_state=failed`、`error_code=execution_plan_stale`），不创建Operation。
2. **`execute`返回确定失败，不再抛出。** 结果是`ActionExecutionOutcome(kind="failed",
   error_code="execution_plan_stale")`。Agent侧得到一条普通的失败工具结果，本轮继续。
3. **不刷新Snapshot，不复用旧批准。** 旧计划成为终态，不能再执行。模型按当前内容重新发起后是一个新的调用：
   重新规划、重新冻结Snapshot、重新审批。这保持了Workspace模块的不变量“不能在执行阶段静默刷新Snapshot并继续复用
   旧Approval”。
4. **给模型和用户的说明是固定文本。** “批准后、执行前工作区发生了变化，本次操作没有执行，没有产生任何改动；
   请按当前内容重新发起这次操作（需要再次审批）”。说明由错误码选择，不包含路径、参数或异常正文。
5. **其余路径不变。** Route不是`ready`时（已在执行、已是终态、尚未批准）复核失败照旧抛出；进入Executor之后发现的
   变化仍按原规则处理（写类操作保守地记为未知，再由对账查证）。

## 为什么不是别的做法

- **保持`ready`并让上层把它当作可修正的拒绝**：Route会永久停在`ready`，旧批准对应的计划仍可被再次执行；一旦工作区
  恰好回到旧状态，它会在用户不知情时生效。终态更安全，审计也更清楚。
- **借用`ready → running → failed`**：不需要改状态机，但会创建一个从未调用Executor的Operation，并在两次写入之间
  留下崩溃窗口（`running`会被恢复流程收敛为`unknown`，再进入对账）。一次CAS没有这个窗口。
- **在执行阶段重新捕获Snapshot后继续**：等于用旧批准执行新事实，违反上面的不变量；审批时展示的差异也可能已经
  不成立。

## 后果

- 审批期间工作区发生变化时，用户看到的是一条失败（说明未执行）而不是整轮中断；模型通常立即重新发起，用户需要
  再批准一次。绑定范围收窄（ADR 0143）之后，无关文件不再触发这条路径，只有目标本身被改动时才会。
- 有程序持续向被绑定的目录写文件时，在ADR 0143落地前可能反复出现“过期—重新审批”。
- `ready → failed`是状态机的新增迁移。已有审计数据不受影响（旧数据里不存在这条迁移）；旧版本读取含这条迁移的
  审计链不会校验失败，因为迁移合法性只在写入时检查。
- 四个原先断言“批准后来源变化→抛出／中断”的测试改为断言新的行为。
