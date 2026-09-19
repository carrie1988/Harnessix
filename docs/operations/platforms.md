---
doc_type: deployment-design
status: current
version: 8
code_revision: 809ed2b1a10f5cb462989a12dddf44f83a9d01ab
owners:
  - core
modules:
  - deployment
  - workspace
  - processes
  - sandbox
  - tools
  - product_ui
related_adrs:
  - docs/adr/0062-local-first-v1-commercial-boundary.md
  - docs/adr/0063-windows-v1-platform-support.md
  - docs/adr/0079-preflight-and-native-read-port.md
  - docs/adr/0081-single-coding-agent-product-boundary.md
related_tests:
  - tests/workspace
  - tests/processes
  - tests/sandbox
  - tests/product_config/test_server_and_cli.py
  - tests/tools/test_windows_read_adapter.py
  - tests/tools/test_windows_native_runtime.py
  - tests/product_ui
supersedes: []
---

# Harnessix Code平台与运行环境

## 1. 支持等级定义

| 等级 | 定义 |
|---|---|
| 产品支持 | 安装器、启动、Coding Tool、终端/进程、Sandbox、升级、恢复和Dogfooding均达到发布门禁 |
| 候选可用 | 关键纵向切片在原生平台测试，但发行、长期运行或部分能力仍缺失 |
| 库级可用 | 合同或底层端口可导入/调用，不代表默认产品装配 |
| 失败关闭 | 入口主动拒绝，不尝试使用不安全的降级实现 |
| 未验证 | 没有足够证据，不能推断行为 |

“CI存在该操作系统Job”只能证明该Job列出的测试，不自动升级为产品支持。

## 2. 当前平台矩阵

| 能力 | Linux | macOS | Windows | Container |
|---|---|---|---|---|
| Python基础包/Coding Agent | CI主路径 | 候选测试 | 选定测试 | 可构建开发命令镜像 |
| Textual View/Controller与领域交互 | CI候选 | CI候选 | CI候选 | 非容器默认入口 |
| `harnessix code`完整子进程链 | CI候选 | CI候选 | 原生只读候选，当前提交待CI | 当前镜像未装配 |
| `agent-server`默认入口 | 候选可用 | 候选可用 | 四项只读Tool候选；显式Git失败关闭 | 当前镜像未装配 |
| 只读Coding Tool | POSIX实现 | POSIX实现 | Win32 Handle实现`list/read/glob/grep`，待本轮CI | 需显式宿主装配 |
| Workspace安全观察 | POSIX FD/no-follow | POSIX FD/no-follow | Win32 Handle/Reparse Point端口 | 取决于宿主/挂载 |
| Process Supervisor | Session/Process Group | Session/Process Group | Job Object/ConPTY候选 | Container Owner可显式装配 |
| 普通目录事务发布 | POSIX候选 | POSIX候选 | 缺少抗Reparse竞态，失败关闭 | 取决于挂载语义 |
| Git Worktree/Commit | 跨平台候选 | 跨平台候选 | 跨平台候选 | 需Git和持久卷 |
| Git Push | 本地bare remote受控验证 | 同类逻辑 | 平台测试有限 | 公网认证未开放 |
| 强Container Sandbox | Docker兼容后端 | Docker兼容后端 | 后端能力依赖宿主 | 容器内再嵌套不默认支持 |
| 正式安装器/自动更新 | 未实现 | 未实现 | 未实现 | 无签名发布镜像 |

