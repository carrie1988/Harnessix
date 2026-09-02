# Tool Runtime 研究与执行契约

## 1. 研究基线

见[源码研究基线](baselines.md)。本主题只研究通用 Tool Runtime；Patch、Shell 和 Sandbox 的平台细节在 0.5/0.7 继续形成专项 ADR。

## 2. Tool 不是普通函数

生产级 Tool 必须同时定义：

- 面向模型的名称、描述和输入 Schema；
- 面向 Runtime 的版本、输出 Schema 和错误类型；
- 只读/本地写/外部写等 Effect Class；
- 风险、权限、审批和 Sandbox 需求；
- 并发、互斥、超时、取消和结果大小；
- 幂等、重试和对账能力；
- 可观测性与 Secret 处理。

模型看到的 JSON Schema 只是契约的一部分，不能决定执行权限。

## 3. 参考实现事实

### 3.1 Codex

**事实**

- ToolRouter 将模型输出解析为统一 ToolCall，再交给 Registry；
- Registry 拒绝重复注册，未知工具返回模型可见错误；
- Payload 类型与 Tool 类型不匹配属于 Runtime 错误；
- Pre Hook 可以阻止或改写请求，Handler 执行后再运行 Post Hook；
- Post Hook 拒绝不能撤销已经发生的效果；
- Registry 记录工具是否支持并行；
- 模型 Item 在 Tool Future 调度前进入历史，工具结果再回到统一生命周期。

**推断**

Hook 适合做扩展和审计，但真正的权限必须在效果发生前完成；事后 Hook 不是回滚机制。

### 3.2 OpenCode

**事实**

- Tool Definition 同时持有输入/输出 Schema、执行 Context 和结构化 ToolFailure；
- Registry 负责物化、注册、未知工具处理和输出边界；
- 被 Permission 禁用的工具不会出现在模型可见 Definitions；
- Bash 有默认/最大超时和输出捕获上限，并明确说明 Host Shell 拥有当前用户的文件、进程和网络权限；
- Read Filesystem 对行数、文本字节、媒体大小和长行设置上限；
- 路径使用 real path 与 location containment 检查；
- Edit 在批准后检测文件是否发生变化，避免审批后基于陈旧内容覆盖。

**推断**

“不向模型广告”能减少误调用，但不能代替执行时鉴权。Bash 参数中的路径分析仍是启发式，强边界必须由 Sandbox 和 OS 权限提供。

### 3.3 Claude Code 逆向仓库

**事实，仅作行为佐证**

- Tool 接口可见输入/输出 Schema、并发安全、只读、破坏性、可中断、开放世界、MCP、延迟加载和结果大小元数据；
- 部分安全相关默认值采用保守值，例如未声明时不视为只读或并发安全；
- Query Loop 在失败与取消时尽量保持 Tool Use/Result 配对。

## 4. Harnessix Tool Contract

### 4.1 ToolDefinition

~~~text
identity:
  name, version, source
model_contract:
  description, input_schema
runtime_contract:
  output_schema, timeout, max_output, concurrency
effect_contract:
  effect_class, risk_level, idempotency, reconciliation
security_contract:
  permissions, sandbox_profile, network_profile, secret_refs
~~~

Effect Class 首版复用并扩展 Action Plane 语义：

| 类型 | 示例 | 默认恢复 |
|---|---|---|
| PURE | 参数格式化 | 可重算 |
| READ_ONLY | read_file、grep | 显式安全分类后可重试 |
| LOCAL_WRITE | apply_patch | 先对比 Workspace 证据 |
| IDEMPOTENT_EXTERNAL_WRITE | 带业务幂等键的 API | 复用键或对账 |
| NON_IDEMPOTENT_EXTERNAL_WRITE | 无幂等能力的外部操作 | 失败不自动重放 |

### 4.2 ToolCall

ToolCall 持久字段：

- call_id、thread_id、turn_id；
- tool name/version/source；
- 原始参数文本摘要与规范化参数；
- cwd、workspace revision、environment digest；
- policy version、approval fingerprint；
- effect class、risk、idempotency key；
- trace/causation id。

