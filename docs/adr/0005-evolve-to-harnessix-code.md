# ADR 0005：将 Harnessix 演进为 Harnessix Code

- 状态：已接受
- 日期：2026-09-02

## 背景

Harnessix 0.1 已完成 Framework-agnostic Agent Action Plane 的基础能力，包括 Action Contract、Policy、Approval、Effect Journal、租约、Worker、幂等、不确定结果对账和 OpenTelemetry。

该能力解决了 Agent 外部副作用的治理问题，但不包含 Agent Loop、模型调用、Context、代码工具、Workspace、Sandbox、Session、客户端协议和 Coding Eval，无法单独构成完整 Coding Agent。

项目的长期目标已经明确为：通过研究 Codex、Claude Code 和 OpenCode，独立设计和实现可作为真实开发工具使用的生产级 Coding Agent，而不是 POC、演示项目或第三方 Agent 框架包装层。

## 决策

1. 顶层产品名称升级为 **Harnessix Code**；仓库、Python 包和 CLI 保留 `Harnessix` / `harnessix`。
2. 自研 Agent Runtime，不使用 LangGraph 作为核心 Agent Loop。
3. 现有 Action Plane 保留为执行治理子系统，继续负责高风险、外部和不确定副作用。
4. 第一版采用本地优先、CLI + Headless App Server、macOS/Linux 的产品范围。
5. 继续遵循 ADR 0001 的 Python-first 决策；只有进程、PTY、Sandbox 或分发需求得到基准数据证明后，才下沉局部 Rust 组件。
6. 开发采用“源码研究—ADR—生产切片—故障测试—评测基线”的闭环，不直接复制参考项目代码。
7. 每个里程碑必须满足统一质量门禁，不允许以 Demo 实现替代正式领域模型和失败语义。

## 结果

### 正向结果

- 项目能够展示完整 Coding Agent 架构，而不只是执行治理能力；
- Action Plane 成为区别于普通 Coding Agent Wrapper 的差异化能力；
- 现有代码、测试、文档、仓库历史和 Python 生态投入得到保留；
- 核心运行语义由 Harnessix 自己拥有，后续客户端和 Provider 可以独立演进。

### 成本与风险

- 项目范围显著扩大，需要严格按里程碑控制边界；
- Agent Runtime、Context 和 Sandbox 会引入新的状态与故障组合；
- Python 在 PTY、单文件分发和低层沙箱上的限制可能需要独立 Sidecar；
- 同时研究三个项目容易停留在阅读阶段，必须用 ADR、测试和可运行切片闭环。

## 被否决方案

### 继续只做 Action Plane

无法满足完整 Coding Agent 的学习和作品集目标。

### 基于 LangGraph 快速组装 Coding Agent

可以更快得到演示，但核心 Agent Loop、状态、重试和流式语义由第三方框架决定，不利于深度理解和展示运行时设计能力。

### 立即整体改写为 Rust

当前没有性能或隔离基准证明整体重写必要，会延迟 Agent 核心语义的验证。保留后续将 Process/Sandbox 下沉为 Rust Sidecar 的路径。
