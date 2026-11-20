---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [agent, trusted_actions, artifacts]
related_adrs:
  - docs/adr/0158-history-artifact-bodies-verified-once-per-store.md
  - docs/adr/0174-a-protection-check-that-cannot-finish-fails-only-that-call.md
  - docs/adr/0057-tool-result-model-view-and-artifact-binding.md
related_tests:
  - tests/agent/test_host_stall.py
  - tests/artifacts/test_model_history.py
  - tests/trusted_actions/test_projection_lifecycle.py
supersedes: []
---

# ADR 0177 宿主卡顿让内部核对超时时，稍等重做，不让整轮失败

- 状态：评审中
- 日期：2026-11-20
- 修订：历史Artifact核对（5秒）与Action输出投影（10秒）超时后最多再做两次；时限、错误码与持久形状不变

## 背景

两处内部核对有固定的短时限，平时只需几毫秒：

- **历史Artifact核对**：每次请求模型之前核对历史里引用的全部Artifact，限5秒
  （`agent/runtime.py::_verify_history_artifacts`，ADR 0158）。超时以`context_artifact_timeout`结束整轮。
- **Action输出投影**：命令或动作结束、Router已提交审计终态之后，按终态重建公开输出、发布完整输出存档并做
  正式校验，限10秒（`trusted_actions/agent_gateway_output.py`，`DEFAULT_OUTPUT_BUDGET`）。超时以
  `trusted_action_output_timeout`结束整轮，用户看到内部错误，命令其实已经跑完。

2026-11-18至20的评测里，这两处各因整机卡顿失败一次，都与被测代码无关：

- NL2RepoBench O3第1轮box：同机另一个评测任务的自测代码死循环，内存涨到182 GB，整机内存耗尽；同一秒
  历史核对超过5秒，整轮失败。
- golden dev23 b48 scikit-learn-12682：一次短暂卡顿中自动审批用了51秒，随后的输出投影超过10秒，整轮失败。

两处都是所有版本已有的行为。真实用户的机器同样会卡顿（内存紧张、杀毒软件扫描、休眠唤醒），一次卡顿就丢掉
整轮已经做完的工作，不符合“稳定、不误报失败”的要求。

## 候选方案

1. **超时后稍等重做（本决策）。** 每次仍按原时限，连续三次都超时才按原错误失败。
2. **放宽时限到30秒以上。** ADR 0158已经否决过对历史核对放宽：5秒是“本机核对异常慢”的信号，放宽会让持续
   变慢的会话每一步都白等几十秒；输出预算的Schema上限也只有30秒，而51秒的卡顿放宽后仍会失败。
3. **维持现状。**

## 决策

1. **重做的条件。** 只有“本层时限到了”才重做：历史核对是`asyncio.timeout`到期（`TimeoutError`）；输出投影是
   `trusted_action_output_timeout`。其他错误（Artifact过期、被改、找不到，投影不匹配、资源越界、发现Secret）
   以及取消一律原样抛出、不重做。
2. **次数与等待。** 第一次超时后等1秒、第二次后等2秒，最多做三次（`agent/host_stall.py::retry_after_host_stall`，
   `STALL_RETRY_DELAYS_SECONDS`）。整机卡顿通常几秒内恢复；本机持续变慢时，历史核对约18秒后、输出投影约33秒后
   照旧以原错误码失败。等待期间取消立即生效，不再做下一次。
3. **为什么重做是安全的。**
   - 历史核对只读：读取Artifact记录与正文、比对摘要；ADR 0158记住的“已完整核对”只在核对成功后写入。
   - 输出投影不执行任何东西：Router审计终态与双Hash已经提交，投影按它重建同一份输出；完整输出存档用由
     计划ID确定的Artifact ID发布，同一调用再次发布时返回已有收据（`publish_action_output`“发布或查询同一终态
     输出收据”），被取消的那次未提交事务随连接回滚。这与恢复路径重新查询Owner是同一条路径，后者已有
     “不重执行、不重对账”的测试。
4. **范围。** 只改这两处。Router内执行与对账阶段的返回校验期限（超时记为未知效果）、MCP服务端的投影期限、
   ADR 0174的保护检查规则都不变。

## 没有改变的

- 时限数值、错误码、可重试标记与用户看到的错误说明；核对与投影的内容与顺序。
- 事件类型、持久形状与重放；不新增配置。
- 平时（不超时）的行为与耗时。

## 后果

- 几秒内恢复的卡顿不再结束整轮；命令已跑完的结果照常交给模型。
- 本机持续变慢时，报错比原来晚十几到三十几秒，错误码不变。
- 重做期间界面停在原来的状态（准备上下文或执行工具），不新增提示；最长约33秒，之后要么继续、要么照旧报错。
- 与ADR 0174的关系：0174写明“存档检查超时时，输出投影共用的10秒期限也已用完，仍按原样失败”；本决策之后，
  这种超时先按卡顿重做，三次都超时才按原样失败。
