# ADR-0054：版本化 Context 规划、指令优先级与无正文检查记录

- 状态：Accepted
- 日期：2026-09-07
- 范围：0.6.1

## 背景

0.5 结束时，Agent Runtime 直接把全部完成消息与工具结果交给 Provider。`ModelRequest`没有供应商中立的系统指令字段，也没有上下文窗口、来源、选择或持久诊断契约。继续直接在两个 Provider Adapter 内拼 Prompt 会导致优先级、预算和恢复语义分叉。

源码求证见[Context 规划专项研究](../research/context-planning-and-instructions.md)。

## 决策

### 1. Context Planner 是独立端口

`ContextPlanner.prepare(ContextBuildInput) -> PreparedContext`位于 Agent Runtime 与 Provider 之间。Runtime 提供稳定的 Thread/Turn/Step/Workspace 身份，以及规范化历史和 Tool Definition 的确定性 JSON 文档；Planner 不访问 Provider SDK，也不执行文件或命令。

0.6.1 提供静态`ContextEngine`实现。项目文件自动发现、Git/环境采集由0.6.2的受控 Source 端口完成，不能塞入当前纯规划器。

### 2. Fragment 身份和优先级不可由正文声明

`ContextFragment.kind`唯一决定信任、优先级和必选属性：

| kind | trust | priority | required |
|---|---|---:|---|
| runtime_instruction | runtime | 600 | 是 |
| user_instruction | user | 500 | 是 |
| project_instruction | project | 400 | 否 |
| workspace | external | 300 | 否 |
| git | external | 200 | 否 |
| environment | external | 100 | 否 |

Fragment ID 为`kind + NUL + source + NUL + content`的 SHA-256。重复身份在 Runtime 启动前拒绝。正文按结构化 JSON 转义后进入单一指令包；这能保护结构边界，但不声称消除模型层 Prompt Injection。工具权限仍由 Registry、Policy、Approval 和后续 Sandbox 强制执行。

### 3. 输入预算先扣除不可占用空间

~~~text
available_input
  = context_window
  - reserved_output
  - provider_overhead
  - safety_margin
~~~

0.6.1 的`utf8-bytes/v1`按 UTF-8 字节数估算，优点是确定、无供应商依赖且比常见 BPE 计数保守；代价是可用窗口偏小。估算器名称持久化，不能把结果解释为供应商 Usage。

历史和 Tool Definition 是固定输入。固定输入超限，或 Runtime/User 必选指令无法装入时，返回`context_budget_exceeded`并在发网前结束 Turn。可选 Fragment 按优先级、来源、ID排序，逐个尝试装入；大 Fragment 被省略后，较小的低优先级 Fragment仍可进入，所有决策可检查。

### 4. 模型请求保持供应商中立

`ModelRequest.instructions`承载已经确定的单一指令包：

- OpenAI-compatible Adapter 在历史前加入一个`system` message；
- Anthropic Adapter 写入顶层`system`；
- 历史 Item、Tool Call/Result 配对和 Provider Event 不变。

Adapter 不重新排序 Fragment，不推断 trust，也不读取项目文件。

### 5. ContextPrepared 是 Event v10 的持久事实

每次模型步骤在`PREPARING_CONTEXT`内提交唯一`ContextPrepared`，然后才进入`CALLING_MODEL`。Turn 投影保存`context_inspections`。记录包含：

- Context limits 与可用输入；
- 历史、工具、指令和总估算；
- Fragment ID、kind、source、trust、priority、required 和 disposition；
- 指令包 SHA-256，不包含指令正文。

Session migration 0012 只标记最低 reader 版本，不改写旧事件或快照。旧 v1-v9 Event 原字节保留；新投影写为 v10。

### 6. 失败、取消与恢复

- 预算失败归类为`FailureCategory.BUDGET`；指定检查记录不存在归类为`INPUT`；
- Planner 运行前后均检查 Turn CancelToken；同步 Planner 不拥有后台任务；
- Context 记录提交失败沿用 Session Store 原子提交和 Storage 分类；
- 记录提交后、模型调用前发生故障时，Context 检查事实保留，Turn按现有保守规则失败；0.6.1不自动重发模型请求；
- 检查记录不含正文，因此未来恢复必须重新加载来源并核对指纹，不能从记录伪造原 Prompt。

### 7. 可观测性

每次启用 Planner 的模型步骤产生`harnessix.agent.context` Span。指标仅使用固定维度：

- `harnessix.agent.context.tokens{component}`；
- `harnessix.agent.context.fragments{kind,disposition}`。

正文、source、Fragment ID、Thread/Turn ID 均不进入 Metric 标签。Thread/Turn/Step 仅位于 Trace。

## 取舍

### 不直接使用模型 tokenizer

Provider 适配层尚未暴露稳定 tokenizer 能力，代理平台也可能不等于模型原厂。当前保守估算先保证行为确定；后续新增 estimator，而不是修改 v1 算法。

### 不把完整指令写入 ContextPrepared

完整正文已经来自宿主配置或项目文件，重复写入 Session 会扩大泄漏面和数据库体积。持久摘要足以诊断选择与预算，恢复时再核对真实来源。

### 不在本片自动读取 AGENTS.md

文件发现涉及目录作用域、符号链接、读取缺口、Workspace Revision 和动态更新，必须复用0.5文件安全原语并单独设计失败语义。静态 Fragment 不冒充完整项目指令发现。

## 非目标

- 自动 Compaction 或 reactive overflow recovery；
- Tool Result 裁剪与 Artifact 原文引用；
- 精确 tokenizer 和动态模型能力发现；
- Session Resume/Fork/Archive；
- Prompt Injection 的模型级完全防御；
- Secret Redactor 与 OS Sandbox。
