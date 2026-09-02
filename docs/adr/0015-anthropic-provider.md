# ADR 0015：第二类 Provider 与用量演进边界

- 日期：2026-09-03
- 状态：已接受；0.4.2a 已实现并通过离线验收，0.4.2b 待实施
- 范围：0.4.2a Anthropic Adapter；0.4.2b 用量明细独立验收

## 1. 接口求证

本切片使用官方 Anthropic Python SDK 1.3.0 的 Messages API。该版本使用 HTTPX2，与现有 OpenAI Adapter 的 HTTPX 不是同一种 Client/Transport 类型；不通过降级 SDK 或不安全强转来掩盖差异。

- [Messages 流协议](https://platform.claude.com/docs/en/build-with-claude/streaming)：消息、Block、累计 Usage 与终态顺序。
- [工具定义](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)：客户端工具及参数 Schema。
- [缓存计数](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)：输入总量须包含缓存读取和创建。
- [错误分类](https://platform.claude.com/docs/en/api/errors)：HTTP 与 SSE 错误边界；普通 400 不等于上下文超限。
- [Stop Reason](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)：上下文超限、拒绝与服务端暂停不是正常工具继续。
- 本地核对 SDK 的 AsyncAnthropic 构造、AsyncStream、RawMessageStreamEvent、Usage/MessageDeltaUsage 与异常工厂。禁用默认重试，不使用 SDK 内置 Tool Runner。

## 2. 复用与隔离

提取不依赖 SDK 的配置基类、History 配对/别名、严格 JSON 解析和截止时间等待；不把 Anthropic 请求翻译成 OpenAI API 请求，也不让任一 Adapter 导入另一 SDK。

有界字节/SSE frame 检查沿用同一实现。HTTPX2 仅增加类型正确的薄封装，认证仍来自显式环境引用，默认禁止代理、重定向、压缩和自定义环境 Header。普通 HTTP 错误 body 也必须关闭。

补充两个实测边界：SDK 的默认解析会将 bool Usage 转换为 int，故原始 SSE JSON 必须先做严格验证，不能仅检查 SDK 对象；单个网络块也能容纳大量 Ping，故 max_chunks 同时限制网络分片数与 SSE frame 数，不只限制有语义的 Kernel Event。

两个 Adapter 保持独立的供应商流状态机，复用同一 ProviderContract。错误发生在 HTTP 阶段还是开始流之后由各供应商协议决定，公共契约约束错误语义、禁止工具释放和重试次数，不要求所有错误都发生在 response_started 之前。

## 3. 支持范围

- 支持 user/assistant 文本、客户端 tool_use/tool_result 与多工具配对；连续结果合并为 user 内容块，以内部 Call UUID 建立稳定引用。
- 不发送 assistant prefill。请求必须以 user 或工具结果结束。
- 当前显式禁用 thinking，不接收/过滤后重放签名 Thinking Block；强制 Thinking 的模型不在此配置范围。未开放服务器工具、Fallback、Citations、图片、原生压缩和 Beta 控制。
- 未知语义事件返回固定 invalid_provider_output，而不是默默遗漏。Ping/注释允许且受传输预算约束。
- Tool Call 只在 Block 完整、消息终态、Usage 校验和 EOF 确认后交给 Kernel。EOF 前的错误或重复终态不能触发工具。
- 首事件前有界重试；已向 Kernel 暴露事件后不重发请求。取消覆盖建连、读流、错误 body 和退避，且不依赖同一个 asyncio Task 持续消费。

## 4. 用量及失败语义

message_delta 的 Usage 是累计值，不能把每次值相加。输入总量是 input_tokens + cache_creation_input_tokens + cache_read_input_tokens；输出取最后累计值。显式字段必须非负且累计不回退。

本切片在完整响应末尾要求可确定输入、输出和两个缓存计数；计数可来自 message_start 或后续更新。缺失缓存计数不猜测为零，而是拒绝将不完整账目作为完整预算。此严格配置暂不支持省略缓存计数的响应。

当前持久 Usage 仍只有输入/输出总量。缓存/推理明细、失败请求已消费用量、Attempt 身份和价格版本将在 0.4.2b/0.4.3 设计版本化记录；此处不修改旧 Agent Event/Thread/Provider Event Schema 或 Migration。上下文超限归一化为 context_overflow；普通 400/413 保留 invalid_request，不从任意错误文案推测业务分类。

max_tokens/refusal/pause_turn 不触发工具调用或自动续跑；可获得完整总量时沿用已实现的失败终态用量记录。上下文超限和中途异常当前只记录结构化失败，完整失败 Usage 的缺口在 0.4.2b 处理，不声称当前统计等于账单。

## 5. 0.4.2b 后续验收方案

用量明细应记录在每次 Model Attempt 的不可变事实中：明确供应商/请求模型/实际模型、计数包含关系、完整/部分/未知状态及可选缓存、推理明细。未知是 null，不是 0；总量不能重复包含缓存或推理子集。

新增语义需版本化 Schema 与 Session 最低读者版本，并用 0.4.2a 之前真实 v3 Transcript 验证升级、混合 Replay 和字节级旧事件导出不变。价格和估算成本采用独立版本化表，不能在历史事件中回填当前价格。只有这些门禁及两类 Provider 的用量映射均通过，才标记 0.4.2 完成。

## 6. 本切片门禁

两类 Adapter 的同一核心契约、Anthropic 分片/坏事件/取消/错误 body/缓存总量测试、Kernel 多步骤与跨 Provider 继续、审批重启、旧功能全量回归、独立可选依赖安装与中文文档同步。真实 Anthropic/百炼请求不属于离线门禁，不使用 Mock 成功替代真实平台验证。
