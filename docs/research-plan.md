# 主流 Coding Agent 源码研究计划

## 1. 研究目标

研究 Codex、OpenCode 和 Claude Code 的目的不是复制功能列表，而是理解它们在以下问题上的边界、数据模型、不变量和工程权衡，并为 Harnessix Code 的 ADR 提供证据。

本地参考源码：

| 项目 | 建议目录 | 主要价值 | 使用限制 |
|---|---|---|---|
| Codex | `${HARNESSIX_RESEARCH_ROOT}/codex` | 完整运行时分层、Rust 工程、App Server、Sandbox | 代码规模大，需要按调用链阅读 |
| OpenCode | `${HARNESSIX_RESEARCH_ROOT}/opencode` | Session、Provider、Tool、TUI/Server 边界清晰 | Monorepo 变化快，需要记录研究提交 |
| Claude Code | `${HARNESSIX_RESEARCH_ROOT}/claude-code-source-code` | Query、Tool、Context、交互行为参考 | 反编译且缺失内部模块，不能作为完整源码基准 |

`HARNESSIX_RESEARCH_ROOT` 只表示本地研究目录，不进入 Harnessix 运行时配置、发布包或测试依赖。

研究文档必须记录具体提交号。研究期间不修改三个参考仓库中的源码，也不把参考实现代码复制到 Harnessix。

## 2. 研究方法

采用“按主题横向对比”，不采用“逐仓库从头读到尾”。每个主题必须回答：

1. 用户可观察行为是什么；
2. 入口、核心调用链和关键状态在哪里；
3. 持久化对象和生命周期是什么；
4. 正常、取消、超时、崩溃分别如何处理；
5. 安全边界和信任边界在哪里；
6. 三个项目方案的共同点和差异是什么；
7. Harnessix Code 选择什么，为什么；
8. 该选择需要哪些测试和运行时不变量。

只描述“某文件做了什么”不算完成研究。研究结论必须最终进入 ADR、架构文档、测试设计或明确的拒绝决策。

## 3. 主题与阅读顺序

| 顺序 | 主题 | Codex 重点 | OpenCode 重点 | Claude Code 重点 | 预期产出 |
|---:|---|---|---|---|---|
| 1 | Agent Loop | `core`、`codex_thread`、事件映射 | `session/processor`、`session/prompt` | `query.ts`、`QueryEngine.ts` | Loop 状态机 ADR |
| 2 | 会话模型 | `thread-store`、`state`、`rollout` | `session/schema`、`message-v2` | `history.ts`、`Task.ts` | Thread/Turn/Item 模型 |
| 3 | 流式协议 | `app-server`、`app-server-protocol` | `server/event`、SDK | AsyncGenerator 消息 | Protocol v1 草案 |
| 4 | 模型抽象 | `model-provider`、Responses client | `provider`、`session/llm` | Query 参数和流事件 | Provider Contract |
| 5 | 工具系统 | `tools`、`function_tool` | `tool/registry`、`session/tools` | `Tool.ts`、`tools.ts` | Tool Contract |
| 6 | 代码修改 | `apply-patch`、文件系统模块 | `tool/edit`、`tool/apply_patch` | Edit/Write 工具 | Patch 事务设计 |
| 7 | Shell/进程 | `exec`、`shell-command` | `tool/shell` | Bash/任务执行工具 | Process Runtime ADR |
| 8 | Context | `context`、`context_manager`、`compact` | `session/compaction`、`instruction` | `context.ts`、自动压缩 | Context Engine ADR |
| 9 | 权限与沙箱 | `execpolicy`、`sandboxing`、平台沙箱 | `permission`、外部目录规则 | 权限与审批行为 | Threat Model |
| 10 | 扩展机制 | MCP、Skills、Hooks、Plugins | MCP、Plugin、Skill | Commands、Hooks、MCP | Extension Contract |
| 11 | 客户端产品 | TUI、App Server Client | TUI、App、Desktop、Server | Ink REPL | 客户端边界 ADR |
| 12 | Subagent | Agent Registry、通信与协作 | Agent、Task 工具 | Task/Coordinator 线索 | Subagent 设计边界 |
| 13 | 可观测性 | OTel、Rollout Trace、Analytics | 日志、事件、统计 | Cost/Telemetry | 可观测性规范 |
| 14 | 测试与评测 | 测试客户端、协议和核心测试 | Core/Tool/Protocol 测试 | 可恢复部分测试线索 | 测试金字塔与 Eval 方案 |

## 4. 每个主题的交付模板

每份研究文档使用以下结构：

```text
# 主题名称

## 研究基线
- Codex commit:
- OpenCode commit:
- Claude Code version/commit:

## 用户行为
## Codex 调用链
## OpenCode 调用链
## Claude Code 调用链
## 共同机制
## 关键差异及原因
## 失败语义
## 安全边界
## Harnessix Code 决策建议
## 待验证假设
## 对应测试
## 源码索引
```

## 5. 研究完成门槛

一个主题只有同时满足以下条件才算完成：

- 调用链已由源码验证，不依赖目录名臆测；
- 至少记录一个正常流程和两个失败流程；
- 明确区分事实、推断和 Harnessix 决策；
- 产出至少一个 ADR、协议草案、状态图或测试设计；
- 结论经过一个最小实验或现有测试验证；
- 文档使用简体中文，并链接到稳定的源码文件和提交号。

## 6. 与开发迭代的关系

研究和开发不是两个完全串行阶段。采用以下节奏：

```text
研究一个主题 → 写 ADR → 做 Runtime Spike → 删除 Spike 或转为正式实现
→ 补契约与故障测试 → 更新架构文档 → 进入下一个主题
```

Spike 只用于验证未知问题，不直接作为生产实现合并。正式实现必须重新按领域边界、错误模型、可观测性和测试门禁完成。