截至最终验证Revision `93723773676349fbfbe0ef42c26d9000cce379c8`，没有桌面平台达到完整1.0“产品支持”等级。Windows
原生Handle四项只读Tool已经由[CI 34735529084](https://github.com/carrie1988/Harnessix/actions/runs/34735529084)
验证，Product Preflight、`agent-server`和`harnessix code`不再由POSIX平台门拒绝；Windows显式Git仍返回
`product_git_platform_unsupported`。该证据不能外推到写入、Process、Delivery、安装器或长期终端稳定性。

## 3. CI证据矩阵

| Job | OS/服务 | Python | 覆盖边界 |
|---|---|---|---|
| `python` | Ubuntu | 3.12、3.13 | 锁定依赖、Ruff、Readability、Mypy、全量Pytest与离线示例 |
| `coding-tools-macos` | macOS | 3.12 | Tool、Artifact、Patch、Process、Eval、Context、Execution、Workspace、Sandbox、Secret、Delivery、扩展与配置选集 |
| `windows-trusted-execution` | Windows | 3.12 | 治理、Workspace、Execution、Sandbox、Process、Delivery、Trusted Action、扩展与产品配置选集 |
| `postgres` | Ubuntu + PostgreSQL 17 | 3.12 | PostgreSQL Journal集成 |
| `container-sandbox` | Ubuntu + 固定BusyBox Digest | 3.12 | 真实Container Sandbox集成 |

0.9.1a已经把Client State、Projection和Recoverable Session纳入Linux全量、macOS及Windows矩阵。0.9.1b新增的
Controller、Textual无头View、CLI和stdio恢复已由
[CI 34721082419](https://github.com/carrie1988/Harnessix/actions/runs/34721082419)完成Linux Python 3.12/3.13、macOS、
Windows、PostgreSQL、Container与文档矩阵验收。该证据只证明基础产品链的CI候选状态，不代表正式安装器、真实终端
长期运行或Windows完整产品链已经完成。

CI定义以[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)为准。0.9.1d已把Windows真实`agent-server`启动、
四项Tool、长路径、Junction、ADS、保留名、硬链接和关闭场景加入`windows-trusted-execution`，并由
[CI 34735529084](https://github.com/carrie1988/Harnessix/actions/runs/34735529084)完成验收。当前仍缺
三平台安装器、真实终端长期交互、网络文件系统、ARM发布矩阵和平台升级/回退Dogfooding。

## 4. 文件系统要求

### 4.1 POSIX

- Product Config必须为当前UID拥有的普通文件、硬链接数1、Group/Other无权限；
- Agent状态目录必须为当前UID拥有的真实目录，Mode最终为`0700`；
- Workspace与状态目录不能互相包含；配置文件不能位于Workspace；
- Coding Tool使用目录FD和`O_NOFOLLOW`，拒绝Symlink并在读取前后验证对象漂移；
- Session使用本地文件锁和SQLite WAL，不应部署到NFS或跨主机共享目录；
- 受管文件发布依赖POSIX对象和目录同步语义。

### 4.2 Windows

- Workspace观察通过Win32 Handle、File ID和Reparse Point检查提供底层能力；
- Process通过挂起创建、不可Breakaway Job Object和ConPTY管理进程树；
- 普通目录事务发布尚无与POSIX等价的抗Reparse Point竞态实现；
- Product Config的POSIX Owner/Mode检查在Windows不执行；
- 默认产品入口装配四项Windows只读Tool；Git、普通目录写与完整Delivery仍失败关闭。

Windows只读入口必须使用`WindowsWorkspaceRoot`的逐段Handle、Final Path、File ID和Reparse检查；不得替换为
字符串前缀或把POSIX权限位映射到Windows。正式发行仍需补齐配置/状态ACL、安装器和签名制品。

### 4.3 大小写与Unicode

路径权限判断必须使用平台原生规范化、对象身份和Workspace Snapshot，不只做字符串前缀比较。Windows通常大小写不敏感，
macOS默认文件系统也可能大小写不敏感；测试环境需要同时覆盖大小写敏感和不敏感行为。当前完整文件系统矩阵尚未建立。

## 5. 进程与终端

| 平台 | Owner机制 | 取消 | PTY |
|---|---|---|---|
| Linux/macOS | 新Session/Process Group | 信号进程组并等待回收 | POSIX PTY候选 |
| Windows | Suspended Process + Job Object | 终止Job并核对回执 | ConPTY候选 |
| Container | 宿主容器CLI与持久Owner账本 | 通过容器生命周期停止 | 默认非交互，取决于Profile |

平台Process合同必须验证启动前Plan、环境白名单、输出预算、Timeout、取消、进程树、Owner Lease、回执和重启恢复。
只验证`subprocess` Exit Code不足以证明平台支持。

0.9.1b TUI以Textual 8.2.8的`App.run_test()`验证按键、会话选择、Composer、Resize和Context退出，不依赖真实TTY。
该证据证明View/Controller合同，不证明平台终端全部键盘布局、IME、Shell启动、睡眠唤醒或长时间交互。真实终端和安装器
Dogfooding属于0.9.5。

## 6. Sandbox与网络

- 强隔离依赖显式探测通过的Docker兼容后端和固定镜像Digest；
- Container argv不经Shell字符串拼接；
- 网络默认关闭，启用时需绑定Profile、DNS快照和Egress规则；
- 宿主进程模式不是强Sandbox；
- Docker Desktop、Linux Docker Engine和Windows容器/WSL的网络、卷及PID语义不同，不能相互外推；
- 当前产品`agent-server`只装配本地只读Coding Tool，不装配完整Container Sandbox。

真实Container集成见[`tests/integration/test_container_sandbox.py`](../../tests/integration/test_container_sandbox.py)。

## 7. 数据库与网络拓扑

### 7.1 SQLite

SQLite用于Agent Session和多种专用账本。每个Store的并发和锁合同不同；Session明确采用
单Runtime Owner。旧Action Journal只作为迁移归档，不进入产品启动。不要在共享网络盘或多个主机间复用SQLite路径。

### 7.2 PostgreSQL

PostgreSQL 17只用于旧Action Worker兼容回归和迁移验证，不是Harnessix Code 1.0运行依赖。完成0.9.1f3后，
相关Schema仅按归档策略保留；新增产品部署不得建立多Worker Action Journal。

### 7.3 外部Provider

Provider端点必须为无用户信息、Query或Fragment的HTTPS URL。平台信任库、代理、DNS和企业TLS拦截会影响行为；
当前配置没有CA Bundle、Proxy和端点—凭据—Egress统一合同，必须在目标环境单独验收。

## 8. 容器运行要求

当前[`Dockerfile`](../../Dockerfile)只构建非Root开发命令镜像，默认执行`harnessix --help`，不监听端口、
不声明Action数据库Volume，也不启动`serve/worker`。它不是正式Coding Agent发行镜像。正式Container发行仍需在0.9.5
补齐Workspace/状态卷、Provider和Sandbox边界、资源限制、只读RootFS、网络策略、签名、SBOM及升级回退证据。

## 9. 平台验收要求

```mermaid
flowchart LR
    Install[全新安装] --> Start[启动与配置诊断]
    Start --> IO[文件/Git/终端交互]
    IO --> Failure[取消、Timeout、崩溃]
    Failure --> Recover[重启、Replay、Reconcile]
    Recover --> Upgrade[旧版本升级与回退]
    Upgrade --> Soak[长时间Dogfooding]
    Soak --> Claim[平台支持声明]
```

每个平台必须至少覆盖：

1. 安装、升级、卸载和权限；
2. 路径大小写、Unicode、长路径、Symlink/Junction/Reparse Point、Hardlink和TOCTOU；
3. Pipe/PTY、进程树、Signal/Job、Timeout和强制取消；
4. Git路径、Hook禁用、配置隔离和Credential不继承；
5. SQLite WAL、锁、备份和崩溃恢复；
6. Provider TLS、证书、DNS、代理和断网；
7. Sandbox能力探测、网络隔离和资源限制；
8. 长会话、磁盘耗尽、系统睡眠/唤醒和异常关机。

## 9.1 默认Workspace Patch平台矩阵

| 平台 | 默认能力 | 安全端口 | 当前结论 |
|---|---|---|---|
| macOS | `apply_patch_batch`可验证后广告 | POSIX目录描述符、`O_NOFOLLOW`、原子替换、目录`fsync` | 实现与本地回归完成，仍需目标Revision CI关闭 |
| Linux | `apply_patch_batch`可验证后广告 | 同上，另需发行环境文件系统验证 | 实现与本地回归完成，仍需目标Revision CI关闭 |
| Windows | 不广告Patch | 仅原生Handle只读端口 | 写入失败关闭，不允许Python路径或Shell回退 |

POSIX测试同时覆盖Unicode、创建/替换/删除、链接拒绝、Lease竞争、取消后的部分效果与Snapshot漂移。网络文件系统、FUSE、云同步目录和同UID恶意进程仍不在当前安全证明内；这些环境不得仅因操作系统名称匹配而推断受支持。

## 10. 源码与测试映射

| 平台职责 | 源码 | 测试 |
|---|---|---|
| 启动Preflight与平台选择 | [`product_config/preflight.py`](../../src/harnessix/product_config/preflight.py)、[`tools/runtime.py`](../../src/harnessix/tools/runtime.py) | [`test_server_and_cli.py`](../../tests/product_config/test_server_and_cli.py)、[`test_windows_native_runtime.py`](../../tests/tools/test_windows_native_runtime.py) |
| Workspace对象安全 | [`workspace/snapshot.py`](../../src/harnessix/workspace/snapshot.py)、[`workspace/windows.py`](../../src/harnessix/workspace/windows.py) | [`tests/workspace`](../../tests/workspace/) |
| Process Owner | [`processes/supervisor.py`](../../src/harnessix/processes/supervisor.py)、[`processes/windows_owner.py`](../../src/harnessix/processes/windows_owner.py) | [`tests/processes`](../../tests/processes/) |
| Container Sandbox | [`sandbox/container.py`](../../src/harnessix/sandbox/container.py) | [`test_container_sandbox.py`](../../tests/integration/test_container_sandbox.py) |
| POSIX交付 | [`delivery/filesystem.py`](../../src/harnessix/delivery/filesystem.py) | [`test_filesystem.py`](../../tests/delivery/test_filesystem.py) |
| Git交付 | [`delivery/git.py`](../../src/harnessix/delivery/git.py) | [`test_git.py`](../../tests/delivery/test_git.py) |
| Windows只读Tool适配 | [`tools/windows_read.py`](../../src/harnessix/tools/windows_read.py)、[`tools/windows_read_port.py`](../../src/harnessix/tools/windows_read_port.py)、[`tools/windows_search.py`](../../src/harnessix/tools/windows_search.py) | [`test_windows_read_adapter.py`](../../tests/tools/test_windows_read_adapter.py)、[`test_windows_native_runtime.py`](../../tests/tools/test_windows_native_runtime.py) |
| TUI平台中立层 | [`product_ui/controller.py`](../../src/harnessix/product_ui/controller.py)、[`product_ui/app.py`](../../src/harnessix/product_ui/app.py) | [`tests/product_ui`](../../tests/product_ui/) |

## 11. 当前风险

- Windows原生只读产品链已由CI 34735529084验证；写入、Git读取、安装器和长期稳定性仍是1.0风险；
- 0.9.1b与0.9.1c三平台CI已经完成；0.9.1c由[CI 34727612571](https://github.com/carrie1988/Harnessix/actions/runs/34727612571)验证当前领域交互矩阵，但TUI仍缺少真实用户终端长期运行和发行物证据；
- macOS/Linux尚无安装器和长期Dogfooding，候选实现不能视为产品支持；
- CI Runner不能覆盖真实用户终端、安全软件、代理、企业证书和文件系统差异；
- 容器镜像缺少正式供应链和Hardening门禁；
- ARM、WSL、网络文件系统和离线环境未形成支持政策；
- Provider网络与系统代理/证书缺少统一配置合同。
