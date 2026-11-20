---
doc_type: change-design
status: current
version: 1
code_revision: 9c6c60ac3954edbe8943493b8b1c07d313c807aa
owners:
  - core
  - product
modules:
  - models
  - agent
  - product_ui
related_adrs:
  - docs/adr/0166-silence-after-a-tool-call-starts-is-bounded-by-the-request-deadline.md
  - docs/adr/0156-generation-progress-and-live-usage-estimate.md
  - docs/adr/0144-automatic-retry-after-service-failures-in-interactive-mode.md
related_tests:
  - tests/models/test_stream_silence.py
  - tests/agent/test_generation_progress.py
supersedes: []
---

# 服务端攒工具参数时不再误判断线，界面如实说明

## 1. 文档摘要

有的模型服务只边生成边发送字符串参数，数组、对象参数要等写完才一次发出。实测本机vLLM与阿里云百炼的Qwen都是这样，
DeepSeek则逐段发出。产品此前把这段静默当成断线：连续`io_timeout_seconds`收不到字节，就把生成到一半的内容作废重来；
界面同时显示像出了故障的“模型服务 N 秒未返回新内容”。

本次改动后：工具调用开始之后，静默只受单次请求的总期限约束（ADR 0166）。界面在工具调用一开始就切到
“正在生成修改内容”；服务端攒参数时，界面说明这一阶段已经用了多久。

## 2. 需求背景

- 2026-11-15/16 NL2RepoBench正式回归（0.10.0b48，本机vLLM）：35次请求恰好在第302秒被判读取超时、整段作废，
  平均每题多花约4.5分钟。
- 2026-11-17实测，同一个“写两个文件”的工具调用：
  - vLLM：工具名在1.4秒到达，随后静默20.6秒，2633字符一次到达；
  - 百炼qwen3.8-flash：最长静默9.7秒，2669字符一次到达；
  - DeepSeek：1021个片段，最长间隔0.11秒。
- 产品默认值下，百炼关闭思考时静默上限只有30秒，一次写入超过约2500个Token就会被误判断线；开启思考时上限180秒。
- 负责人2026-11-17批准O1，选定界面方向（说明正在生成什么、已经用了多久），并要求当前及后续优化都从产品角度考虑用户体验。

## 3. 设计目标与非目标

- 目标：
  - 服务端合法地攒参数时，不中断生成；
  - 真正断线时，仍然能发现并重试；
  - 用户始终看得到模型在做什么、已经用了多久。
- 非目标：
  - 让服务端改为逐段发送。vLLM这一版没有对应配置，云端服务我们也改不了；
  - 把参数改成字符串结构。这样会影响模型写参数的准确性，需要单独评估；
  - 改变总期限、重试次数与计费。

## 4. 总体架构

| 层 | 变化 |
|---|---|
| 传输（`models/_bounded_http.py`、`_anthropic_http.py`） | 请求交给下层发送时、以及每收到一批字节时，回调本次请求的活动记录（上下文变量`TRANSFER_ACTIVITY`），不读内容 |
| 适配器（`models/_provider_io.py`、`openai_chat.py`、`anthropic.py`） | `StreamSilence`按阶段判断静默；SDK客户端不再单独限制读取 |
| 流解析（`_chat_stream.py`、`_anthropic_stream.py`、`_progress.py`） | `tool_call_started`；工具调用开始、参数还没有字节时，先报一次0字节与工具名 |
| 界面（`product_ui/generation_estimate.py`、`activity_indicator.py`） | 收到工具名就显示阶段；攒参数时显示这一阶段已用时 |

## 5. 接口设计与数据结构

- `ToolArgumentsProgress.utf8_bytes`下限由1改为0（`spec/provider-event-v4`放宽，向后兼容）。
- `InFlightRequest.arguments_held_back`：工具调用已开始，且参数不超过`HELD_BACK_PREFIX_BYTES`（64字节）。
- 配置不变：`io_timeout_seconds`的含义写入配置说明。

## 6. 流程与数据流

