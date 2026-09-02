# 0.4 Model Runtime 实施计划

- 日期：2026-09-03
- 状态：待实施；0.3 Kernel 已完成本地验收，尚无真实 Provider Adapter
- 依据：ADR 0008、当前 ModelProvider/ProviderEvent 契约与主路线图

## 1. 目标与边界

让同一 Kernel 切换 OpenAI-compatible 与 Anthropic Provider，而不依赖供应商消息对象。不在本阶段开放写工具或绕过审批，不以真实 API 能返回文本作为整个 Coding Agent 完成的证据。

以小切片推进，每次先核对官方文档、当前 SDK 源码/版本和测试能力，不猜测流事件或重试行为。

## 2. 0.4.1：接口求证与首个 Provider

设计交付：

- 选定 SDK/传输方案并记录版本、接口来源与取消/重试语义；
- Provider 配置、能力矩阵、模型标识和 Secret 读取边界；
- 内部 Item 到供应商消息的映射，尤其是 Tool Call/Result 的稳定身份与跨步骤配对；
- 文本流、参数分片、结束事件、Usage 和错误的映射表；
- 超时/输出预算与取消时连接关闭的规则；
- 重试只限“尚未向 Kernel 暴露部分响应或 Tool Call”的安全边界，避免 SDK 隐式重试与 Kernel 控制叠加。

实现交付：

- 首个 OpenAI-compatible Adapter；
- 可注入的测试传输/客户端与脱敏诊断；
- 默认离线的共享 Provider 契约，包括坏 JSON、截断、重复结束、取消和限流；
- 不将 API Key、HTTP Header 或原始 SDK 异常写入 Session/Trace。

## 3. 0.4.2：第二类 Provider 与能力差异

- Anthropic Adapter 接入同一契约；
- 明确 capability 不支持时的行为，不静默丢弃不支持的 Item 或伪造 Usage；
- 保留公开 Reasoning Summary 与私有推理内容的边界；
- 增加缓存、推理 Token 等供应商差异的结构化表示，不能直接改变 Kernel 主循环；
- 两个 Adapter 通过相同的核心 Contract，额外差异由各自测试覆盖。

## 4. 0.4.3：受控真实验证与阶段验收

- 真实 Smoke Test 与默认 CI 分离，凭据仅由运行环境注入；
- 首先验证文本与只读工具闭环，设置请求次数、Token、超时和费用边界；
- 核对实际 Usage、响应中断、取消清理、审批后继续与日志/Trace 内容；
- 提交可复现测试命令、脱敏结果和剩余兼容性限制；
- API 认证、额度或网络不可用时记录为外部阻塞，继续独立的离线契约测试，不将 Mock 通过写成真实平台验证通过。

## 5. 完成条件

主路线图中 0.4 的 Provider、能力、认证、错误/重试、取消、Token/成本和脱敏诊断均有实现与测试证据；两类 Adapter 共享契约通过，至少完成受控真实平台验证后再进入 0.5 Coding Tool Runtime。

当前不需要安装远程中间件。是否引入新 SDK、选择哪些模型及具体成本上限，将在 0.4.1 接口求证后固化，不在本计划中臆测供应商规格。
