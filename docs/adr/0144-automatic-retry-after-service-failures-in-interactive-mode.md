---
doc_type: adr
status: reviewing
version: 2
code_revision: pending
owners: [core]
modules: [agent, product_config, models]
related_adrs:
  - docs/adr/0123-provider-service-failure-attribution.md
related_tests:
  - tests/agent/test_model_attempts.py
  - tests/context/test_pressure_window_candidates.py
  - tests/product_config/test_service_failure_retries.py
  - tests/product_config/test_retry_limit.py
  - tests/product_ui/test_configure_vendors.py
supersedes: []
---

# ADR 0144 交互模式下服务方故障后的自动重试

- 状态：评审中
- 日期：2026-11-12
- 修订：[ADR 0123](0123-provider-service-failure-attribution.md)第6节“付费端点的交互模式：维持停止”

## 背景

0.10.0b20真实仓库复测中，一个OCR开发任务进行到第57次模型请求时，流式响应中途断开
（`provider_transport`，`httpx_protocol_failure`）。该次请求的用量没有报回，按ADR 0123第6节的规则，付费端点的
交互模式“不再发起付费请求”，整轮以失败结束，用户输入“继续”后才接着做。同一轮测试里另外两次连接失败
（连接未建立，确定未计费）都自动重试成功。

这条规则的出发点是费用不明时不替用户继续花钱。但终端界面没有单任务费用上限（产品负责人决定D1），用户就在
屏幕前；一次网络抖动最多多付一两次请求的费用，换来的却是二十分钟的任务被打断。产品负责人2026-11-12决定：
网络中断允许自动重试，重试次数做成配置项，默认5次。

## 决策

1. **付费端点的交互模式下，服务方故障造成的用量未知不再立即停止。** 服务方故障指ADR 0123第3.1节的三类：
   `provider_transport`（连接与超时、流式中断）、`provider_rate_limit`（429）、`provider_provider_internal`（5xx）。
2. **连续至多5次。** 本Turn最近连续的“服务方故障且用量未知”的尝试不超过5次时，允许发起下一次请求；第6次连续
   失败之后停止，错误码和说明与原来相同（原故障码，“按计费安全规则不再发起付费请求”）。中间只要有一次请求成功，
   计数重新开始。次数是配置项：产品配置的`service_failure_retries`（0～20，未设置时为默认值5，0表示不自动重试），
   `harnessix code configure --service-failure-retries N`写入；默认值是`SERVICE_FAILURE_RETRIES = 5`。
3. **其他原因的用量未知照旧停止。** 例如输出无效、响应正常结束却没有报回用量。这些不是一时的故障，重试没有意义。
4. **继续时仍按预留额记账。** 与无人值守模式相同：每个用量未知的尝试按本Turn已知的单次最大消耗预留，预留耗尽
   Token预算时停止（`require_reserved_unknown_usage`）。
5. **向导生成的配置把单次请求的尝试上限由4提高到6**（首次请求加至多5次重试），等待时间仍按`Retry-After`与
   指数退避取较大值。配置合同的`max_attempts`上界相应由5放宽到6。已有配置不变。
6. **Runtime参数由开关改为策略对象。** `AgentRuntime(continue_after_unknown_usage=...)`改为
   `AgentRuntime(unknown_usage=UnknownUsagePolicy(always=..., service_failure_retries=...))`；默认值仍是“停止”。
   无人值守运行和不计费的本地端点传`always=True`，行为不变。

## 没有改变的

- 响应已经开始输出之后才断开的请求，适配器不重试（ADR 0123），该轮仍以原故障结束，界面提示直接发送“继续”。
  要在这种情况下自动重试，需要作废已经显示的半截内容并重新采样这一步，涉及事件合同，另行设计。
- 确定未计费的失败（HTTP错误响应、连接未建立）本来就会重试，不受本ADR影响。（第2版起，配置了`service_failure_retries`时它们的次数也受这个设置限制，见下文“修订”。）
- 评测与冒烟配置仍要求`max_attempts=1`。

## 后果

- 重试的那几次请求有可能已被服务商计费；最坏情况是一次故障多付5次请求的费用。这是决定里接受的代价。
- 单次请求最多等待的退避时间变长（向导配置的基数是1秒：1、2、4、8、16秒），仍受该请求总时限约束。
- 7个公开Schema里`max_attempts`的`maximum`由5变为6，是向后兼容的放宽。

## 修订（第2版，2026-11-13）

0.10.0b24的Beta复测（GitHub Issue #2）：用`--service-failure-retries 2`生成配置，让端点对每个请求都返回HTTP 503，
仍然发出6次请求，与不设置时相同。用户看到的是“重试次数配置不生效”。

原因在第1版的分工：一次请求内的重试次数由Profile的`max_attempts`决定（向导写6）；本ADR的设置只限制
“用量未知”的失败。503、429和没建立的连接确定没有计费，记为零用量，不在“用量未知”之列，于是这个设置管不到它们。
帮助文字和用户手册却把它写成“网络中断、限流、服务端错误后的重试次数”。

决定：配置了`service_failure_retries`时，它同时决定一次模型请求最多尝试几次——N+1次，不超过`max_attempts`的
合同上限6次，所有失败类型一样；没有配置时沿用Profile的`max_attempts`，行为不变。用户只需要认一个数。

- 设置值大于5时按5次重试（一次请求最多6次尝试）；不收紧取值范围，已经写入20以内取值的配置照常可用。
- 设置了它的旧配置（`max_attempts`为4）会按设置的次数尝试，比原来多；这正是设置的意思。
- 交互与无人值守模式相同。评测与冒烟直接使用`max_attempts=1`的模型配置，不经过产品配置的这条路径。
- 配置的回退模型是另一个Profile，主模型用完次数后，回退模型按同样的次数尝试。
