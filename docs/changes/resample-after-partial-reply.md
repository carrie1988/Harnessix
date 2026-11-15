---
doc_type: change-design
status: current
version: 1
code_revision: 39e1e1bfc52e8a6ad66b518dd6c82ee1fc98bb48
owners:
  - core
modules:
  - models
  - agent
  - product_ui
related_adrs:
  - docs/adr/0144-automatic-retry-after-service-failures-in-interactive-mode.md
  - docs/adr/0123-provider-service-failure-attribution.md
related_tests:
  - tests/agent/test_retry_after_thinking_break.py
  - tests/agent/test_model_attempts.py
  - tests/models/test_chat_transport_diagnostics.py
  - tests/models/test_cost_integration.py
  - tests/contracts/provider.py
  - tests/product_ui/test_rendering.py
supersedes: []
---

# 正文写到一半断开时整步重新请求

## 1. 文档摘要

模型回复的正文写到一半、连接断开或读取超时时，同一步自动重新请求，不再让整轮失败。断开前已经实时显示的
半截正文作废：它对应的正文Item标为失败、不进模型历史，界面撤下这段正文；新尝试的正文另起一个Item。
这是[ADR 0144](../adr/0144-automatic-retry-after-service-failures-in-interactive-mode.md)第5版（第3版延后的方案B）。

## 2. 需求背景

- 2026-11-15 NL2RepoBench冒烟：tinydb第11步，本机vLLM开始输出正文后卡住，300秒后读取超时，错误可重试、尝试上限
  为6，但适配器交付过正文增量后不再重试，整轮以`provider_transport`结束；无人值守的任务就此结束，交互用户要发送
  “继续”。
- 第3版（2026-11-13，负责人决定“先做A”）只重试断开前没有正文与工具调用的请求，把这种情况留到有数据后再定。
  负责人2026-11-15选B。

## 3. 设计目标与非目标

- 目标：完整回复交付之前的可重试断开不再结束整轮；同一步只留一份回复；不重复执行工具；计费照实。
- 非目标：完整回复或工具调用交付之后的故障（仍不重试）；缩短读取超时；摘要模型路径与多候选回退的规则。

## 4. 总体架构

| 层 | 变化 |
|---|---|
| 适配器（`models/_provider_io.py`） | 可作废的事件加上`TextStarted`与`TextDelta`；只有完整正文、工具调用与终态出现过后才不重试。OpenAI Chat与Anthropic共用 |
| 运行时（`agent/model_text.py`） | `restart_attempt`作废未完成的正文Item，清空本步骤正文块与输出字数，新尝试的正文换公开保护身份 |
| 运行时（`agent/runtime.py`） | 新尝试开始时本步骤事件计数重新从1计（只改原有一行） |
| 界面（`product_ui/rendering.py`） | 失败且没有正文的回复不显示为空行 |

## 5. 接口设计与数据结构

- 不新增公开接口或事件类型。作废的正文是`ItemFinished(status=FAILED, content=TextContent(kind="assistant_message"))`，
  错误为`AgentFailure(code="provider_attempt_discarded", retryable=True)`，与整轮失败时收尾未完成正文的形状相同。
- `ModelTextPublication`新增`discarded`计数与`protection_id(content_id)`：第N次作废之后的正文块以
  `<content_id>#attempt<N>`作为公开保护身份。

## 6. 流程与数据流

1. 适配器：尝试1流式交付`ResponseStarted`、`TextStarted`、若干`TextDelta`后连接断开；只出现过可作废事件且错误可重试，
   结束尝试1（失败）并在退避后开始尝试2。
2. 运行时：收到尝试2的`ModelAttemptStarted`，`restart_attempt`提交作废事实，清空本步骤正文状态，思考预览另起一段。
3. 尝试2照常交付，`TextCompleted`写入完整正文，`ResponseCompleted`记账；之后才提交并执行工具调用。

## 7. 失败、恢复、取消与超时

- 每次都断开时用完`max_attempts`（产品交互模式另受`service_failure_retries`连续次数约束）后，以原故障结束整轮。
- 重试共用该请求的总Deadline；取消照旧立即生效，未完成的正文按取消收尾。
- 完整正文或工具调用交付之后的断开仍以`invalid_provider_output`拒绝新尝试（适配器本来也不会发起）。

## 8. 持久化与数据流程

正文内容到`TextCompleted`才写入；作废只追加一个失败结束事实，不改写已有事件，回放结果与在线投影一致。用量按
尝试记账：报回用量的断开请求按已知计入，没有报回的按未知预留，不重复计算。

## 9. 安全与信任边界

新尝试的正文换公开保护身份，`SecretTextStep`不向已开始扫描的块续写；作废块被扣住的尾部不会放出。单步扫描预算
跨尝试共用，超过时照旧拒绝。

## 10. 可观测与错误分类

尝试记录照旧带失败类别与诊断（`chat_transport/v1:*`）；作废的正文Item带`provider_attempt_discarded`；界面的生成进度
说明“第 N 次尝试，上次网络中断或超时”。

## 11. 部署、兼容与回退

公开事件合同与配置不变。回退：把`_DISCARDABLE_EVENTS`恢复为第3版的四类事件即回到“出过正文不重试”；运行时与界面
的改动在不重试时不起作用。三个平台行为相同。

## 12. 限制与风险

推理服务反复在正文中途断开时，一步最多重试到尝试上限，每次都从头生成，耗时与费用相应增加；断开的那次请求可能已被
计费，与其他重试相同。

## 13. 验证

见ADR 0144第5版“验证”；新测试在0.10.0b46上失败、本分支通过，“正文已完整交付后重试”的协议用例在两边都被拒绝。

## 14. 源码与测试映射

| 源码 | 测试 |
|---|---|
| [`models/_provider_io.py`](../../src/harnessix/models/_provider_io.py) | `tests/agent/test_retry_after_thinking_break.py`、`tests/models/test_chat_transport_diagnostics.py`、`tests/contracts/provider.py` |
| [`agent/model_text.py`](../../src/harnessix/agent/model_text.py) | `tests/agent/test_retry_after_thinking_break.py`、`tests/agent/test_model_attempts.py` |
| [`agent/runtime.py`](../../src/harnessix/agent/runtime.py) | `tests/agent/test_retry_after_thinking_break.py` |
| [`product_ui/rendering.py`](../../src/harnessix/product_ui/rendering.py) | `tests/product_ui/test_rendering.py` |
| 用量与成本 | `tests/models/test_cost_integration.py` |
