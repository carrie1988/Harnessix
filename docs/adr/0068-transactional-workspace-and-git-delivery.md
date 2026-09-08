# ADR 0068：事务性 Workspace 与 Git 交付

- 状态：已接受
- 日期：2026-09-08

## 背景

现有受管 Patch 副本按成员顺序写入，可正确报告部分效果，但不是通用 Workspace 事务；Git Eval 交付也不是产品运行时。生产交付需要来源保护、完整 Diff、Checkpoint、Rollback、Branch/Worktree、Commit 和独立 Push。

## 决策

1. 新增 `WorkspaceTransaction`，私有账本持久化来源 Snapshot、文件 CAS、目标 manifest、批准 fingerprint、阶段和逐成员观察。
2. 修改先发生在受管 staging Workspace；Source 保持不变。冻结时生成完整新增/修改/删除/重命名 Diff 和 before/after CAS。
3. 发布前重新核对 Source Snapshot 与涉及文件身份；脏工作区默认拒绝覆盖，只有 manifest 明确包含且获得新批准的文件可变更。
4. 文件系统发布采用 write-ahead journal、同目录临时对象、fsync、逐成员 replace 与持久游标；跨文件不虚假宣称内核原子性，但任意崩溃点都能恢复为 before、after、diverged 或 unknown。
5. Git 仓库优先在独立 worktree/branch 中完成修改；Checkpoint 使用 Git tree/commit 或 CAS manifest。Rollback 是新的显式事务，不删除用户未纳入 manifest 的修改。
6. Commit 仅作用于计划列出的路径并显式绑定作者、消息、parent和预期tree。0.7默认禁用仓库Hook、fsmonitor、attributes外部来源及可执行filter；未来启用任何Hook/filter都必须作为新的可执行资源进入Sandbox和批准指纹，不能继承普通Commit许可。
7. Push 是 Action Plane 中独立的外部网络写，默认关闭；Commit 的批准不能授权 Push、force push 或创建 PR。
8. POSIX普通Workspace提供直接发布；Windows 0.7交付优先在受管Git worktree/branch中完成。未实现抗Reparse Point竞态的Windows普通目录发布端口前，该组合失败关闭，不以字符串复核冒充安全写入。
9. Commit使用确定性原始Git对象：Checkpoint先绑定tree和parent，Commit Spec再绑定作者、时区时间、消息、实现摘要与预期OID；执行只写入该对象，并通过旧值全零的`update-ref`创建此前不存在的新branch。该策略不修改来源HEAD/index，不运行Hook，也能在返回丢失后按对象正文与ref对账。

## 取舍

- 普通文件系统无法提供多路径单系统调用原子提交，因此承诺 durable recoverability，不使用“原子多文件写”误导用户。
- Git tree 是内容快照，不是对工作区、索引、submodule、LFS 和 Hooks 的完整事务；这些能力分别建模。

## 失败语义

- Source 漂移/脏冲突：`delivery_source_changed` / `delivery_dirty_conflict`；
- 写前失败：`not_applied`；
- replace 后记账前：`unknown`，必须 reconcile；
- 部分发布：`interrupted`，重开后按 manifest 核对，不盲目继续；
- Commit对象或ref更新结果丢失：按预期OID、完整对象正文和ref查询，不生成第二个commit；
- Push 结果丢失：按远端 ref 对账，不自动重推。
