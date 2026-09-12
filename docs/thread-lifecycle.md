---
doc_type: change-design
status: historical
version: 1
code_revision: 1cb15efdd154f16e0f894e70998d26670ca60d04
owners:
  - core
modules:
  - agent
  - session
  - context
  - artifacts
related_adrs:
  - docs/adr/0060-thread-lifecycle-and-authority-free-forks.md
related_tests:
  - tests/context/test_thread_lifecycle.py
  - tests/context/test_thread_lifecycle_recovery.py
supersedes: []
---

> **历史专题设计**：本文保留0.6.4 Thread Resume、Fork与Archive的增量设计，不再作为当前实现的唯一事实源。当前领域入口、生命周期状态和恢复语义见[Agent Runtime模块](modules/agent.md)，事务、迁移与重建边界见[Session模块](modules/session.md)，历史物化及Artifact继承见[Context模块](modules/context.md)与[Artifacts模块](modules/artifacts.md)。

# Thread Resume、Fork 与 Archive 详细设计

- 版本：0.6.4
- 更新日期：2026-09-08
- 状态：实现、本地完整验收及远端CI完成

## 1. 目标与边界

本设计为持久Coding Agent提供同身份恢复、安全分支和只读归档，并保证Resume/Fork不重复已完成副作用。实现不包含Unarchive、删除、跨Workspace Fork、活跃Turn Fork、文件系统回退或跨节点分布式锁。

## 2. 模块

| 模块 | 职责 |
|---|---|
| `agent/models.py` | `ThreadForkSnapshot v1`、Artifact所有者、Archive记录及v16事件 |
| `agent/lifecycle.py` | 来源边界选择、规范摘要、决定收集、Artifact所有者继承和确定性重建 |
| `agent/runtime.py` | Resume/Fork/Archive公开运行时入口与发网前Artifact验证 |
| `session/ports.py` | 跨Thread来源CAS创建契约 |
| `session/sqlite.py` | 单事务Fork、归档Event/Projection提交和带来源前缀的Rebuild |
| `context/tool_result_view.py` | Fork历史并入模型历史、跨代Artifact所有者解析 |
| `context/compaction_window.py` | 暴露活动窗口背后的原始模型历史边界 |

## 3. 领域契约

### 3.1 Resume

`resume_thread(thread_id)`返回同一Thread的值副本。Runtime打开时已经扫描活跃Turn：普通中间状态收敛为`interrupted`，等待审批和等待Action保持原状态。Resume本身不新增Event、不恢复Provider流、不执行工具。

### 3.2 Fork

`fork_thread(source_thread_id, request_id, through_turn_id=None)`：

- `request_id`为1至256字符；
- 默认边界是最新终结Turn；空Thread允许产生空历史Fork；
- 显式边界按Turn包含，边界后历史不继承；
- 来源必须未归档且没有活跃Turn；
- 子Thread ID为来源UUID命名空间下的UUIDv5；
- 首事件时间取来源投影更新时间，使同一未变化来源上的重试载荷完全一致。

`ThreadForkSnapshot`包含：

- 来源Thread、sequence、投影摘要和边界Turn；
- 可选来源Compaction窗口；
- 原始历史与模型视图摘要；
- 冻结的Tool Result视图策略和每结果决定；
- 每个Artifact引用的真实所有者Thread；
- 固定`authority=none`。

Fork Item只允许完成的User/Assistant/Tool Call/Tool Result，调用必须完整配对。它们参与后续模型历史和Compaction，但不出现在子Thread的Turn集合，不会进入`pending_calls`、审批查找、Action Context或Tool执行。

### 3.3 Archive

`archive_thread(thread_id, reason=None)`在无活跃Turn时追加`thread_archived`。投影保存事件序号、UTC时间和可选原因。归档后Event Reducer拒绝全部Turn级变更；读取、事件游标、Replay和Rebuild保持可用。

## 4. Fork事务

