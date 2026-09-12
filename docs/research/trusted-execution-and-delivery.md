---
doc_type: source-research
status: historical
version: 1
code_revision: f0d366c3071c2e5c134dd2c475d1763ef87360c1
owners:
  - core
modules:
  - execution
  - sandbox
  - secrets
  - processes
  - workspace
  - delivery
related_adrs:
  - docs/adr/0065-platform-capability-ports-and-execution-plan.md
  - docs/adr/0066-sandbox-network-and-secret-boundaries.md
  - docs/adr/0067-process-ownership-and-terminal-lifecycle.md
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0069-unified-coding-action-risk-route.md
related_tests:
  - tests/execution
  - tests/sandbox
  - tests/secrets
  - tests/processes
  - tests/workspace
  - tests/delivery
supersedes: []
---

> **冻结源码研究**：本资料的参考版本与访问日期冻结于2026-09-09；具体提交、版本和证据位置见正文及[统一研究基线](baselines.md)。结论不随上游分支移动自动更新，Harnessix现行行为以关联ADR和模块设计为准。

# 0.7 可信执行与工程交付研究

- 状态：已冻结
- 冻结日期：2026-09-08
- 适用范围：Harnessix Code 0.7

## 1. 研究方法与证据等级

本研究延续[0.2 源码研究基线](baselines.md)的clean-room规则，只提取机制、边界、不变量和失败语义，不复制参考实现。证据分为：

- **事实**：锁定提交的源码、测试或协议可以直接复查；
- **推断**：由多个入口和失败路径归纳，不代表参考项目的正式承诺；
- **决策**：Harnessix Code 的独立设计选择；
- **待验证**：必须通过 0.7 实现、故障注入或三平台 CI 关闭。

## 2. 刷新基线