1. 请求交给传输层：开始计时。
2. 工具调用开始之前：
   - 每批字节都刷新静默期限；
   - 超过`io_timeout_seconds`没有字节，按读取超时作废重试。
3. 收到工具调用分片：
   - 适配器报0字节与工具名，界面切到“正在生成修改内容”；
   - 静默期限改为本次请求的总期限。
4. 参数整段到达：工具调用照常完成。

## 7. 失败、恢复、取消与超时

- 工具调用开始之前，读取超时的异常类型、错误分类与诊断原因（`httpx_read_timeout`）都与原来相同，照常有界重试（ADR 0144）。
- 工具调用开始之后，连接还在、却再也不来数据，要到总期限才报错（自托管档1800秒，思考档900秒，云端默认120秒）。
  这期间界面一直显示已用时，用户可以随时中断。对端关闭、连接重置等可见的错误仍会立即报告。
- 取消：仍由`CancelToken`立即生效，与原来相同。

## 8. 持久化与数据流程

不新增事件，不改会话记录。0字节进度与原有进度一样只是临时增量，不进入Item、模型历史和输出计量。

## 9. 安全与信任边界

- 活动回调只记录时间，不接触响应内容。
- 公开输出保护、出站请求检查不变。

## 10. 可观测与错误分类

- 工具调用开始之前的静默超时，诊断原因仍是`chat_transport/v1:httpx_read_timeout`。
- 总期限到达仍报`timeout_origin_unknown`。

## 11. 部署、兼容与回退

- 公开Schema只放宽一个下限，旧的进度生产者不受影响。
- 不声明`generation_progress`的客户端（`harnessix exec`、默认SDK）行为不变。
- 回退：撤销本提交即可，不涉及数据迁移。
- 三个平台没有差异：只涉及asyncio、HTTPX与终端文案。

## 12. 限制与风险

- 判断“服务端在攒参数”靠开头字节数（不超过64字节）。逐段返回的服务在5秒内就会超过这个数，不会被误判。
- 工具调用开始之后真的断线，最长要等到总期限才发现。这是为了不中断合法的长生成所做的取舍。
- 用户看到的Token估算：攒参数期间不增长，结束时换成服务商报回的实际值。

## 13. 用户能看到的变化

| 场景 | 此前 | 现在 |
|---|---|---|
| 本机vLLM或百炼上写一大段修改（生成超过静默上限） | 生成到一半被当成断线，整段作废重来；界面显示“模型服务 N 秒未返回新内容” | 一次写完；界面显示“正在生成修改内容 · 已用时 N 秒，模型服务写完才会返回” |
| 按完整键值发出参数的服务（如Anthropic） | 大参数到齐前一直显示“思考中” | 工具块一开始就显示“正在生成修改内容” |
| 逐段返回的服务（如DeepSeek） | Token估算每秒更新 | 相同 |
| 连接真的断开（对端关闭、重置） | 立即报告并重试 | 相同 |

## 14. 验证

- 单元与契约：`tests/models/test_stream_silence.py`覆盖两个适配器；`tests/agent/test_generation_progress.py`覆盖0字节通告与界面文案。
- 真实服务：247上真实vLLM副本，阈值设为5秒做对照。0.10.0b48两次尝试都失败；本版一次完成，用时20～25秒。
- 全量回归与b48对比，使用Homebrew git。

## 15. 源码与测试映射

| 源码 | 测试 |
|---|---|
| `src/harnessix/models/_provider_io.py`（`StreamSilence`） | `tests/models/test_stream_silence.py` |
| `src/harnessix/models/_bounded_http.py`、`_anthropic_http.py`（`TRANSFER_ACTIVITY`） | `tests/models/test_stream_silence.py::test_any_bytes_count_as_activity_before_a_tool_call` |
| `src/harnessix/models/_progress.py`、`_chat_stream.py`、`_anthropic_stream.py` | `tests/agent/test_generation_progress.py` |
| `src/harnessix/product_ui/generation_estimate.py`、`activity_indicator.py` | `tests/agent/test_generation_progress.py::test_the_tui_names_the_phase_and_estimates_what_was_generated` |
