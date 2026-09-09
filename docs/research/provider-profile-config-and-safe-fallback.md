# Provider、Profile、配置与安全 Fallback 源码研究

## 1. 研究目标

本研究为 Harnessix Code 0.8.6 冻结以下生产边界：

- 模型 Provider、模型 Profile 与运行参数如何分层；
- 配置来源、迁移、并发更新和诊断如何形成可审计事实；
- API Key 如何只以版本化 Secret 引用进入配置；
- Provider 自动 Fallback 在什么事件边界前仍然安全；
- 远端 MCP OAuth 与公网 Git 凭据是否应与模型 Provider 配置共用实现。

研究只提取可验证的架构原则，不复制第三方实现。Claude Code 样本属于非官方逆向材料，
仅用于行为交叉验证，不作为接口兼容或许可证来源。

## 2. 研究基线

| 项目 | 本地源码版本 | 重点文件 |
| --- | --- | --- |
| Codex | `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67` | `codex-rs/config/src/config_toml.rs`、`profile_toml.rs`、`diagnostics.rs`、`merge.rs`、`codex-rs/core/src/thread_manager.rs` |
| OpenCode | `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5` | `packages/opencode/src/provider/provider.ts`、`auth/index.ts`、`config/tui-migrate.ts`、`session/llm.ts` |
| Claude Code 逆向样本 | `2ca5ddabfed5f220812ea11f029eda03b21bc4c1` | `src/query.ts`、`src/services/api/withRetry.ts`、`src/utils/model/providers.ts` |
| Harnessix Code | `bf10c9d6ffea373f1a6e39fc8621b65d08133e03` | `models/config.py`、`models/openai_chat.py`、`models/anthropic.py`、`secrets/provider.py`、`agent/runtime.py` |

## 3. Codex 求证结果

### 3.1 配置与 Profile

Codex 的 `ConfigToml`拒绝未知字段，区分内置 Provider、用户 Provider、模型和命名
Profile。Profile 不是别名字符串，而是模型、Provider、推理、Sandbox、审批等运行选择的
组合。配置通过有来源信息的层合并，诊断可定位到具体文件范围；写配置具有版本冲突语义。

可吸收原则：

1. Provider 定义与 Profile 选择必须分离；同一 Provider 可承载多个精确模型 Profile。
2. 未知 Profile 或模型必须报错并给出受限诊断，不能静默选择“相近”模型。
3. 配置来源及激活版本必须可以追溯，不能只保留最终内存对象。
4. 内置安全约束不能被低优先级用户层覆盖。

### 3.2 Fallback

Codex 的启动期 Provider/模型回退是显式允许的选择，实际请求使用已经冻结的
Provider/模型；会话不会在一个已暴露响应中任意替换底层流。Provider 与模型最终选择进入
会话事实，而不是只写调试日志。

对 Harnessix 的约束是：Fallback 必须是 Profile 中显式声明的有序链，并且只能发生在一次
候选尝试已经结算、尚未产生响应可见事件时。

## 4. OpenCode 求证结果

OpenCode 的 Provider 目录同时包含能力、成本、上下文限制和状态；可用 Provider 由环境、
认证、配置及插件组合得到。精确模型查找失败时提供候选提示，而不是直接替换。Provider
allowlist/denylist 提供了产品级可用性控制。

同时观察到两类不适合直接继承的边界：

- 灵活的配置替换和插件合并扩大了 Secret 与动态代码的攻击面；
- 某些认证模式把 API Key 写入权限受限的本地 JSON。文件权限降低了误读风险，但仍把
  Secret 正文纳入备份、同步和取证范围。

Harnessix 因此保留能力目录思想，但配置只保存 Secret 名称、精确版本和宿主来源定位，
不保存 Secret 值。

OpenCode 的 SDK 实现回退发生在请求流真正执行前；这支持“实现选择可以回退，已暴露模型
响应不可回退”的边界。

## 5. Claude Code 逆向样本求证结果

样本在切换 fallback model 时丢弃当前临时 assistant/tool 缓冲和旧流执行器，并移除与原模型
绑定的 thinking 签名。中间错误在恢复决策完成前不会作为最终错误暴露给上层。该行为说明：

