---
doc_type: adr
status: current
version: 1
code_revision: 24e08998146aacbeb8c4dd8238e5ee3dca20f081
owners:
  - core
modules:
  - agent
  - session
  - context
  - artifacts
related_adrs: []
related_tests:
  - tests/context
supersedes: []
---

# ADR 0060：Thread生命周期与无授权Fork

- 状态：Accepted
- 日期：2026-09-08

## 背景

持久Coding Agent必须支持重新附着、分支探索和归档整理。直接复制完整Thread投影会把审批、Action状态和外部效果身份带入子Thread；只复制文本又会破坏Tool Call/Result配对、Compaction和大结果Artifact证明。跨Thread创建还存在来源在读取后继续变化、目标仅提交一半及调用方重试创建重复分支的问题。

## 决策

1. `resume_thread`是只读重新附着，不产生事件，不调用Provider或Tool。
2. `ThreadForkSnapshot v1`作为子Thread首事件的一部分，冻结来源Thread、sequence、投影SHA-256、终结Turn边界、原始模型历史、模型视图SHA-256、Tool Result决定和Artifact真实所有者。
3. Fork历史标记`authority=none`并存放在`Thread.fork_snapshot`，不进入`Thread.turns`。Reducer、审批和执行端口只能修改本地活跃Turn。
4. SessionStore新增跨Thread `fork`事务：先识别已提交的确定性目标，再校验来源CAS和纯函数重建结果，最后原子写入目标Event与Projection。
5. Fork请求ID在来源Thread命名空间内生成确定性子Thread ID；来源不变时可幂等重试，来源变化时同请求明确冲突。
6. Artifact保留原所有者，不复制正文、不改写manifest。子Thread每次使用历史前仍以当前Workspace scope访问原所有者并验证完整证据。
7. `ThreadArchived v1`只允许在无活跃Turn时提交。归档是不可变只读状态；重复同原因归档幂等，不同原因冲突。
8. Agent Event/Thread升级为v16，Session migration18只推进最低reader，不改写旧事件、投影或Artifact。

## 失败语义

| 场景 | 错误 | 状态变化 |
|---|---|---|
| 来源有活跃Turn | `thread_busy` | 无 |
| 来源已归档 | `thread_archived` | 无 |
| 边界不存在 | `turn_not_found` | 无 |
| 来源sequence或摘要变化 | `sequence_conflict` | 无 |
| 相同请求绑定不同快照 | `event_conflict` | 已提交目标保持不变 |
| Fork快照不能重建 | `thread_fork_invalid` | 无 |
| Artifact缺失、过期或损坏 | 既有Artifact错误 | 发网前失败，无子Thread |
| 活跃Thread归档 | `thread_busy` | 无 |
| 归档后写入 | `thread_archived`或Reducer拒绝 | 无 |

## 后果

### 正向

- Resume/Fork不会自动重放已完成副作用；
- 子Thread可继续使用完整Tool历史和Compaction结果；
- 来源、边界和Artifact所有权可审计；
- 事务退出后只存在“完整提交”或“完全回滚”两种状态。

### 代价

- Fork首事件物化最多8192项/8 MiB历史，增加存储；
- 来源Artifact过期后，子Thread同样无法继续使用被省略正文；
- v1归档不可逆，恢复活跃使用需创建安全Fork或后续显式Unarchive契约。

## 未采用方案

- **复制全部Turn投影**：会复制审批与执行状态，副作用边界不安全。
- **仅保存父Thread指针并动态读取最新历史**：Fork边界会随父Thread变化，无法确定性Replay。
- **复制Artifact正文并改写所有者**：扩大配额、生命周期和一致性事务，且掩盖真实证据来源。
- **允许活跃Turn Fork**：无法证明开放Provider、审批或外部效果的确定状态。
