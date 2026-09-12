---
doc_type: source-research
status: historical
version: 1
code_revision: 16c5838bcb14070eda16da807cfa2d97c0704217
owners:
  - core
modules:
  - context
  - agent
related_adrs:
  - docs/adr/0054-context-planning-and-inspection.md
related_tests:
  - tests/context
supersedes: []
---

> **冻结源码研究**：本资料的参考版本与访问日期冻结于2026-09-07；具体提交、版本和证据位置见正文及[统一研究基线](baselines.md)。结论不随上游分支移动自动更新，Harnessix现行行为以关联ADR和模块设计为准。

# Context 规划、指令与预算源码研究

- 研究日期：2026-09-07
- 适用范围：Harnessix Code 0.6.1
- 证据基线：Codex `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67`、OpenCode `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5`、Claude Code 逆向仓库 `2ca5ddabfed5f220812ea11f029eda03b21bc4c1`

本文只记录冻结提交中可复查的机制和 Harnessix 的独立决策。Claude Code 逆向仓库仅作行为佐证，不作为安全或协议结论的唯一依据。

## 1. 研究问题

0.6.1 聚焦四个问题：

1. Runtime、用户和项目指令如何保持来源与优先级；
2. 模型请求如何同时兼容 OpenAI-compatible 与 Anthropic；
3. 送模前如何预留输出、协议开销和安全余量；
4. 如何持久记录 Context 决策而不复制原始指令正文。

自动 Compaction、Tool Result 裁剪、项目文件发现和 Session Fork 不属于本片。

## 2. Codex

### 2.1 可复查事实

- 基础指令明确规定目录级 `AGENTS.md` 作用域、深层文件优先级，以及直接 system/developer/user 指令高于 `AGENTS.md`。见 [`default.md:17-27`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/protocol/src/prompts/base_instructions/default.md#L17-L27)。
- `ContextManager::for_prompt_annotated` 在生成模型历史前执行规范化；规范化补齐缺失调用结果、移除孤儿结果并按模型能力移除不支持媒体。见 [`history.rs:284-301`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/core/src/context_manager/history.rs#L284-L301) 与 [`history.rs:563-580`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/core/src/context_manager/history.rs#L563-L580)。
- Tool 输出进入活动历史时按策略裁剪，审查历史保留独立表示。见 [`history.rs:250-277`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/core/src/context_manager/history.rs#L250-L277)。
- Token 估算把基础指令与历史 Item 分项计算，源码明确说明这是近似值而非 tokenizer 精确计数。见 [`history.rs:332-359`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/core/src/context_manager/history.rs#L332-L359)。
- 删除最旧 Item 时同步删除调用配对项，避免历史协议失效。见 [`history.rs:362-372`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/core/src/context_manager/history.rs#L362-L372)。

### 2.2 结论

指令、活动模型视图和审查事实需要分层；预算动作不得破坏 Tool Call/Result 配对。近似估算必须带版本和余量，不能伪装成供应商账单 Token。

## 3. OpenCode

### 3.1 可复查事实

- `InstructionContext` 从全局配置目录和项目向上路径发现 `AGENTS.md`，对路径去重；读取缺口进入 unavailable，而不是静默当作空内容。见 [`instruction-context.ts:40-88`](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/instruction-context.ts#L40-L88)。
- 指令渲染保留来源路径；更新时明确旧 ambient instructions 已被替换。见 [`instruction-context.ts:29-38`](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/instruction-context.ts#L29-L38) 与 [`instruction-context.ts:99-101`](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/instruction-context.ts#L99-L101)。
- 内建 System Context 单独生成工作目录、项目根、Git 标志、平台和日期。见 [`builtins.ts:12-42`](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/system-context/builtins.ts#L12-L42)。
- Compaction 在请求前估算 system/messages/tools，总窗口扣除输出或 buffer 后才决定是否压缩；摘要失败或为空时不发布结束事实。见 [`compaction.ts:176-247`](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/session/compaction.ts#L176-L247)。

### 3.2 结论

项目指令发现、动态环境 Context 和压缩是不同职责。来源读取失败应显式表达；Context 选择不能把“没有内容”和“无法读取”混为一谈。

## 4. Claude Code 逆向仓库

### 4.1 可复查行为

- Git 状态有独立字符上限，生成结果明确声明是会话开始时的快照。见 [`context.ts:84-103`](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/context.ts#L84-L103)。
- System Context 和 User Context 分开缓存；用户 Context 包含项目说明文件发现结果。见 [`context.ts:113-188`](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/context.ts#L113-L188)。
- Query Loop 先做 Tool Result 预算，再做微压缩、Context Collapse 和自动压缩；Collapse 是完整历史上的读取时投影。见 [`query.ts:365-467`](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/query.ts#L365-L467)。

### 4.2 结论

动态快照必须标记时效；多级裁剪需要固定顺序。该仓库不能证明 Anthropic 官方内部架构，Harnessix 不复制其实现。

## 5. Harnessix 差异化决策

| 主题 | 0.6.1 决策 |
|---|---|
| 指令优先级 | 固定为 Runtime > User > Project > Workspace > Git > Environment，不接受模型自报优先级 |
| 信任 | `kind`唯一映射到`trust/priority/required`，调用方不能用项目正文伪装 Runtime 指令 |
| 编码 | 统一编码为结构化 JSON 指令包，内容由 JSON 转义，低信任正文不能闭合外层结构 |
| 预算 | `window - reserved_output - provider_overhead - safety_margin`得到输入上限 |
| 估算 | 0.6.1 使用`utf8-bytes/v1`；它是保守、确定性的供应商中立估算，不替代真实 tokenizer |
| 省略 | Runtime/User 为必选；其他 Fragment 按固定优先级尝试装入，省略原因持久记录 |
| 持久化 | Event v10 只保存来源、信任、预算、决策和正文指纹，不保存指令正文副本 |
| Provider | OpenAI-compatible 使用首个 system message；Anthropic 使用顶层 `system` 字段 |
| 溢出 | 历史/工具或必选指令超限时在网络调用前失败，不在尚未实现 Compaction 时隐式删历史 |
| 诊断 | `inspect_context`读取持久检查记录；Trace/Metric 只包含步骤、分项计数和有限枚举 |

`utf8-bytes/v1`按 UTF-8 字节数计 Token，通常高估模型实际 Token。其用途是建立确定性门禁和测试基线；后续可增加经过验证的 tokenizer 实现，但必须保留 estimator 版本并用同一输入重算，不能静默改变历史解释。

## 6. 后续求证

0.6.2 需继续求证并实现项目指令发现失败语义、Workspace/Git freshness、Tool Result 原文 Artifact 与模型视图的绑定。0.6.3 再实现软阈值和 Provider `context_overflow` 触发的自动压缩，并验证摘要替换不修改原始 Event。
