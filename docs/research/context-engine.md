# Context Engine 研究与预算模型

## 1. 研究基线

见[源码研究基线](baselines.md)。Context Engine 的目标不是“把更多内容塞给模型”，而是在有限窗口内保留当前任务所需证据，并使裁剪和压缩可解释、可恢复。

## 2. 参考实现事实

### 2.1 Codex

**事实**

- Context Manager 同时维护送模历史和受界限保护的原始证据；
- Prompt 构造会规范化历史，使每个 Call 有对应 Output，移除孤儿 Output，并处理供应商不支持的媒体；
- Tool Result 在写入历史时执行按 Item 类型的截断策略；
- Token 估算用于决定压缩，但实现明确是近似值；
- 删除旧记录时成对删除 Function Call/Output，避免协议损坏；
- Compaction 会用摘要替换送模历史，但保留独立审查 Transcript；
- 手动/轮前压缩与轮中自动压缩对初始 Context 的处理不同；
- 压缩替换同时更新内存和持久历史，保证两者一致。

**推断**

“事实历史”和“当前模型视图”必须分离。Compaction 不是删除事件，而是生成新的上下文派生物。

### 2.2 OpenCode

**事实**

- V2 Compaction 定义 buffer、保留历史、Tool 输出字符上限和摘要输出上限；
- 历史选择与 Tool 内容序列化是独立步骤；
- 溢出后可以启动 reactive compaction；
- Compaction 有 Started/Ended 事件，摘要和 recent 内容进入显式消息；
- 当前 Runner 在每次 Provider 请求前检查是否需要压缩。

**推断**

固定默认值适合启动，但长期需要按 Provider 能力和 Tool 输出类型配置；压缩结果应版本化，否则切换摘要策略后难以解释历史。

### 2.3 Claude Code 逆向仓库

**事实，仅作行为佐证**

- 用户/系统 Context 在会话中存在缓存；
- Git 状态等环境片段有大小限制；
- Query Loop 在请求前依次进行 Tool 输出裁剪、微压缩和 Context Collapse；
- Prompt 过长时有 reactive compact/collapse；
- QueryEngine 记录 compact boundary 并释放旧消息引用。

## 3. Harnessix Context Pipeline

每次模型请求按以下顺序构建：

~~~text
1. Runtime Base Instructions
2. 用户级可信配置
3. 项目指令（带来源与信任级别）
4. Agent / Skill 选定指令
5. Tool Definitions（经过 Permission 预筛选）
6. Workspace / Git / 环境摘要
7. Thread 历史的规范化模型视图
8. 当前 Turn 输入和已完成 Tool Result
9. 输出格式约束
~~~

后层不能静默提升自身优先级。仓库中的说明文件属于不可信项目数据，不可覆盖 Runtime 安全规则或要求泄漏 Secret。

## 4. Context Fragment

所有片段使用统一元数据：

| 字段 | 说明 |
|---|---|
| fragment_id | 稳定 ID |
| kind | system、user、project、workspace、history、tool 等 |
| source | 文件、生成器或事件引用 |
| trust | runtime、user、project、external |
| priority | 预算不足时的相对保留级别 |
| token_estimate | 当前 tokenizer 的估算 |
| freshness | 生成时间与 Workspace Revision |
| redaction | 已应用的脱敏策略版本 |
| content_ref | 内联内容或 Artifact 引用 |

Context Inspect 只展示来源、预算、截断和摘要关系；默认不展示 Secret 或私有 Provider 数据。

## 5. Token Budget

总窗口分配：

~~~text
context_window
  - reserved_output
  - provider_overhead
  - safety_margin
  = available_input
~~~

Input 预算优先级：

1. Runtime 安全和协议约束；
2. 当前用户意图；
3. 未结算 Tool Call/Result 配对；
4. 当前任务约束和最新计划；
5. 近期代码证据；
6. Compaction Summary；
7. 较旧对话和低价值输出。

Token 估算必须记录 tokenizer/model 版本。估算不足不能以删除系统约束作为补救。

## 6. Tool Result 裁剪

三层表示：

1. **完整 Artifact**：受控文件，包含完整 stdout、搜索结果或 Diff；
2. **Session Result**：结构化元数据、摘要、首尾片段和 Artifact 引用；
3. **Model View**：按当前请求预算生成的最小证据。

裁剪策略按类型处理：

- 文本：保留首尾、匹配行和明确省略计数；
- 搜索：优先高相关文件，保留总命中数；
- 测试：优先失败摘要、首个根因和最终状态；
- Diff：优先修改摘要与受影响文件，完整 Patch 放 Artifact；
- 二进制：只提供元数据和安全预览；
- Secret 命中：先脱敏，再进入任意层。

## 7. Compaction

Compaction 触发：

- 轮前预测超过软阈值；
- Provider 返回 context_overflow；
- 用户显式请求；
- 长任务达到历史成本阈值。

输出必须包含：

- 当前目标和验收条件；
- 已完成工作与验证结果；
- 未完成计划；
- 已修改文件和 Workspace 状态；
- 关键约束、审批和权限；
- Tool 效果/Action ID；
- 不确定结果和恢复要求；
- 被保留的最近 Items；
- summary schema、prompt 和 model 版本。

Compaction 创建独立 Item 和新的 Model View，不改写原始 AgentEvent。摘要失败时保留旧视图并返回结构化错误。

## 8. 失败语义

| 场景 | 处理 |
|---|---|
| Token 估算偏低 | 捕获 context_overflow，最多触发一次 reactive compaction |
| 摘要 Provider 失败 | 不替换当前视图，可有限重试或中断 |
| Tool Call/Result 被预算切开 | 规范化器成对保留或成对移除 |
| Artifact 已过期 | Context 显式标记 unavailable，不伪造内容 |
| Workspace 已变化 | 旧 Fragment 标记 stale，按需刷新 |
| Provider 切换 | 从语义 Items 重建，不重用供应商原始消息 |
| 摘要遗漏关键约束 | Eval 失败；保留事件支持重新压缩 |

## 9. 测试与 Eval

- 预算边界与 tokenizer 随机测试；
- Call/Result 配对属性测试；
- 超长 Tool 输出和长行；
- 轮前和 reactive compaction；
- 摘要前后约束保持 Eval；
- Provider 切换后的历史规范化；
- Resume/Fork 后 Context 来源一致；
- Secret canary 不出现在 Prompt、Transcript、日志和 Artifact 元数据；
- 相同 Event Transcript 产生确定性的 Fragment 选择结果。

## 10. 待验证假设

- Python Token 估算性能是否足够；
- 摘要是否必须使用当前主模型；
- Artifact 的默认保留期；
- Repo Map/索引是否能在真实 Eval 中显著提升质量。没有数据前不引入向量数据库。

## 11. 源码索引

- Codex：[history.rs](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/core/src/context_manager/history.rs)、[compact.rs](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/core/src/compact.rs)
- OpenCode：[compaction.ts](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/session/compaction.ts)
- Claude 逆向仓库：[context.ts](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/context.ts)、[query.ts](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/query.ts)