### 4.3 ToolResult

统一结果：

~~~text
status: succeeded | failed | cancelled | unknown
model_content: 有界、脱敏、适合回送模型
structured_output: 通过输出 Schema 校验
artifact_refs: 完整 stdout、Diff 或大文件的受控引用
error: taxonomy + retryable
effect_evidence: pre/post hash、exit code、action_id 等
metrics: duration、bytes、truncated
~~~

Tool 抛出的预期业务失败转为模型可见 ToolResult；编程错误、Schema 破坏或 Runtime 缺陷导致 Step 失败，不伪装成普通工具输出。

## 5. 执行生命周期

~~~text
RECEIVED
  → VALIDATED
  → AUTHORIZED
  → APPROVED（可选）
  → SCHEDULED
  → RUNNING
  → SUCCEEDED | FAILED | CANCELLED | UNKNOWN
~~~

顺序约束：

1. 使用 Runtime Registry 解析稳定版本；
2. 在执行前完成 Schema 校验和参数规范化；
3. 用规范化参数计算权限资源和审批指纹；
4. ToolCall 持久化后才调度效果；
5. 再次校验审批绑定与 Workspace Revision；
6. 执行时只注入声明的 Secret 和能力；
7. 输出先做大小限制、脱敏和 Artifact 落盘；
8. ToolResult 持久化后才能进入下一次模型请求。

## 6. 并发与取消

- PURE/READ_ONLY 只有在 Definition 显式声明 `concurrency_safe` 时并发；
- LOCAL_WRITE 默认按 Workspace 串行；
- Shell 默认串行，未来可按隔离 Workspace 放宽；
- 外部 Action 由 Action Plane 的 Worker/Lease 语义管理；
- 每次调用拥有子 Cancel Token；
- Process Tool 必须终止进程组并等待清理；
- 取消后返回 CANCELLED；若效果已提交但结果未知，必须返回 UNKNOWN。

## 7. 输出与 Context 边界

- Tool Runtime 负责产生完整 Artifact；
- Context Engine 决定本轮向模型暴露多少；
- Session 保存有界 model_content 和 Artifact 元数据；
- 日志只记录摘要、大小、退出状态和脱敏错误；
- 截断必须显式标识，不能让模型误以为读取了完整内容。

## 8. 失败语义和测试

必须覆盖：

- 重复工具名和版本冲突；
- 未知、过期、被禁用工具；
- 参数 JSON 分段、非法 JSON 和 Schema 错误；
- ToolResult 输出 Schema 错误；
- Pre Hook 拒绝、Handler 失败、Post Hook 失败；
- 超时、取消、进程树和超大输出；
- Tool Call 已持久化但未执行时崩溃；
- 本地写效果已发生但 Result 未提交；
- 外部写结果丢失进入 UNKNOWN 并对账；
- 审批后参数、cwd、环境或 Workspace Revision 变化导致失效。

## 9. Harnessix 差异化

Harnessix 不只提供 Tool Registry，而是把 Tool Call 映射到现有 Action Plane：

- 普通本地只读/写走低开销 Tool Runtime；
- 高风险或外部效果升级为 Durable Action；
- Approval 与效果指纹统一；
- 非幂等写发生不确定结果时显式 UNKNOWN；
- Reconcile 只观察外部结果，不重做原效果。

这使 Coding Agent 的“工具调用”与生产系统中的副作用可靠性形成同一条治理链。

## 10. 源码索引

- Codex：[router.rs](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/core/src/tools/router.rs)、[registry.rs](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/core/src/tools/registry.rs)
- OpenCode：[tool.ts](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/tool/tool.ts)、[registry.ts](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/tool/registry.ts)、[bash.ts](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/tool/bash.ts)、[read-filesystem.ts](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/tool/read-filesystem.ts)
- Claude 逆向仓库：[Tool.ts](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/Tool.ts)、[tools.ts](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/tools.ts)