| 项目 | 分支 | 0.7 锁定提交 | 提交时间 | 证据定位 |
|---|---|---|---|---|
| Codex | `main` | [`d6489472f3c15e87d2d7763a5fde033545c530f8`](https://github.com/openai/codex/tree/d6489472f3c15e87d2d7763a5fde033545c530f8) | 2026-09-08T05:57:24Z | Permission、Sandbox、网络代理、Windows Job Object、PTY、Exec Server |
| OpenCode | `dev` | [`d6855b6b47a8433462ac6aeeba882ccf734cb7f1`](https://github.com/anomalyco/opencode/tree/d6855b6b47a8433462ac6aeeba882ccf734cb7f1) | 2026-09-08T06:51:49Z | Permission V2、跨平台 Shell、外部目录、Git tree/worktree |
| Claude Code 逆向仓库 | 本地当前分支 | [`2ca5ddabfed5f220812ea11f029eda03b21bc4c1`](https://github.com/carrie1988/claude-code-source-code/tree/2ca5ddabfed5f220812ea11f029eda03b21bc4c1) | 2026-04-01T09:47:37+08:00 | Bash/PowerShell、Sandbox Adapter、Git 安全规则的辅助佐证 |

Claude Code 仓库并非 Anthropic 官方源码，且主提交未变化；其内容只作行为佐证，不作为安全结论的唯一依据。

## 3. Codex 事实

### 3.1 Permission 与执行快照

**事实**

1. `NetworkSandboxPolicy` 将网络能力显式区分为 `Restricted` 与 `Enabled`；文件系统访问使用 `Read`、`Write`、`Deny`，同等具体度冲突时 `Deny > Write > Read`。
2. 文件系统策略区分受限、无限制和外部 Sandbox，并支持 Root、Project Roots、临时目录等特殊路径以及缺失路径跳过语义。
3. `PermissionProfileSnapshot` 原子携带具体权限、活动 Profile 身份和 Profile Workspace Roots；Profile Roots 与 Turn 级 Runtime Roots 是不同概念。
4. 权限交集在外部 Sandbox、平台默认路径或不支持路径无法安全求交时失败关闭；输入要求在相同 executor/cwd 上完成物化和规范化。
5. 默认 workspace-write 对 `.git`、`.agents`、`.codex` 等控制面子路径保留只读保护。

**独立结论**

授权不能只绑定模型可见参数。Harnessix 必须先生成不可变 `ExecutionPlan`，把规范化参数、cwd、环境摘要、Workspace 快照、策略版本、Sandbox 和网络能力一次性冻结，再由审批、执行和审计共同消费。

**源码索引**

- [`protocol/src/permissions.rs`](https://github.com/openai/codex/blob/d6489472f3c15e87d2d7763a5fde033545c530f8/codex-rs/protocol/src/permissions.rs)
- [`permission_profile_snapshot.rs`](https://github.com/openai/codex/blob/d6489472f3c15e87d2d7763a5fde033545c530f8/codex-rs/protocol/src/permission_profile_snapshot.rs)
- [`permission_profile_intersection.rs`](https://github.com/openai/codex/blob/d6489472f3c15e87d2d7763a5fde033545c530f8/codex-rs/protocol/src/permission_profile_intersection.rs)

### 3.2 Sandbox 与网络

**事实**

1. Sandbox Manager 按平台和策略选择 macOS Seatbelt、Linux Sandbox 或 Windows Restricted Token；托管网络要求无法满足时在启动前报错，不静默降级。
2. Linux 默认使用 Bubblewrap：只读根、可写 bind roots、受保护子路径重新只读挂载；受限网络使用独立 network namespace。
3. 托管网络通过隔离网络命名空间和代理桥接，结合 seccomp 限制新建 AF_UNIX/socketpair；代理支持 HTTP、SOCKS5、allow/deny 和 limited mode。
4. allow/deny 采用 allowlist-first 且 deny 优先；有限模式只允许安全 HTTP 方法。HTTPS CONNECT/SOCKS5 若无 MITM 无法在应用层验证方法。
5. 网络代理文档明确承认仅靠域名判定难以消除 DNS rebinding，建议结合 IP 固定或更低层执行边界。
6. Windows 受限后端使用 Restricted Token/ACL；托管网络和 deny-read 的部分能力要求 elevated backend，否则失败关闭。

**独立结论**

Harnessix 0.7 不实现“看起来像 Sandbox”的 Host 文本检查。`none` 网络必须由后端证明完全禁网；选择性网络只有执行后端同时证明 DNS、IPv4/IPv6、重定向和代理路径均受控时才可声明 `restricted`。

**源码索引**

- [`sandboxing/src/manager.rs`](https://github.com/openai/codex/blob/d6489472f3c15e87d2d7763a5fde033545c530f8/codex-rs/sandboxing/src/manager.rs)
- [`linux-sandbox/README.md`](https://github.com/openai/codex/blob/d6489472f3c15e87d2d7763a5fde033545c530f8/codex-rs/linux-sandbox/README.md)
- [`network-proxy/README.md`](https://github.com/openai/codex/blob/d6489472f3c15e87d2d7763a5fde033545c530f8/codex-rs/network-proxy/README.md)
- [`windows-sandbox-rs`](https://github.com/openai/codex/tree/d6489472f3c15e87d2d7763a5fde033545c530f8/codex-rs/windows-sandbox-rs)

### 3.3 Process、PTY 与 Windows Job Object

**事实**

1. Exec Server 的 `process/start` 使用非空 argv、绝对 cwd、显式环境、PTY/pipe stdin 标志和调用方稳定 process id；连接关闭时终止该连接剩余的托管进程。
2. Windows `JobObject` 设置 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，可选择禁止 breakaway，并以 `TerminateJobObject` 终止整个进程树。
3. 先普通 spawn 再加入 Job Object 存在子进程在绑定前逃逸的竞态，Codex 源码对此有明确注释。
4. 无竞态路径使用 `CREATE_SUSPENDED`：创建挂起进程、加入 Job Object、再恢复；测试验证直接子进程属于 Job Object，且终止会清理进程树。
5. Windows PTY 使用 ConPTY，并在扩展启动信息中把伪终端和Job同时绑定；标准句柄由Pseudo Console接管而不是继承宿主。POSIX 使用进程组，并在 Linux 路径设置父进程死亡信号。
6. ConPTY 输入是终端按键流而不是普通重定向stdin。关闭其输入传输管道不能可靠表达应用EOF，关闭Pseudo Console还会向附着进程发送`CTRL_CLOSE_EVENT`；控制台应用在processed-input模式下以`Ctrl+Z`表达EOF。

**独立结论**

Harnessix Windows 原生进程端口必须采用挂起启动后加入 Job Object 再恢复；如果当前宿主 Job 限制、API 缺失或绑定失败，启动整体失败，不能退回仅终止根 PID。后台进程必须由持久 `ProcessLease` 所有，不能仅存在内存 Map。

PTY的`close_stdin`不能直接关闭ConPTY输入句柄，否则可能把正常完成误报为`0xC000013A`。Windows端发送`Ctrl+Z + CR`终端EOF键并逻辑关闭写侧，传输句柄保留至目标结束；原始模式程序若不解释该键，仍由已批准的deadline、取消或显式停止收敛，不伪造普通pipe EOF语义。

**源码索引**

- [`exec-server/README.md`](https://github.com/openai/codex/blob/d6489472f3c15e87d2d7763a5fde033545c530f8/codex-rs/exec-server/README.md)
- [`utils/pty/src/win/job.rs`](https://github.com/openai/codex/blob/d6489472f3c15e87d2d7763a5fde033545c530f8/codex-rs/utils/pty/src/win/job.rs)
- [`utils/pty/src/windows_tests.rs`](https://github.com/openai/codex/blob/d6489472f3c15e87d2d7763a5fde033545c530f8/codex-rs/utils/pty/src/windows_tests.rs)
- [Microsoft Terminal：ConPTY输入关闭与EOF讨论](https://github.com/microsoft/terminal/discussions/15006)
- [Microsoft Terminal：`ClosePseudoConsole`发送`CTRL_CLOSE_EVENT`](https://github.com/microsoft/terminal/blob/main/src/cascadia/TerminalConnection/ConptyConnection.cpp)

## 4. OpenCode 事实

### 4.1 Permission 与外部目录

**事实**

1. Permission V2 规则由 action/resource/effect 构成，最后一个匹配通配规则生效，未匹配默认 `ask`；多资源结果采用 `deny > ask > allow`。
2. Agent 配置中的 deny 在已保存 allow 前检查；`always` 规则按项目持久化，并可解决其他被新规则覆盖的等待请求。
3. 待审批对象仍保存在进程内 Map，进程结束时统一 decline，因此不是崩溃可恢复审批。
4. 路径准入同时检查词法路径与 canonical path；对尚不存在的目标向上寻找现存祖先后重建，用于识别 canonical escape；外部绝对路径产生独立 `external_directory` permission。

**独立结论**

Harnessix 可采用有序规则和外部目录单独展示，但审批必须继续使用现有 Session 事件持久化，禁止退回进程内 Promise/Map。

### 4.2 Shell、PTY 与 Git

**事实**

1. 新版 Shell 使用 tree-sitter 解析 Bash/PowerShell，并做 Windows 路径和环境处理；取消或超时后先终止，三秒后强制清理。
2. Windows 进程清理使用 `taskkill /T /F` 作为跨进程树手段；PTY 状态保存在进程内 Map，缓冲区为 2 MiB，退出会话最多保留 25 个，不提供服务重启后的持久恢复。
3. V2 Apply Patch 先解析全部目标并完成审批，但仍顺序落盘；后续成员失败时前面修改保留，源码明确承认部分效果。
4. Git 层能捕获 tracked/untracked binary patch，创建 detached worktree，捕获 tree、生成完整文件 diff，并按 tree 恢复文件。
5. tree 恢复是逐文件 checkout/remove；`checkout-index --all --force` 和 `clean -fd` 都是破坏性操作，不等于跨文件原子事务。

**独立结论**

解析 Shell 有助于解释审批，不构成隔离。Harnessix 不采用 `taskkill` 作为 Windows 正式所有权；Git tree/worktree 用作 Checkpoint 与交付载体，但仍需要事务账本、来源 CAS、脏工作区保护和崩溃恢复。

**源码索引**

- [`permission.ts`](https://github.com/anomalyco/opencode/blob/d6855b6b47a8433462ac6aeeba882ccf734cb7f1/packages/core/src/permission.ts)
- [`location-mutation.ts`](https://github.com/anomalyco/opencode/blob/d6855b6b47a8433462ac6aeeba882ccf734cb7f1/packages/core/src/location-mutation.ts)
- [`git.ts`](https://github.com/anomalyco/opencode/blob/d6855b6b47a8433462ac6aeeba882ccf734cb7f1/packages/core/src/git.ts)
- [`tool/apply-patch.ts`](https://github.com/anomalyco/opencode/blob/d6855b6b47a8433462ac6aeeba882ccf734cb7f1/packages/core/src/tool/apply-patch.ts)

## 5. Claude Code 逆向仓库佐证

**事实，仅作辅助佐证**

1. Sandbox Adapter 将文件读写路径、网络 allowed/denied domains、Unix Socket、本地监听和代理端口转换为独立 Sandbox Runtime 配置。
2. 配置文件、Skills 和 Git 控制面路径被列为禁止写入对象；对可被种植成 bare repository 的 `HEAD/objects/refs/hooks/config` 有额外防护。
3. Bash 和 PowerShell 各有解析与只读判定，危险命令提示与真正 Permission 判定分离；PowerShell 对变量、子表达式、download cradle 和 Git Hook 路径做保守升级审批。
4. Git Commit/Push 由显式用户意图控制，Push 不因已允许 Commit 自动取得权限。

**独立结论**

控制面文件必须作为高优先级 deny-write；Git Hook、配置注入和 bare repo 识别属于执行边界，而不仅是提示词规则。Commit 与 Push 必须是两个不同效果和两个不同审批指纹。

**源码索引**

- [`sandbox-adapter.ts`](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/utils/sandbox/sandbox-adapter.ts)
- [`BashTool`](https://github.com/carrie1988/claude-code-source-code/tree/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/tools/BashTool)
- [`PowerShellTool`](https://github.com/carrie1988/claude-code-source-code/tree/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/tools/PowerShellTool)

## 6. Harnessix 生产差距矩阵

| 能力 | 0.6 当前事实 | 参考实现证据 | 0.7 独立决策 | 验证门禁 |
|---|---|---|---|---|
| Workspace 路径 | POSIX root FD + `O_NOFOLLOW`，拒绝链接/硬链接 | Codex 平台权限；OpenCode 词法+canonical | `WorkspacePath` 与 POSIX/Windows 端口分离 | POSIX race + Windows drive/UNC/ADS/reparse |
| Workspace Revision | 工具版本绑定根身份；审批未绑定内容快照 | Codex 原子 permission snapshot | 显式 `WorkspaceSnapshot` 进入执行计划 | 参数/cwd/env/revision/policy 任一漂移失效 |
| Permission | Kernel 工具级准入，规则表达力有限 | Codex split policy；OpenCode ordered rules | 规范化 Resource/Effect 规则，deny 优先 | 属性测试、规则冲突、外部目录 |
| Sandbox | 未实现 OS 强隔离 | Codex 三平台后端；Claude adapter | Host 仅 guarded；Container 才声明 strong | 后端探测、不可用 fail closed、实际逃逸测试 |
| Network | Provider 有界 HTTP；Tool 无网络端口 | Codex netns+proxy | none/limited/restricted/full；无能力不降级 | DNS/IPv4/IPv6/redirect/proxy bypass |
| Secret | Provider Key 由宿主注入，缺统一 Provider/Redactor | Claude/Codex 最小环境 | `SecretRef` + 临时物化 + 全链路 Redactor | Canary 不出现在七类持久/展示面 |
| Process | POSIX argv、单前台、进程组、有界双流 | Codex PTY/Job Object；OpenCode 跨平台 Shell | 持久 `ProcessLease`、stdin/PTY/background、三平台 owner | 取消/超时/宿主死亡/重启核对 |
| Windows Process | 不支持 | Codex suspended + Job Object | 原生 Job Object，失败不得退根 PID | Windows 真机进程树测试 |
| 多文件写 | 受管副本，组内顺序写且可部分效果 | OpenCode Patch 同样部分效果 | 来源 CAS + durable transaction + reconcile | 每个崩溃切点可归因/恢复 |
| Git | 只读状态/diff/test反馈；Eval 有独立交付 | OpenCode tree/worktree | Checkpoint/rollback/branch/worktree/commit 正式端口 | 脏保护、Hook/配置注入、完整 diff |
| Push | 无正式产品端口 | Claude 显式 Push 意图 | 独立高风险 Action，默认关闭 | 无单独批准不能联网/推送 |
| Action Plane | 通用外部 Action 有 UNKNOWN/Reconcile；Coding Tool 各自桥接 | Codex/Claude 工具路由 | 统一 immutable ExecutionPlan 和审计入口 | 扩展不能旁路；结果丢失不重放 |

## 7. 平台求证结论

### 7.1 路径

- 领域层路径统一为 UTF-8、`/` 分隔、相对 Workspace 的 `WorkspacePath`，禁止 `.`、`..`、空段、NUL 和绝对前缀。
- POSIX 端口继续使用 root FD、`openat`/no-follow 和对象身份复核。
- Windows 端口必须额外拒绝设备命名空间、盘符/UNC 注入、ADS、保留名、尾随点/空格；根路径可由宿主使用盘符或 UNC 绑定，但模型参数不可携带根前缀。
- Windows 打开链必须拒绝 Reparse Point/Junction，并用文件句柄身份复核；单纯 `Path.resolve()` 或字符串大小写比较不能构成边界。

### 7.2 进程

- POSIX owner 为新 Session/Process Group；Linux 可额外使用 parent-death signal，但不能代替重启核对。
- Windows owner 为不可 breakaway 的 Job Object；创建路径必须消除 spawn→assign 窗口。
- macOS/Linux/Windows 均需要持久 lease、bounded output artifact、显式 stdin/PTY、停止原因和回收结果。

### 7.3 Sandbox 与发行

- macOS Host strong 可由 Seatbelt 适配器提供；Linux Host strong 可由 Bubblewrap + namespaces/seccomp 提供；缺能力时失败关闭。
- Windows Restricted Token/ACL 后端工程量和审计成本高于 0.7 的 Python 主线，0.7 strong 默认采用受管 Docker Desktop 或 WSL2 Linux Sandbox；Windows native host 只声明 guarded。
- Python 继续承担 Kernel、契约和编排；只有基准或不可修复的原生生命周期缺口证明必要时才引入 Rust sidecar。0.7 研究未发现必须立即迁移整个 Runtime 的证据。

## 8. 0.7 验收假设

1. 同一执行计划从展示、批准到 spawn/commit 的 canonical JSON 必须一致。
2. 强隔离请求没有可证明后端时，结果是 `sandbox_unavailable`，不是 host fallback。
3. 选择性网络无完整代理和网络命名空间时不可用；`network=none` 可作为默认强门禁。
4. Windows native 文件和进程能力必须在 Windows CI 真实执行；仅纯函数测试或 WSL2 不算 native。
5. 多文件事务允许崩溃中间态短暂存在，但恢复后必须确定为 before/after/diverged/unknown，不能盲重放。
6. Commit 只消费已批准交付计划；Push 永远形成新的 Action。
7. 0.4.3c 的真实 Provider 计价适用性不属于可信执行实现，按路线图留到 0.9 Provider 发布证据门禁关闭。

## 9. 0.7.4事务交付专项求证

### 9.1 Codex受管Worktree

**事实**

1. Codex `worktree`组件先解析精确仓库根与基准commit，再以`worktree add --detach --no-checkout`创建受管checkout，随后用固定`reset --hard --no-recurse-submodules`物化文件；中途失败会删除不完整worktree。
2. Git命令主动清除继承的`GIT_DIR`、`GIT_WORK_TREE`、`GIT_INDEX_FILE`、replace refs、object目录等仓库选择环境，禁用terminal prompt、hooks、fsmonitor、attributes来源，并在工作树操作前枚举和关闭配置的clean/smudge/process filter。
3. 重开受管worktree时同时核对受管目录布局、`.git`普通文件、common directory和管理目录`gitdir`回链，避免陈旧注册被其他checkout占用。

**独立结论**

Harnessix的Git执行环境不能只设置`GIT_CONFIG_GLOBAL=/dev/null`。受管worktree创建、重开、暂存和commit都必须清除仓库选择环境并固定hooks、fsmonitor、attributes和filter语义；worktree路径及管理回链必须进入持久身份。

**源码索引**

- [`worktree/src/git.rs`](https://github.com/openai/codex/blob/d6489472f3c15e87d2d7763a5fde033545c530f8/codex-rs/worktree/src/git.rs)
- [`worktree/src/lib.rs`](https://github.com/openai/codex/blob/d6489472f3c15e87d2d7763a5fde033545c530f8/codex-rs/worktree/src/lib.rs)
- [`worktree/src/git_tests.rs`](https://github.com/openai/codex/blob/d6489472f3c15e87d2d7763a5fde033545c530f8/codex-rs/worktree/src/git_tests.rs)

### 9.2 OpenCode Tree、Diff与恢复

**事实**

1. OpenCode通过刷新index并执行`write-tree`捕获tracked与受限untracked文件；预览使用独立`GIT_INDEX_FILE`、`read-tree`和`update-index --cacheinfo`生成候选tree，不直接改变主index。
2. tree diff逐路径输出新增、删除、修改、行数和文本patch；binary单独标记，不把空patch误认为无变化。
3. tree restore按路径checkout或删除，完整checkout使用`read-tree`和`checkout-index --all --force`；这些操作仍是逐成员效果，不提供跨路径内核原子性。
4. 独立worktree使用detached HEAD创建；变更捕获同时处理tracked binary patch与untracked文件。

**独立结论**

Git tree适合作为Checkpoint和commit前事实，但不能代替Workspace事务。Harnessix使用私有CAS保存before/after文件镜像、独立index生成预期tree、逐成员write-ahead游标和事后核对；恢复只能根据实际文件或Git tree判定before、after、interrupted、diverged或unknown。

**源码索引**

- [`packages/core/src/git.ts`](https://github.com/anomalyco/opencode/blob/d6855b6b47a8433462ac6aeeba882ccf734cb7f1/packages/core/src/git.ts)

### 9.3 Claude Code逆向仓库的Git授权佐证

**事实，仅作辅助佐证**

1. Commit和Push是不同工具权限，单次Push授权不扩展到后续上下文；强制Push和破坏性checkout/reset被单独视为高风险。
2. Bash/PowerShell安全分析必须防止命令拼接、环境前缀和变量展开把看似Commit的授权扩大为Push或其他命令。
3. worktree辅助逻辑会处理`core.hooksPath`与Husky，但这同时证明Hook是可执行控制面，不能只作为普通仓库文件处理。

**独立结论**

Harnessix不从Shell文本推断Git写权限。Commit、创建/移动本地ref和Push分别使用结构化合同；0.7默认禁用仓库Hook与外部filter，未来启用必须作为新的可执行资源进入Sandbox和批准指纹。Push不属于Workspace事务，也不继承Commit批准。

**源码索引**

- [`src/commands/commit.ts`](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/commands/commit.ts)
- [`src/commands/commit-push-pr.ts`](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/commands/commit-push-pr.ts)
- [`src/tools/BashTool/bashSecurity.ts`](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/tools/BashTool/bashSecurity.ts)

### 9.4 兼容性求证与独立实现结论

本地macOS系统Git为2.24.3。该版本的`rev-parse`不支持`--path-format=absolute`，`worktree list`也不支持`-z`；直接采用新版本argv会把未知选项当成revision文本或直接失败。Harnessix因此使用所有目标版本均可核对的`rev-parse --show-toplevel`，并只解析自身生成、不含控制字符的受管worktree路径。对象格式由已验证HEAD OID长度确定，不依赖较新`--show-object-format`。这些兼容分支经过真实2.24.3执行，不以版本字符串臆测能力。

独立实现不复制任一参考项目：Workspace正文保存在私有SHA-256 CAS；Git对象通过独立`GIT_INDEX_FILE`、`read-tree`、固定`hash-object`与`update-index --cacheinfo`生成；候选tree与计划路径全集核对后再通过受管worktree独立index物化。Commit原始对象由已批准的tree、parent、作者、时间和消息确定，先用不写对象的`hash-object -t commit --stdin`得到预期OID；执行时写入同一确定性对象，再以旧值全零的`update-ref`创建新branch。这样在对象写入或ref更新后丢失响应时，可以核对精确对象和ref，不生成第二个提交，也不移动来源HEAD或来源index。

## 10. 可复查命令

~~~bash
git -C "$HARNESSIX_RESEARCH_ROOT/codex" show -s --format='%H %cI %s' \
  d6489472f3c15e87d2d7763a5fde033545c530f8
git -C "$HARNESSIX_RESEARCH_ROOT/opencode" show -s --format='%H %cI %s' \
  d6855b6b47a8433462ac6aeeba882ccf734cb7f1
git -C "$HARNESSIX_RESEARCH_ROOT/claude-code-source-code" \
  show -s --format='%H %cI %s' \
  2ca5ddabfed5f220812ea11f029eda03b21bc4c1
~~~