```text
Runtime锁定来源Thread
  → 读取来源投影
  → 选择终结Turn边界/活动Compaction原历史
  → 冻结Tool Result模型视图
  → 验证全部Artifact正文、TTL、用途、覆盖和当前Workspace scope
  → 构造确定性子ID、事件ID和快照
  → SessionStore BEGIN IMMEDIATE
      → 已存在目标：校验同一事件并幂等返回
      → 读取来源并校验sequence/Workspace/投影摘要
      → 纯函数重建整个Fork快照
      → 写目标首Event
      → 写目标Projection
    COMMIT
```

SQLite在`session.fork.after_source`、`session.after_events`、`session.after_projection`退出时回滚；`session.after_commit`退出时目标完整存在。恢复不会调用Provider。

## 5. Artifact继承

Fork不复制Artifact表记录。模型历史准备为每个引用解析`owner_thread_id`：来源本地Artifact指向直接父Thread，已继承Artifact继续指向原祖先。验证器按真实所有者读取manifest和正文，同时使用当前子Thread运行时提供的Workspace scope。Workspace不一致、正文损坏、TTL到期或覆盖证明失败均在Provider请求前终止当前操作。

归档父Thread不会删除Artifact或事件，因此不破坏已存在分支的审计读取；Artifact自身TTL仍独立生效。

## 6. 失败、取消与恢复

| 窗口 | 行为 |
|---|---|
| Fork准备/Artifact验证取消 | 不写子Thread；异步验证被排空 |
| 来源在提交前变化 | 来源CAS失败，不创建目标 |
| 目标首Event或Projection前退出 | SQLite回滚 |
| COMMIT后响应前退出 | 确定性request重试返回同一子Thread |
| Archive Event/Projection前退出 | SQLite回滚，Thread仍可写 |
| Archive COMMIT后退出 | Thread完整只读，重复同原因归档幂等 |
| Rebuild Fork | 重放来源事件到冻结sequence，再验证快照并重建子投影 |

## 7. 安全边界

- `authority=none`是结构化契约，不依赖Prompt约定；
- 不继承Approval Item、审批决定的可消费位置、活跃调用、Action租约或Runtime任务；
- 历史Tool Call参数仅作为模型可见事实，不能直接送入执行端口；
- 不改变Workspace，不能用Fork绕过路径或Artifact scope；
- Metric只使用固定`action/outcome`标签；Thread、Turn、路径、正文、摘要和Artifact ID不进入Metric标签。

## 8. 持久化与兼容

- Agent Event/Thread：v16；
- 新Schema：`agent-event-v16`、`agent-thread-v16`、`thread-fork-v1`、`thread-archive-v1`；
- Session migration18校验和：`4cbe8c146e4ed71f021e9be45b63da3b5b691115307412dd61e4ffcb277a2f9c`；
- migration18不改写v1-v15 Event、Projection或Artifact；
- 真实v15 wheel升级保持旧字节不变，v15 reader对migration18失败关闭。

## 9. 可观测性

- Counter：`harnessix.agent.thread.lifecycle{action,outcome}`；
- Histogram：`harnessix.agent.thread.fork.inherited_items`；
- action仅为`resume/fork/archive`，outcome仅为`completed/idempotent/rejected`；
- 失败同时保留结构化`KernelError`并计入低基数`rejected`，Metric不携带业务标识。

## 10. 验收矩阵

1. Resume同一身份且零Provider请求；
2. Fork最新和指定终结Turn，后续历史被排除；
3. 继承Tool Call/Result但不再次执行来源工具；
4. 跨代Artifact按真实父Thread验证；
5. request幂等与来源变化冲突；
6. Archive只读、幂等、冲突及Replay/Rebuild；
7. Fork/Archive七个真实进程退出切点；
8. v15→v16独立wheel升级与旧reader拒绝；
9. 全量严格回归、Schema生成和静态检查。

本地严格全量结果为2914 passed、2 skipped；两个skip仅因未配置`HARNESSIX_TEST_POSTGRES_URL`。Ruff格式与规则、Mypy严格检查164个源文件均通过。Schema连续生成两次聚合SHA256均为`e9e54ad0afd92c0d9de41477d32e2c52a43cdc0ec0b2e38bd2764c7a59d0004a`。

实现提交`24e0899`通过[CI 34188329001](https://github.com/carrie1988/Harnessix/actions/runs/34188329001)的Python 3.12、Python 3.13、macOS Coding Tools与PostgreSQL四项任务。
