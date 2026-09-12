---
doc_type: source-research
status: historical
version: 1
code_revision: 24e08998146aacbeb8c4dd8238e5ee3dca20f081
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
  - tests/context
supersedes: []
---

> **冻结源码研究**：本资料的参考版本与访问日期冻结于2026-09-08；具体提交、版本和证据位置见正文及[统一研究基线](baselines.md)。结论不随上游分支移动自动更新，Harnessix现行行为以关联ADR和模块设计为准。

# Thread Resume、Fork 与 Archive 源码研究

- 更新日期：2026-09-08
- 研究基线：Codex `a0dcfe2`、OpenCode `69c172e`、Claude Code Source `2ca5dda`
- 研究范围：本地持久会话恢复、会话分支、归档、历史边界与副作用继承

## 1. 研究问题

0.6.4需要回答以下问题：

1. Resume是重放旧副作用，还是重新附着到已持久化状态；
2. Fork复制哪些历史，如何冻结边界并证明来源未变化；
3. 历史事实、Artifact访问与执行授权能否同时继承；
4. Archive如何阻止新写入，同时保留审计和重放能力；
5. 并发、取消或进程退出发生在跨Thread创建中间时，如何避免半个分支。

## 2. Codex证据

### 2.1 Resume与活跃写者

`codex-rs/app-server-protocol/src/protocol/v2/thread.rs`的`ThreadResumeParams`区分按`thread_id`、内存历史或路径恢复，并明确运行中Thread采用重新加入语义。`codex-rs/thread-store/src/local/live_writer.rs`在恢复前取得Thread写锁、拒绝重复Recorder，再用已有rollout路径重开持久写者。

可复用结论：Resume不应复制历史或创建新身份；单一写者与恢复来源一致性必须先于后续写入。

### 2.2 Fork边界与来源保留

`ThreadForkParams`支持最新位置、包含指定Turn和排除指定Turn三种边界，并拒绝把进行中Turn作为包含边界。`codex-rs/thread-store/src/local/paginated_fork.rs`先保留来源生命周期、持久化来源、物化索引，再冻结`history_base`和有界`model_context`；来源保留一直持续到子引用持久化完成。

可复用结论：Fork必须绑定稳定边界、来源版本和有界模型历史；读取来源与创建子Thread之间不能存在未检测的来源漂移。

### 2.3 Archive

`codex-rs/thread-store/src/local/archive_thread.rs`按稳定顺序取得生命周期锁和写者锁，存在活跃Recorder时拒绝归档。文件移动或元数据更新失败时尝试回滚已移动rollout。读取接口默认排除归档Thread，恢复前需显式解除归档。

可复用结论：Archive不是删除；必须拒绝活跃写入，并保持读取、审计和失败原子性。

## 3. OpenCode证据

`packages/opencode/src/session/session.ts`的`Session.fork`创建新Session，按边界复制消息和Part，重建消息父子ID，并同步Compaction尾部引用。归档由`time_archived`表达，列表默认排除归档记录。`packages/opencode/src/session/revert.ts`在工作区快照恢复和消息清理前调用`assertNotBusy`。

可复用结论：分支需要独立身份和可重建的消息配对；归档/回退不能与活跃执行并发。

风险：直接复制完整消息对象容易把供应商字段、执行身份或授权元数据一并复制。Harnessix不采用“复制后默认可信”的策略。

## 4. Claude Code Source证据

`src/utils/sessionRestore.ts`恢复时默认复用原Session ID，`--fork-session`保留新Session ID；Fork不会接管来源Worktree所有权。`src/commands/branch/branch.ts`为分支创建新Session ID，复制主对话，写入`forkedFrom`来源，并迁移Tool Result内容替换记录。`src/cli/print.ts`支持在指定消息位置截断恢复；文件回退由独立Checkpoint路径处理。

可复用结论：Resume与Fork身份语义必须分离；会话历史、工作区所有权和文件回退是不同边界，不能因复制对话而推导副作用所有权。

## 5. Harnessix决策

1. Resume仅重新附着并返回同一Thread投影，不调用Provider、不执行工具；Runtime启动恢复仍遵循既有等待/中断规则。
2. Fork v1支持最新终结Turn或指定终结Turn的包含边界，不接受活跃Thread、归档Thread或不存在边界。
3. Fork把有界、已配对模型历史写入子Thread首事件；这些Item不属于子Thread的Turn，`authority=none`，不能被审批、Action或Tool Runtime消费。
4. 子Thread继承模型可见事实和冻结的Tool Result视图决定，不继承审批Item、待执行调用、Runtime槽、Action租约或工作区写权限。
5. Artifact正文不复制。Fork记录每个引用的真实所有者Thread；每次模型使用前仍按当前Workspace能力验证来源Artifact、TTL、正文和覆盖证明。
6. SessionStore在一个SQLite事务中校验来源sequence、完整投影摘要、边界和目标空状态，再创建子Thread。相同`source_thread_id + request_id`得到确定性子ID和首事件ID。
7. Archive写入版本化Thread事件；归档后允许读取、Replay和Rebuild，禁止新Turn、Resume、Fork和其他状态修改。
8. v1不提供Unarchive、跨Workspace Fork、活跃Turn Fork或工作区文件回退；这些能力不能通过历史继承隐式获得。

## 6. 差异化结论

Harnessix将“对话可见事实”与“副作用执行权”分离：Fork历史能继续提供上下文，但其审批、效果身份和Artifact所有权不会被重写成子Thread权限。来源CAS、确定性重建、Artifact跨代所有者链和崩溃事务测试共同构成可审计的副作用继承边界，而不是只完成消息复制。
