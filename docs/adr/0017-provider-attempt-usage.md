# ADR 0017：实际 SDK 的尝试与用量映射

- 日期：2026-09-03
- 状态：已接受，0.4.2b2 已通过离线验收
- 前置：ADR 0016、Agent Event/Thread v4、Provider Event v2

## 1. 求证基线

沿用锁定的 OpenAI Python SDK 2.54.0 与 Anthropic Python SDK 1.3.0，不因本次接入升级依赖。

- [OpenAI 官方缓存文档](https://developers.openai.com/api/docs/guides/prompt-caching)：缓存读写都是输入总量的分项；普通输入由总量减去读写分项得到。
- 本地 `openai/types/completion_usage.py`：Chat 的 `prompt_tokens_details` 已包含 `cached_tokens` 和 `cache_write_tokens`，`completion_tokens_details` 包含 `reasoning_tokens`，这些明细均可为空。
- [Anthropic 流协议](https://platform.claude.com/docs/en/build-with-claude/streaming)：message_delta 的 Usage 为累计值，message_stop 才是消息终结。
- 本地 `anthropic/types/usage.py`、`message_delta_usage.py`、`output_tokens_details.py`：输入包含未缓存输入、缓存创建和缓存读取；输出总量包含 thinking_tokens，不能把 Thinking 再加一次。

此处只读取公开计数，不开放私有 Thinking Block、原生压缩、服务器工具或 Fallback。计数映射不等于推理内容支持，也不等于真实平台兼容性已经验收。

## 2. 尝试边界

每次实际 SDK create 前 yield ModelAttemptStarted，恢复消费后才发 HTTP。参数或本地配置校验在此之前失败时，不伪造网络尝试。每次失败发 ModelAttemptFinished，再判断是否允许重试；重试生成新的内部 UUID 和连续 index。

Started/Usage/Finished 元数据不改变 Adapter 的 exposed 标志；只有已发布的响应/内容语义阻止自动重试。HTTP 失败通常没有用量，保持 unknown。已开始响应后失败不重试。

完成事件列表先全部校验，再发尝试 Finished 和响应终值；不在发出部分工具调用后继续发现同一响应的参数错误。Finished=completed 表示协议处理已完成，不代表 Turn 成功，例如达到输出上限仍会使 Turn 失败。

取消和消费者关闭不在生成器清理期间 yield；连接清理保持原行为，由 Kernel 结算已提交的开放尝试。直接消费 Provider 的调用者也必须承担这一步，不能把生成器停止解释为远端未收费。

## 3. 观测映射

### OpenAI Chat

- 首个合法 Chunk 记录实际模型和响应 ID，Usage 尚未知。
- 最终 Usage Chunk 到达且通过计数/顺序校验后立即记录 complete；后续 DONE 缺失或工具参数解析失败，仍保留该完整计数。
- input=prompt_tokens，output=completion_tokens；cached_tokens→cache_read_input_tokens，cache_write_tokens→cache_creation_input_tokens，reasoning_tokens→reasoning_output_tokens。
- 只有缓存读写两个分项均明确提供时，才推导 uncached_input_tokens。不能用某个分项缺失推断为零。
- 原始 SSE JSON 在 SDK 类型转换之前做严格校验，防止 bool/数字字符串被转成看似合法整数。整个响应的 model/id 必须稳定。

### Anthropic Messages

- message_start 和后续 message_delta 记录 partial 累计快照；重复累计值不重复增加预算。
- input_tokens 保存在 uncached_input_tokens；两个缓存分项都明确已知时，才计算包含缓存的 input_tokens。
- output_tokens_details.thinking_tokens 映射为 reasoning_output_tokens，后续未提供明细时保留此前已知值。
- message_stop 时将可确定的完整总量提升为 complete；无法确定缓存计数仍保持 partial，并沿用当前严格配置拒绝成功响应，不猜零。
- context_overflow/SSE 错误/断流保留先前已观测用量；被拒绝的坏事件不能覆盖已提交的合法快照。

计数更新先验证完整候选快照，再替换本地累计状态。旧 Agent Schema 和 Migration 不变，本切片只实现已发布 v2 契约。

## 4. 有意保留的边界

账本只保存经过验证并交付 Kernel 的观测，不从原始异常、HTTP Header 或未通过帧验证的网络缓冲猜测用量。网络块中若包含违反原始帧约束的数据，整块可能在 SDK 消费前被拒绝；此前未发布的用量保持未知，不声称捕获了服务器全部消费。

本次不保存缓存 TTL 细分、服务等级或实时价格；后续成本估算若缺少定价必需信息必须显示未知，而不是补默认价。现有 Token 上限仍是已知消费后的调度约束，不能保证部分/未知请求的实际账单硬上限。

ModelAttempt.provider 标识 Adapter 类型，不是计费商户或账号；例如 openai_chat 也可连接百炼等兼容端点。后续价格键必须显式绑定计费平台/配置，不能仅凭 Adapter 类型套用 OpenAI 价格。

## 5. 验收门禁

两个真实 SDK + 各自 HTTP 替身验证：请求意图在 HTTP 前持久化、重试次数/UUID/未知用量、模型/响应身份、缓存/推理子集、累计不重算、失败/截断/取消用量、工具门禁、跨 Provider/审批继续、秘密不进入事件和观测。补真实子进程切点，并运行全量回归、异步调试、五个离线入口和三类独立可选依赖安装。

后续 0.4.3a 已完成显式价格绑定的事后估算与报告离线验收（[ADR 0018](0018-versioned-token-cost.md)）；实际计费上下文自动采集和真实 API 尚未验收，不使用离线成功替代。
