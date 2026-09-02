# Harnessix Code 产品章程

## 1. 产品定义

**Harnessix Code** 的目标是成为生产级、本地优先、模型无关的 Coding Agent。

它面向真实软件仓库完成代码理解、计划、修改、命令执行、测试和结果交付，并把模型推理、上下文管理、工具执行、权限审批、会话恢复和副作用治理纳入同一个可观测、可测试的运行时。

仓库名、Python 包名和 CLI 命令继续使用 `Harnessix` / `harnessix`。原有 Framework-agnostic Agent Action Plane 不再作为顶层产品，而是作为 Harnessix Code 的执行治理子系统继续演进。

## 2. 目标用户

第一阶段目标用户是：

- 希望在本地代码仓库中使用可控 Coding Agent 的开发者；
- 需要接入不同模型供应商，又不希望业务绑定单一模型 SDK 的团队；
- 对命令执行、文件写入、网络访问和外部系统副作用有审计与审批要求的工程团队；
- 需要研究和扩展 Agent Loop、Context、Tool、Sandbox、MCP、Skills 的 Agent 工程师。

## 3. 第一版产品形态

第一版采用以下约束：

- 本地优先，支持 macOS 和 Linux；
- CLI 优先，同时提供无界面的 App Server；
- 单个 Workspace 对应一个明确的文件系统边界；
- 支持交互式会话和一次性 Headless 任务；
- 支持 OpenAI-compatible 与 Anthropic 两类 Provider；
- 支持读取、搜索、补丁修改、Shell、Git 和测试闭环；
- 支持持久会话、恢复、取消、审批和上下文压缩；
- 支持 MCP、项目指令和 Skills；
- 高风险外部副作用由 Harnessix Action Plane 治理。

IDE、Web、多租户云平台和大规模分布式调度不属于第一版发布范围，但核心协议不得阻断后续客户端扩展。

## 4. 核心价值

### 4.1 完整 Coding Agent 闭环

Harnessix Code 必须能够独立完成：

```text
理解请求 → 探索仓库 → 制定或调整计划 → 调用工具
→ 修改代码 → 运行验证 → 根据结果继续迭代 → 交付变更摘要
```

核心 Agent Loop 不依赖 LangGraph 等通用编排框架，避免把关键运行语义交给第三方框架。

### 4.2 生产级执行安全

对文件写入、Shell、网络访问和外部系统操作进行分级处理：

- 只读操作走低开销执行路径，但仍保留结构化事件；
- Workspace 内写操作必须可生成 Diff、可审批、可取消；
- Shell 具备超时、输出限制、进程树终止和资源清理；
- 外部不可逆操作进入 Action Plane，使用策略、审批、幂等和 Effect Journal；
- 不确定副作用不得盲目重试，必须进入显式对账流程。

### 4.3 可恢复、可解释、可评测

- Thread、Turn、Item 和运行事件可持久化；
- 进程退出后能够恢复到明确状态；
- Context 的来源、裁剪和压缩结果可检查；
- Tool Call、审批、执行结果和最终代码 Diff 可追踪；
- 通过确定性 Fake Provider、Transcript Replay 和真实仓库 Eval 防止回归。

### 4.4 Provider 与客户端解耦

- Agent Runtime 面向统一模型事件，而不是供应商原始响应；
- App Server 面向版本化协议，而不是绑定某个 CLI/TUI；
- Provider、Tool、Sandbox、Session Store 和扩展机制均通过稳定端口接入。

## 5. 明确不做

第一版不做以下事情：

- 不复制通用工作流引擎；
- 不以复杂多 Agent 拓扑代替可靠的单 Agent Loop；
- 不把 RAG、向量数据库或长期记忆作为 Coding Agent 的默认前提；
- 不追求一次支持所有模型、IDE 和操作系统；
- 不把 Prompt 中的文字约束冒充真正的权限隔离；
- 不承诺任意外部系统上的 Exactly Once；
- 不直接复制 Codex、OpenCode 或反编译 Claude Code 的实现代码。

## 6. 产品成功标准

Harnessix Code 1.0 必须满足：

1. 能在非示例仓库中完成“定位问题—修改—测试—交付 Diff”的闭环；
2. 支持交互式与 Headless 两种运行方式；
3. 支持至少两个 Provider 家族，并通过统一契约测试；
4. 支持会话恢复、用户取消、Tool 超时和上下文压缩；
5. 具备 Workspace 边界、命令审批、网络策略和至少一种隔离执行后端；
6. 支持 MCP、项目指令和 Skills；
7. 有稳定、版本化的 App Server 协议；
8. 有单元、契约、集成、端到端、故障注入和真实仓库 Eval；
9. 提供 macOS/Linux 安装、升级、配置、诊断和安全文档；
10. 发布可复现的质量、成本、延迟和任务成功率基线。

## 7. 简历项目表达

项目应能够被准确描述为：

> 独立设计并实现生产级 Coding Agent Runtime，包含持久 Agent Loop、Provider 抽象、上下文压缩、代码工具、进程与沙箱、权限审批、MCP/Skills、双向事件协议和自动化评测；通过 Action Plane 进一步解决外部副作用的幂等、恢复与对账问题。

这一定义同时体现 Coding Agent 的完整性和 Harnessix 独有的执行治理能力。
