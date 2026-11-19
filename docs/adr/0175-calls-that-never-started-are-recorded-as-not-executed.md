---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [trusted_actions, agent]
related_adrs:
  - docs/adr/0174-a-protection-check-that-cannot-finish-fails-only-that-call.md
  - docs/adr/0142-stale-approved-plan-fails-without-effect.md
related_tests:
  - tests/product_config/test_unstarted_calls_not_executed.py
  - tests/trusted_actions/test_gateway_error_runtime.py
  - tests/product_config/test_patch_precondition_rejection.py
  - tests/product_config/test_git_decision_link_ledger_sdk.py
supersedes: []
---

# ADR 0175 本轮结束时尚未开始的调用记为未执行

- 状态：评审中
- 日期：2026-11-18
- 修订：Agent收尾“尚无结果的写调用一律记为unknown”；Trusted Action Route的`ready→failed`只用于过期计划（ADR 0142）

## 背景

Turn失败或中断时，收尾逻辑（`AgentRuntime._finish`）为尚无结果的调用补结果：只读调用记为失败或取消，写调用除非能
核对出终态，一律记为`unknown`，整轮因此以`interrupted`/`uncertain_effect`结束、禁止自动重放。Beta Issue #37报告人的
Fastify会话里，一条需要审批的宿主测试命令与失败的grep在同一步，命令从未准备，却被记为未知效果，评测者只能人工核对
它到底有没有启动（报告的期望：明确记为待批、未执行或已有确定执行收据）。ADR 0174让保护检查超限不再使Turn失败，但
Turn因其他原因失败（发现Secret、Provider错误、时限、宿主中断等）时，同样的问题仍在。

Trusted Action的Router能证明调用没有开始：计划在规划时才创建Route；Executor只在Claim之后调用，而Claim在调用前原子提交
`ready→running`。负责人2026-11-18选方案A：有Router证据的调用记为未执行，没有证据的照旧记为未知。

## 决策

收尾时[`recover_action`](../../src/harnessix/trusted_actions/agent_gateway_support.py)按Route核对每个尚无结果的Trusted Action调用：

1. **没有Route。** 写调用从未规划，Executor不可能被调用：结果为`cancelled`，错误码`action_not_started`
   （[`unplanned_result`](../../src/harnessix/trusted_actions/preparation_rejection.py)）。只读调用照旧按本轮错误收尾。
2. **待审批。** 由`system.cancel`拒绝并关闭Route，原因区分用户取消（`turn_cancelled`）、时限已过
   （`turn_time_budget_exceeded`，Issue #39已有）与其他原因（`turn_ended`，新增）；结果为`cancelled`，错误码分别为`cancelled`、
   `time_budget_exceeded`、`action_not_started`（[`close_unstarted_route`](../../src/harnessix/trusted_actions/preparation_rejection.py)，
   取代只处理前两种原因的`cancel_pending_approval`）。
3. **已批准、尚未Claim（`ready`）。** 由[`operation_router.end_unstarted_plan`](../../src/harnessix/trusted_actions/operation_router.py)以
   `ready→failed`、错误码`action_not_started`结束，不再能被执行；结果为`cancelled`，错误码按上一条区分原因，说明
   “本轮在这次调用开始前结束，调用没有执行”。`ready→failed`此前只用于过期计划（ADR 0142），同样是尚未Claim、确定没有效果。
4. 有了这些结果，`_finish`不再因它们把Turn改为`interrupted`：Turn以原失败码结束（如`public_output_secret_leak`、
   `time_budget_exceeded`），用户可以照常继续会话。

## 没有改变的

- `running`、`reconciling`、`unknown`等已开始或可能开始的Route照旧核对或记为未知效果。
- 不经Trusted Action的写工具（没有Router证据）照旧记为`unknown`；产品里的命令、补丁、回滚与Git动作都经Trusted Action。
- 收尾不启动任何副作用；Session事件形状、重放与公开协议不变（只多出错误码`action_not_started`与审批原因`turn_ended`）。
- Git审批历史投影本来就不能表示“审批待定时本轮因其他原因结束”，本次不改变这一点。

## 后果

- #37报告的情形（同批调用失败时旁边未开始的命令）记为“未执行”，Turn以失败结束而不是中断，可以自动续做。
- 已批准但没来得及开始的命令随本轮结束，审计记录为失败（`action_not_started`），不会在之后被执行。
- 三个平台行为相同：判断在Router存储与Python层完成，没有平台分支。

## 验证

修改前失败、修改后通过：

- `tests/product_config/test_unstarted_calls_not_executed.py`：搜索结果含受保护的值使Turn失败时，同一步从未规划的改动记为
  `cancelled`/`action_not_started`、没有Route、文件不变，Turn为`failed`（此前`interrupted`/`uncertain_effect`）；已批准、未开始
  的宿主命令在时限已过时随本轮结束，命令没有运行，Route为`failed`/`action_not_started`、从未交给Executor，Turn为`failed`（此前
  `interrupted`）。
- `tests/trusted_actions/test_gateway_error_runtime.py`：规划上下文或审批Review回调失败时，写调用记为未执行、Turn为`failed`
  并保留原错误码（此前`interrupted`/`uncertain_effect`）；Owner输出失败（已执行）照旧为未知效果。
- 按本决策更新预期的既有测试（原先断言“未开始也记为未知效果”）：`test_patch_precondition_rejection.py`（Review回调报错或
  伪造拒绝原因时改动记为未执行、Route由系统关闭，仍不采信回调自报的原因；Route关不上的竞争情形照旧为未知效果，竞争审批人已
  批准的以`ready→failed`结束）、`test_runtime_sampled_token_budget.py`（规划后、Session审批前崩溃）、
  `test_trusted_action_patch.py`（孤儿Review仍不可读）、`test_call_admission.py`（准入结果无效仍失败关闭）、
  `test_git_decision_link_ledger_sdk.py`（批准后取消：Turn为`cancelled`、Route为`failed`，Ledger仍拒绝追加为审批前取消）。
