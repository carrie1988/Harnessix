# ADR 0020：响应计费元数据与尝试绑定

- 日期：2026-09-03
- 状态：采纳，0.4.3b2 实施

## 1. 求证与边界

核对锁定 OpenAI 2.54.0 的 `ChatCompletionChunk.service_tier`，Anthropic 1.3.0 的 `Usage.service_tier`、`inference_geo`、`cache_creation`。不升级 SDK，不猜兼容平台字段。

- [OpenAI 官方响应契约](https://developers.openai.com/api/reference/ruby/resources/chat/subresources/completions/methods/retrieve)：响应实际等级可能不同于请求；这里引用协议语义，Python 接入以本地 SDK 类型为准。
- [Anthropic Service tiers](https://platform.claude.com/docs/en/api/service-tiers)：请求 auto/standard_only 与实际 standard/priority/batch 分开。
- [Anthropic Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)：缓存写入有 5m/1h 两种分项，可混合；两项之和才是对应完整写入量。

## 2. 数据与失败语义

新增 `ResponseBillingMetadata`：原生服务等级、推理地域、5m/1h 累计写入计数，均可未知。它不是认证过的供应商账单，也不包括 endpoint、Header、Prompt 或凭据。平台、部署地域、推理模式不能从 Adapter 类型或请求默认值推断。

`ModelUsageObserved.billing` 为可选的完整已知快照；None 表示本事件未观测计费元数据，保持先前事实。元数据和同次 Usage 在一个事务内提交，不引入旁路文件账本。标量只能从未知补齐，已知值漂移失败；计数必须严格非负且累计不回退，不能超过已知缓存写入总量。分项未追上最终总量时不推断 TTL。

流中省略字段不清空已知值；重复快照去重，不增加 Token。失败、取消、进程恢复沿用现有尝试结算，保留最近持久事实。没有独立自动重试或模型重放。

## 3. 版本与兼容性

- Agent Event / Thread 升至 **v5**，Provider Event 升至 **v3**；冻结此前所有 Schema。
- 旧 v1–v4 Event 导出移除新增空字段，旧事件禁止携带新元数据；旧尝试投影补空元数据。
- Migration 0005 只提高最低读者版本，不重写旧快照和事件；新写入投影为 v5。使用真实旧提交生成 v4 夹具，并验证旧读者拒绝新数据库。
- `UsageObservation`、PriceSnapshot、CostReport v1 不变。成本报告仍是已解析上下文的事后重算资料，不独立证明供应商元数据来源；来源证据在 Session 的对应尝试观测事件中。

## 4. 计价规则

新增上下文解析函数。只有可信宿主显式声明 `billing_provider=openai` 且 Adapter 为 openai_chat，或声明 `billing_provider=anthropic` 且 Adapter 为 anthropic，才将对应原生响应等级映射为计价等级；Anthropic 同时映射推理地域。代理平台/百炼等没有经过映射验证的字段不自动归因。OpenAI 的 auto 不视为确定的实际等级。

宿主填写的同一字段与已观测事实冲突时拒绝，不静默覆盖。直接 Anthropic 的两个 TTL 分项都已知、恰好覆盖最终缓存写入且仅一个为正，才推断单一 TTL；混合或不完整时不自动选择。已观测混合/不完整分项不能被宿主强行指定为单一 TTL。混合 TTL 需要特定费率时保持成本未知；本阶段不扩展多 TTL 价格表。

缓存写入总量明确为零时，TTL 与本次费用无关：不从零分项推断 TTL，也不拒绝宿主事先核对的 TTL。

`bind_price` 解析并校验上下文；`estimate_attempt`/`build_cost_report` 再检查已有绑定没有绕开新事实。旧报告继续按嵌入的 v1 用量和上下文重算，不添加虚假的来源证明或更改哈希算法。

## 5. 验收

领域累计/冲突/缺失、双 SDK 元数据与失败用量、取消/崩溃保留、v1–v4 混合升级与旧读者拒绝、价格绑定防错、全部回归和独立可选安装。默认 CI 离线。0.4.3c 另行验证百炼；凭据仅进程注入，不写入资料或诊断。
