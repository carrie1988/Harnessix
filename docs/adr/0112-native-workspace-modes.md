---
doc_type: adr
status: current
version: 1
code_revision: d5f34d25d125f952624f8f7b1f76de6d6468bb09
owners: [core]
modules: [sandbox, workspace, product_config]
related_adrs:
  - docs/adr/0028-managed-patch-execution.md
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0111-native-process-sandbox-backends.md
  - docs/adr/0113-sandboxed-command-approval-and-host-escalation.md
  - docs/adr/0121-external-boundary-native-process-profile.md
related_tests:
  - tests/sandbox
  - tests/product_config/test_native_process_action.py
supersedes: []
---

# ADR-0112：原生命令的工作区模式

- 状态：已接受（2026-11-08 随实现合入主线）。设计已由用户在 2026-11-07 拍板（第 6 节）
- 日期：2026-11-07（改写 2026-11-06 草案）

## 1. 背景

- 容器 Profile 的工作区只有两种模式：
  - `read_only`；
  - `snapshot`：每条命令在一次性写时复制副本中运行，结束即丢弃。
- 在这两种模式下，源码只能经受管补丁事务修改（ADR 0028、0068）。
- ADR 0121 的外部边界 Profile 已经改为 `read_write`：命令直接写工作区，改动保留。
- 用户决策（2026-11-07）：原生沙箱同样采用“命令直接写工作区、改动保留”，与 Codex、Claude Code、Cursor、Gemini CLI 一致。没有主流产品在每条命令后自动撤销写入。
- 原草案的以下内容随之取消（第 2.5 节）：
  - `snapshot` 与原地快照；
  - 命令前清单与旧内容；
  - `/undo`；
  - 事件 v23。

## 2. 决策

### 2.1 两种模式

| 模式 | 含义 | seatbelt | bubblewrap | external_boundary、host |
|---|---|---|---|---|
| `read_only` | 工作区只读；命令只能写私有 TMPDIR、私有缓存目录和 `write_roots`（ADR 0111 第 2.3 节） | 支持 | 支持 | 不支持（无法强制） |
| `read_write`（默认，零配置采用） | 命令直接读写工作区，改动保留 | 支持 | 支持 | 支持 |

- 原生 Profile 不提供 `snapshot`；容器 Profile 不变。
- `read_only` 适合只查看、不修改的命令（检索、`git log`、只检查不修复的 lint），需要用户显式配置。测试运行器写缓存（如 `.pytest_cache`、`__pycache__`）会失败；常见运行器能容忍这类失败，由兼容用例确认。

### 2.2 工作区内的固定保护（两种模式都适用）

- **`.git` 只读**，包括 worktree 或子模块指向的外部 gitdir 与 common dir。
  - 原因一：防止命令写入 `.git/hooks` 或 `.git/config`（`core.hooksPath`、`core.fsmonitor`、别名等）。这些改动不出现在 Git 差异中，却会在你之后于终端或 IDE 中运行 Git 时，以你的完整权限在沙箱外执行，等于逃出沙箱。Harnessix 自己调用 Git 时已用参数禁用钩子和 fsmonitor，受威胁的主要是你在沙箱外运行的 Git。
  - 原因二：提交、分支和暂存归 Harnessix 交付流程管理（ADR 0068），命令不应绕开它改写仓库状态。
  - 影响：`git status`、`diff`、`log`、`show`、`blame` 正常；`git add`、`commit`、`stash`、`checkout`、`reset` 会失败。工具说明会告诉模型这一点。
  - 业界：Codex 在可写工作区模式下同样把 `.git` 设为只读；Cursor 保护 `.git/config` 与 `.git/hooks`。
- **工作区 `.env*` 拒读**：除非 Profile 开启 `allow_workspace_env_files`；各后端的保证范围见 ADR 0111 第 2.3 节。
- **工作区 `.harnessix` 不可读写**：与读工具一致。
- **沙箱外命令不受以上保护**：ADR 0113 的沙箱外命令没有隔离，由用户逐条批准。

### 2.3 与补丁事务、交付的关系

- 命令只在前台运行，且不能与其他工具并行（`supports_parallel_calls` 为否），所以同一会话里命令与补丁不会同时写工作区。
- 命令不持有 `WorkspaceLeaseStore` 租约，与 ADR 0121 一致。
- 启动前复核规划时的工作区快照，变化即报 `execution_plan_stale`（现有行为）。
- 命令改过模型之前读到的文件后，模型若基于旧内容打补丁，补丁事务会在前镜像核对时报 `patch_source_changed`，不覆盖文件；模型需要重读。
- Git 交付（ADR 0068）把命令改动当作普通工作区改动，交付前的来源核对照常生效。

### 2.4 中断与恢复

- Harnessix 或宿主进程崩溃后重启：
  - 按现有进程账本对账，终止残留进程（ADR 0111 第 2.5 节），并清理残留的私有 TMPDIR。
  - 任何情况下都不重放命令。
- 中断时命令可能只执行了一部分，工作区保持中断时的样子。工具结果沿用现有原因码（`process_host_lost`、`process_state_unknown` 等）如实报告。

### 2.5 不做的事

| 原草案内容 | 处理 | 原因 |
|---|---|---|
| `snapshot` 与原地快照 | 取消 | 用户决策改为保留写入；Seatbelt 无挂载命名空间，原地快照依赖清单加撤销，实现量大 |
| `/undo` 撤销最近一条命令 | 取消 | 主流产品都不做；被忽略路径（`node_modules`、`.venv`）无法可靠恢复；用户可用 Git 自行回退 |
| 命令前清单与旧内容存储 | 取消 | 只为撤销服务 |
| 独占工作区租约 | 取消 | 命令本就串行；补丁有前镜像核对 |
| 记录每条命令改了哪些文件（含事件 v23） | 不做（用户决策） | 模型可自行运行 `git status`；改动经 Git 差异与交付流程可见；以后需要时可以纯新增，不需推翻现有设计 |

## 3. 取舍

- **改动不可撤销**：换来实现简单、与主流一致。
  - 命令误删或误改的文件，已提交的可用 Git 找回（`.git` 只读，命令改不了历史）。
  - 未提交的改动和被忽略路径中的内容无法恢复。
  - ADR 0113 让沙箱内命令自动执行后，这一风险不再有逐条审批兜底，如实声明。
- **`.git` 只读**：换来封住经 Git 钩子逃逸的路径，代价是模型不能自己提交或暂存。在 Harnessix 中，这些操作本来就由交付流程负责。

## 4. 失败语义

- 本 ADR 不新增 Harnessix 原因码。
- 写只读路径（`read_only` 下的工作区、`.git`、真实 HOME）失败的报错属于命令自身，按非零退出码报告（`process_nonzero_exit`），错误信息由命令输出给出。
- 沙箱相关原因码见 ADR 0111 第 5 节。

## 5. 验收

并入 ADR 0111 第 6 节：
- A 类覆盖 `.git` 写入失败、`read_only` 下工作区写入失败；
- B 类覆盖 `read_write` 下改动保留、Git 只读操作正常；
- D 类覆盖中断后不重放。

## 6. 决策记录（用户 2026-11-07 拍板）

| 事项 | 决定 |
|---|---|
| 工作区语义 | 命令直接写工作区，改动保留；零配置默认 `read_write` |
| 是否记录命令改了哪些文件 | 不记录 |
| `.git` 权限 | 整个 `.git` 只读 |
