---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [models]
related_adrs:
  - docs/adr/0144-automatic-retry-after-service-failures-in-interactive-mode.md
  - docs/adr/0156-generation-progress-and-live-usage-estimate.md
  - docs/adr/0123-provider-service-failure-attribution.md
related_tests:
  - tests/models/test_stream_silence.py
  - tests/models/test_chat_transport_diagnostics.py
  - tests/models/test_attempt_usage.py
supersedes: []
---

# ADR 0166 工具调用开始之后，流式响应的静默只受本次请求的总期限约束

- 状态：评审中
- 日期：2026-11-17
- 修订：OpenAI Chat与Anthropic适配器的流式读取期限

## 背景

`io_timeout_seconds`原本是HTTP客户端的读取期限：流式响应连续这么久收不到任何字节，就按读取超时处理（可重试的
传输失败，ADR 0144）。本机自托管端点的默认值是300秒。

2026-11-15/16 NL2RepoBench正式回归（0.10.0b48，39题，本机vLLM两台副本）：35次模型请求恰好在第302秒被判读取超时，
作废后整步重来，累计约176分钟，平均每题4.5分钟。jinja一题同一步连续被截断3次。逐层核对后确认原因不在网络，也不在
模型生成：

- vLLM的Qwen工具解析器（`vllm/parser/engine/parser_engine.py::_safe_arg_prefix`）只把顶层字符串参数边生成边发出。
  数组、对象、数字等参数要等这个值写完整才整段发出，理由是类型校正可能改写已发出部分。值写完之前，解析器每一步都返回空增量，
  接口层不发SSE分片（`chat_completion/serving.py`里`if delta_message is None: ... continue`）。
- `apply_patch_batch`唯一的参数`files`是数组，模型一次写几万字符的文件内容时，连接上连续几分钟没有字节，生成到第300秒
  就被客户端断开。vLLM随之中止生成，已生成的内容全部丢弃。
- 本机实测（2026-11-17，同一副本）：工具调用开始的分片（ID与名称）在第1.44秒到达，参数开头`{"files": `在第1.55秒到达，
  此后静默20.6秒，第22.13秒整段参数（2633字符）一次到达。
- 本次回归里，参数为数组的调用没有一次在静默约300秒之后成功。参数为字符串的`exec_command`持续生成374秒仍然成功，
  因为它的内容逐字到达。

mini-swe-agent不受影响：它的唯一工具参数`command`是字符串，而且它用非流式请求，等待上限是1800秒。

## 决策

1. **静默按阶段计算（`models._provider_io.StreamSilence`）。** 从请求交给传输层发送起计时。工具调用开始之前，
   连续`io_timeout_seconds`收不到任何字节（含SSE注释与心跳），按读取超时处理，异常类型、错误分类与诊断原因
   （`httpx_read_timeout`）都与原来相同。收到任何工具调用分片之后，静默只受本次请求的总期限（`timeout_seconds`）约束。
   这时服务端已开始生成工具调用，参数可能整段晚到。
2. **字节到达由传输层回调。** `_bounded_http.TRANSFER_ACTIVITY`是只在执行这次IO的子Task里设置的上下文变量。
   `BoundedTransport`、`AnthropicTransport`在把请求交给下层发送时调用一次，`BoundedStream`每收到一批字节调用一次。
   这里只记录时间，不解析内容。工具调用是否开始，由各适配器的流状态判断：`ChatStream.tool_call_started`或
   `AnthropicStream.tool_call_started`。
3. **HTTP客户端不再单独限制读取。** 两个适配器的SDK与HTTPX客户端改为`Timeout(io_timeout_seconds, read=None)`。
   连接、写入与连接池期限不变；读取静默统一由第1条判断。模型目录预检本来就由`wait_for_io`把总时长限制在
   `io_timeout_seconds`内，行为不变。

## 没有改变的

- 总期限`timeout_seconds`、尝试次数、退避、按尝试记账，以及什么情况下还可以作废重来（ADR 0144）。
- 正文、思考阶段与等待首字节阶段的静默期限：与原来相同，仍是`io_timeout_seconds`。
- 请求内容、模型看到的任何东西、事件与摘要。
- 云端默认档的总期限（120秒）小于5分钟，行为实际上不变；自托管档（总期限1800秒）与思考档（900秒）受益。
- 三个平台相同；这里只用Python的asyncio与HTTPX，不涉及平台差异。

## 后果

- 本机vLLM上，写多个文件的长工具调用不再在第300秒被截断、整段作废。2026-11-17在247上做了对照（阈值设为5秒，
  同一请求）：0.10.0b48两次尝试都在静默约5秒后失败；本版一次完成，用时20～25秒，拿到完整参数。
- 代价：工具调用开始之后真的断线，最长要等到本次请求的总期限才发现（自托管档1800秒）；之前是300秒。连接被对端
  关闭、重置等可见的传输错误仍会立即报告，只有“连接还在、却再也不来数据”的情况会等更久。
- 计时从请求交给传输层时开始：连接、写入与等待首字节合计不超过`io_timeout_seconds`。原来这三段各自受这个期限约束。
  本机与常见云端的连接、上传都只要毫秒到数秒。
