---
doc_type: source-research
status: historical
version: 1
code_revision: 83b60853718a3abb6589cafc06915a192319ae0d
owners:
  - core
modules:
  - evals
  - delivery
related_adrs:
  - docs/adr/0052-controlled-eval-change-delivery.md
related_tests:
  - tests/evals
  - tests/delivery
supersedes: []
---

> **冻结源码研究**：本资料的参考版本与访问日期冻结于2026-09-07；具体提交、版本和证据位置见正文及[统一研究基线](baselines.md)。结论不随上游分支移动自动更新，Harnessix现行行为以关联ADR和模块设计为准。

# Coding Eval变更交付源码求证与适用性研究

## 1. 研究范围

本研究回答0.5.5d的单一问题：严格通过的Coding Eval结果如何从私有受管副本转换为可审计变更包，并在不覆盖目标仓库既有改动的前提下，经显式批准写入固定来源版本。

研究对象只包括仓库内已有实现及本地固定源码版本，不把产品宣传、二手文章或模型记忆当作架构事实：

| 项目 | 本地revision | 求证文件 |
|---|---|---|
| OpenAI Codex | `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67` | `codex-rs/git-utils/src/apply.rs` |
| OpenCode | `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5` | `packages/opencode/src/project/vcs.ts`、`packages/opencode/src/git/index.ts` |
| Claude Code源码快照 | `2ca5ddabfed5f220812ea11f029eda03b21bc4c1` | `src/setup.ts`、`src/utils/worktree.ts`、`src/utils/fileHistory.ts` |
| Harnessix Code | `9d0be66e197a82506cf0d0dcbf59832d8865f1e2` | `src/harnessix/evals/*`、`src/harnessix/patches/managed.py`、`src/harnessix/tools/git.py` |

## 2. 已求证事实

### 2.1 Codex

Codex的`apply_git_patch`把统一Diff写入临时文件，预检使用`git apply --check`，真实执行使用`git apply --3way`，并把applied、skipped和conflicted路径解析为结构化结果。该设计适合一般补丁兼容与三方回退，但其核心接口不负责Harnessix已有的Eval报告绑定、批准指纹、私有状态持久化或崩溃后inode归因。

### 2.2 OpenCode

OpenCode的VCS层公开原始Patch输入和布尔应用结果；非Git仓库返回`non-git`，应用失败归类为`not-clean`，底层调用`git.applyPatch`。VCS读取同时维护完整Diff和工作树状态。该实现证明“先把隔离工作区Diff显式交付到另一个工作区”是主流产品需要的能力，但当前接口粒度不足以直接作为Harnessix的审批与恢复契约。

### 2.3 Claude Code源码快照

Claude Code支持显式创建隔离Git worktree并把会话切换到该目录，Session中保存worktree状态；文件历史模块保存快照并支持rewind。该思路强化了两个边界：Agent执行位置与用户原工作区应分离；交付或回退必须依赖持久事实，而不能只依赖最后一段自然语言回答。

### 2.4 Harnessix现状

0.5.5a—c已经拥有版本化任务、固定历史来源、受管执行副本、严格评分、Git证据、多次真实Provider试验和成本报告。0.5.3的单文件执行器还提供“临时文件fsync→意图持久化→原子替换→目录fsync→inode/后镜像核对”的成熟原语。缺口不是再造一个Patch工具，而是把**通过的Eval证据**转换为**目标仓库可批准、可恢复的发布对象**。

## 3. 方案比较

| 方案 | 优点 | 主要问题 | 结论 |
|---|---|---|---|
| 直接复制受管工作区 | 实现最少 | 无来源、报告和批准绑定；容易覆盖用户改动 | 拒绝 |
| 仅保存统一Diff并`git apply --3way` | 兼容一般上下文漂移 | 三方合并会接受非评测基线，改变0.5.5结论；崩溃归因仍缺失 | 后续通用交付再设计 |
| 自动提交或自动合并分支 | 用户操作少 | 把写工作树、写index、创建commit和合并混成一个授权 | 拒绝自动化 |
| 单文件完整前后镜像+严格CAS | 与当前唯一历史任务、受管Patch和评分证据完全一致；可做精确前镜像比较 | 暂不支持多文件、rename、create/delete和三方合并 | 0.5.5d采用 |