- 不能把旧模型私有签名、流游标或半成品 Tool Call 发送给新 Provider；
- 一旦上层已经观察文本或 Tool Call，自动切换会形成重复输出或重复副作用风险；
- Fallback 是新尝试，不是旧 HTTP 流续传。

Harnessix 采用更保守规则：`response_started`即关闭自动 Fallback 窗口，不等待首个文本
Token 或完整 Tool Call。

## 6. Harnessix 现状与差距

### 6.1 已有能力

- OpenAI-compatible 与 Anthropic Adapter 已有 HTTPS、禁环境代理、禁重定向、响应字节/
  Frame/Chunk 上限、取消、总超时和内部重试。
- Adapter 内部重试在任何非用量 Provider Event 暴露后停止。
- `ModelAttemptStarted/UsageObserved/Finished`已经持久化每次尝试；Reducer 强制同一步骤尝试
  序号连续，且只能在失败尝试后开始下一次尝试。
- `SecretProvider`已经提供版本校验、有界解析及可变字节清零。
- Turn Retry 已在持久终态边界支持显式跨 Provider 重试，且不会重放完成工具。

### 6.2 缺口

1. Adapter 构造函数只能直接读取环境变量，无法消费现有 `SecretProvider`。
2. 没有版本化产品配置、命名 Profile、配置摘要、激活 CAS 或迁移收据。
3. 没有离线能力/依赖/Secret 诊断，启动失败只能落在较晚的 Provider 构造阶段。
4. 没有跨 Provider 的安全 Fallback 编排；直接拼接 Adapter 会重复尝试序号。
5. 薄 CLI 仍要求用户自行提供已经装配的 App Server 程序。

## 7. 威胁与失败矩阵

| 场景 | 风险 | 冻结行为 |
| --- | --- | --- |
| 配置含未知字段或重复 JSON Key | 拼写错误被忽略、键覆盖 | 有界严格 JSON，失败关闭 |
| 配置文件是链接、特殊文件或读取中漂移 | 路径替换、阻塞、TOCTOU | 复用跨平台句柄链安全读取 |
| Secret 版本变化或多个引用别名到同一环境变量 | 未审批凭据轮换、版本身份失真 | 要求精确版本且每个环境变量只定位一个Secret引用；诊断及启动失败关闭 |
| Profile/Fallback 环或过长 | 无限切换、尝试账本越界 | 配置验证拒绝，最多六个 Profile、每个最多五次尝试 |
| 候选已发出 `response_started` 后失败 | 重复文本、Tool Call 或费用语义混乱 | 原失败透传，禁止自动切换 |
| 仅产生 Attempt/Usage 事实后可重试失败 | 已有费用但无模型输出 | 记录失败尝试和 Fallback 审计后允许切换 |
| 配置并发迁移或激活 | 覆盖新配置、同一配置内Profile切换丢失、错误回滚 | 源摘要迁移CAS、旧配置摘要与旧Profile激活CAS、同目录锁、临时文件、fsync、原子替换 |
| 诊断输出泄漏 Key、Header 或异常正文 | 凭据进入日志/工单 | 只输出枚举、标识、摘要和布尔状态 |
| 远端 MCP/Git 共用模型认证装配 | 跨端点凭据污染 | 保持关闭，交由独立受管出口与凭据切片 |

## 8. 结论

0.8.6 应形成一个小而正式的产品配置域：严格配置快照、可重放审计、Profile 解析、Secret
引用、离线诊断、Provider 工厂、安全 Fallback 和内置 stdio App Server 启动装配。配置格式
选择规范 JSON，而不是 JSONC/TOML：当前公共协议、Smoke、Schema 和规范摘要均已使用 JSON，
可以复用严格解码和规范摘要语义，避免新增解析与重写依赖。

远端 MCP Streamable HTTP/OAuth 与公网 Git 认证不与模型 API Key 共享工厂。它们需要独立的
目标身份、受管 DNS/出口、OAuth 生命周期、known-hosts 或 askpass 凭据作用域，以及对应的
失败恢复测试。0.8.6继续拒绝这些配置；正式交付门禁移入0.9.4安全供应链和0.9.5安装/
Dogfooding，不以“接受一个Header”冒充生产完成。