## 4. 选定契约

### 4.1 变更包

`CodingEvalChangePackage`只能由`completed + passed`运行生成。生成器重开物化清单、运行状态、报告和受管副本，重新采集Git证据，并要求：

- 报告摘要与运行状态一致，任务、环境、基线和run身份完整匹配；
- 14项评分检查全部通过；
- 只有一个允许的已有普通文件，没有staged、untracked、rename或其他不支持变更；
- 最终回答已解析且路径与Git实况相同；
- 物化基线前镜像、受管副本后镜像、权限和Copy Manifest一致。

包保存任务/报告/仓库来源、source revision、tree OID、规范树摘要、路径、0644/0755权限、前后UTF-8镜像和各自SHA-256。单镜像最多1 MiB，包文件最多3 MiB并要求0600。`created_at`取报告完成时间，因此相同证据重复构建得到相同包和指纹。

### 4.2 无副作用预检与批准

`prepare`要求目标是精确Git仓库根，并同时匹配：

1. `remote.origin.url`；
2. `HEAD^{commit}`；
3. `HEAD^{tree}`；
4. `git ls-tree -r -z --full-tree HEAD`的SHA-256；
5. staged、unstaged、untracked均为空；
6. 目标文件是单链接普通文件，权限和前镜像摘要一致。

计划不保存目标绝对路径，只保存Workspace scope。计划完整内容形成`approval_fingerprint`；`ApprovalRecord.request_fingerprint`必须精确匹配。拒绝决定形成持久终态且永不写目标。批准后执行前再次完成同一组仓库、状态、路径、inode、内容和权限检查。

### 4.3 写入与恢复

执行只修改工作树，不写Git index、不创建commit、不运行Hook。流程为：

```text
pending_approval → approved → applying → applied
                  └───────────────→ rejected
applying --重开观察前镜像--> approved
applying --观察归因后镜像--> applied
applying --观察第三镜像--> conflicted
applying --后镜像非本次inode/无法观察--> unknown
```

后镜像先写入目标文件同目录的唯一临时文件并`fsync`，随后把临时inode身份与`applying`状态原子持久化。最终复核只允许该临时文件作为唯一Git脏项，再执行同目录`os.replace`和目录`fsync`。恢复时只有“内容为后镜像且inode等于已持久临时inode”才能归因为本次成功；内容仍为前镜像可清理临时文件并回到`approved`；其他状态不猜测、不覆盖。

交付状态根、每个交付目录、包、锁和状态分别使用0700/0600；进程锁阻止两个Harnessix实例并发消费同一批准。完整转换历史、稳定原因码、更新时间和记录指纹提供最小可观测性。

## 5. 安全与产品边界

- 该切片只交付当前固定历史任务的**单个已有UTF-8普通文件**；不接受二进制、大文件、符号链接、硬链接、文件创建/删除、rename、submodule或多文件变更。
- 不做三方合并。任何来源revision、基线树或前镜像漂移都要求重新运行Eval或由用户手工处理。
- 不自动提交、push或修改index；`applied`表示批准的工作树后镜像已落盘并归因，不表示代码已发布。
- 目录FD、`O_NOFOLLOW`、单链接检查和最终inode复核缩小路径替换竞态。外部非协作进程仍可在任意时刻写仓库；因此产品宿主还必须对目标仓库实施独占任务租约，0.7再扩展为跨主机仓库锁和Sandbox边界。
- 变更包含源码全文，属于私有发布物，不进入模型上下文、普通日志、脱敏Campaign报告或Git仓库验证目录。

## 6. 验收结论

0.5.5d不照搬任一项目的补丁函数，而是组合三类已验证思路：隔离工作区、显式Patch交付、持久快照/恢复。它保持Harnessix差异化边界：模型只在私有副本中产生候选变更，机器证据决定是否可打包，人类或上层策略批准精确计划，执行器只在目标仍等于批准前镜像时写入，并能在进程退出后从真实文件系统效果恢复。
