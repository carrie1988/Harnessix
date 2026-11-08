---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [sandbox, processes, execution, product_config]
related_adrs:
  - docs/adr/0066-sandbox-network-and-secret-boundaries.md
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0112-native-workspace-modes.md
  - docs/adr/0113-sandboxed-command-approval-and-host-escalation.md
  - docs/adr/0121-external-boundary-native-process-profile.md
related_tests:
  - tests/sandbox
  - tests/product_config/test_native_process_profile.py
  - tests/product_config/test_native_process_action.py
supersedes: []
---

# ADR-0111：原生进程沙箱后端（Seatbelt 与 bubblewrap）

- 状态：评审中。设计已由用户在 2026-11-07 全部拍板（第 8 节）
- 日期：2026-11-07（改写 2026-11-06 草案）
- 范围：macOS、能创建用户命名空间的 Linux 宿主机；Windows 与容器内环境不在本期

## 1. 背景

- ADR 0066 定义了 `host_sandboxed`，但代码只有预检（`sandbox/capabilities.py` 的 `probe_host_sandbox`），没有执行后端。
  - 产品的命令工具（`exec_command.<id>`）只有两条路：
    - 容器 Profile，必须有 Docker 或 Podman；
    - ADR 0121 的外部边界 Profile，Harnessix 不加隔离。
  - 没装容器引擎的开发机没有“带隔离的命令执行”。
  - 配置命令工具还要手写带摘要的 Action 配置，普通用户无法完成。
- ADR 0121 已新增 `harnessix.product-native-process-profile/v1`，`backend` 字段目前只有 `external_boundary`，并预留给本 ADR 的后端。
- 业界做法（2026-11-07 查证）：

| 产品 | 隔离机制 | 读 | 命令直接联网 | 沙箱内命令审批 |
|---|---|---|---|---|
| Codex CLI | macOS Seatbelt、Linux bubblewrap | — | 默认禁止，需要时申请越界 | 不逐条审批，越界才问 |
| Claude Code | 同上 | 几乎全盘可读 | 禁止，只能经本机代理按域名放行 | 默认自动批准 |
| Cursor | 同上 | 工作区外默认可读 | 只能访问白名单（含常用包源） | 不逐条审批 |

- 实测（2026-11-06、11-07）：

| 环境 | 结果 |
|---|---|
| 247 宿主机（Ubuntu 22.04，内核 5.15） | 非特权用户命名空间开启；bubblewrap 0.6.1 已安装；以 `nobody` 身份运行 bubblewrap 成功 |
| 247 默认 Docker 容器 | 用户、挂载、网络、PID 命名空间全部被拒，bubblewrap 不可用 |
| 本机 macOS 27（arm64） | `/usr/bin/sandbox-exec` 存在；在已沙箱化的进程里再套一层返回 EPERM（Claude 会话本身就在 Seatbelt 下） |

## 2. 决策

### 2.1 两个后端，统一归入 `host_sandboxed`

| 后端 | 适用 | 启动方式 |
|---|---|---|
| `seatbelt` | macOS | `/usr/bin/sandbox-exec -p <策略> -D<参数>… <argv>`。策略文本固定，路径只经 `-D` 参数传入，不拼进策略文本 |
| `bubblewrap` | 能创建用户命名空间的 Linux 宿主机 | `bwrap --ro-bind / /` 后叠加可写目录与遮盖，参数包括 `--unshare-net --unshare-pid --unshare-ipc --unshare-uts --unshare-cgroup-try --new-session --die-with-parent --cap-drop ALL`；最终参数集在 247 的 bubblewrap 0.6.1 上实测定稿 |

- **不新增依赖。** 两个后端都通过子进程调用系统自带或已安装的程序。
- **可执行文件路径固定，不按 PATH 查找。**
  - Seatbelt 只用 `/usr/bin/sandbox-exec`。
  - bubblewrap 只查固定候选（`/usr/bin/bwrap`、`/usr/local/bin/bwrap`）。
  - 可执行文件身份摘要（复用 `executable_identity_digest`）写入能力证据，每次启动前复核，身份变化即拒绝执行。
- **进程监管沿用现有 POSIX Process Owner**：`setsid`、`PR_SET_PDEATHSIG`、按进程组终止、账本对账。
  - 启动类别仍是 `host`，进程账本与 `process-launch-binding` 合同不变。
  - 后端只体现在执行计划的 `SandboxBinding` 和命令 argv 前缀上。
- **`SandboxBinding` 增加一致性规则**：`host_sandboxed` 的 backend 只能是 `seatbelt` 或 `bubblewrap`（`execution/contracts.py`）。

### 2.2 零配置与可选配置

**零配置（默认路径）**：
- 启动 `harnessix code` 或 `harnessix exec` 时，如果没有指定 `--action-config`，Harnessix 自动探测本机沙箱。只有这些用户入口请求零配置；程序化与评测启动不受影响（ADR 0113 第 2.4 节）。
- 探测通过就自动生成并启用一个原生沙箱 Profile，用户不需要跑向导，也不需要写配置。
- 启动时显示一行说明，例如“命令在沙箱中运行（macOS Seatbelt）”。
- 探测不通过时的处理见 ADR 0113 第 2.3 节：提供需逐条批准的沙箱外命令，并醒目说明原因与修复办法。

零配置 Profile 的参数全部自动推导：

| 参数 | 推导规则 |
|---|---|
| 后端 | macOS 用 `seatbelt`，Linux 用 `bubblewrap` |
| shell | 登录 shell（`$SHELL`）是 `zsh` 或 `bash` 时用它（`-lc`），否则 `/bin/sh -c` |
| 工作区模式 | `read_write`（ADR 0112） |
| 环境变量 | 按下面的继承白名单从启动环境中取 |
| 包源镜像 | 从用户的 pip、npm、go 配置中只取镜像地址（如 `index-url`、`registry`、`GOPROXY`），以环境变量传入。令牌等凭据不取：`~/.npmrc` 本身在拒读清单里 |
| 超时、输出上限 | 沿用 ADR 0121 默认值 |

- **配置摘要随环境变化**：零配置 Profile 由环境推导，PATH 等变化后配置摘要随之变化。现有双配置激活机制把它当作一次新激活，并先结算上一配置下未完成的命令（`docs/modules/product-config.md` 第 51.3 节），无需额外设计。
- **显式配置优先**：指定了 `--action-config` 时完全按配置文件，不再自动生成。

**环境变量继承白名单**：
- 命令不继承 App Server 的完整环境，只从启动环境中继承固定白名单内的变量：
  - `HOME`、`USER`、`LOGNAME`、`SHELL`、`PATH`、`LANG`、`LC_*`、`TERM`；
  - 常见工具链变量，如 `JAVA_HOME`、`GOPATH`、`CARGO_HOME`、`RUSTUP_HOME`、`VIRTUAL_ENV`、`CONDA_PREFIX`。完整清单随兼容用例定稿。
- 永不继承：
  - 产品配置中引用的模型凭据变量；
  - `HARNESSIX_*`；
  - 代理变量（`HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY`）：沙箱内不能借代理联网（2.4）。代理变量只交给沙箱外命令（ADR 0113）。
- `TMPDIR` 与工具缓存变量由 Harnessix 设置（2.3）。

**可选配置**：给需要定制的用户。沿用 `ProductActionConfigV1.native_process_profiles` 与 `harnessix.product-native-process-profile/v1`。新增字段都有默认值，取默认值时不序列化，已有外部边界 Profile 的摘要不变。

| 字段 | 取值与规则 |
|---|---|
| `backend` | 新增 `seatbelt`、`bubblewrap`，以及 ADR 0113 的 `host`（沙箱外，逐条批准）；`external_boundary` 不变 |
| `workspace_mode` | `read_only` 或 `read_write`（默认）；`external_boundary`、`host` 只允许 `read_write` |
| `write_roots` | 额外可写目录，最多 8 项，仅沙箱后端可用；不能是 `/` 或 HOME，不能与拒读路径重叠 |
| `allow_workspace_env_files` | 默认 `false`；为 `true` 时放行工作区 `.env*`；仅沙箱后端可用 |
| `program`、`arguments`、`timeout_seconds`、`max_output_bytes`、`environment` | 沿用 ADR 0121；`HOME`、`TMPDIR`、`XDG_*` 及工具缓存变量由 Harnessix 设置，不得覆盖 |

- 网络不设字段：bubblewrap 固定断网，Seatbelt 固定为 2.4 节的方式。
- 不支持 Secret 注入。

### 2.3 文件视图：全盘可读、拒读清单优先、写入严格限制

**读**：
- 整台机器可读，拒读清单中的路径除外。
- 真实 HOME 可读，所以 `.gitconfig`、pip 镜像配置等用户配置照常生效。
- `HOME` 环境变量保持真实路径。

**写**：只允许以下位置，其余一律拒绝（包括真实 HOME）。

| 可写位置 | 说明 |
|---|---|
| 工作区 | 仅 `read_write` 模式；`.git` 除外（ADR 0112） |
| 私有 TMPDIR | 每条命令一个，结束即删除；Linux 上同时把 `/tmp` 换成私有 tmpfs |
| 私有缓存目录 | 按工作区隔离、跨会话保留：macOS 为 `~/Library/Caches/harnessix/sandbox/<工作区指纹>/`，Linux 为 `${XDG_CACHE_HOME:-~/.cache}/harnessix/sandbox/<工作区指纹>/`。常见工具的缓存经环境变量指向这里（如 `XDG_CACHE_HOME`、`PIP_CACHE_DIR`、`npm_config_cache`、`GOCACHE`），清单随兼容用例定稿 |
| `write_roots` | 用户显式声明 |

真实 HOME 不可写，是为了防止命令改写 `~/.zshrc`、`~/.gitconfig` 一类文件，在你之后打开终端或运行 Git 时于沙箱外执行。

**拒读清单**：读写都拒绝，优先于任何放行。完整清单随实现定稿，并可在诊断中查看。

| 类别 | 路径（示例） |
|---|---|
| 登录与签名凭据 | `~/.ssh`、`~/.gnupg`、`~/.netrc`、`~/.git-credentials`、`~/.config/gh` |
| 包发布与镜像仓库凭据 | `~/.npmrc`、`~/.yarnrc.yml`、`~/.pypirc`、`~/.cargo/credentials`、`~/.cargo/credentials.toml`、`~/.docker`、`~/.m2/settings.xml`、`~/.gradle/gradle.properties` |
| 云与集群凭据 | `~/.aws`、`~/.config/gcloud`、`~/.azure`、`~/.kube`、`~/.terraform.d/credentials.tfrc.json` |
| 系统密钥与浏览器 | macOS `~/Library/Keychains`、`~/Library/Cookies`、主流浏览器配置目录；Linux `~/.local/share/keyrings`、主流浏览器配置目录 |
| 命令历史 | `~/.zsh_history`、`~/.bash_history`、`~/.python_history`、`~/.node_repl_history`、`~/.psql_history`、`~/.mysql_history` |
| Harnessix 自身 | `~/.harnessix`，以及实际生效的状态目录、产品配置文件和 Action 配置文件 |
| 工作区内 | `.env`、`.env.*`（判定规则与读工具 `tools/workspace.py` 一致，大小写不敏感；`allow_workspace_env_files` 可放行）、`.harnessix` |

`write_roots` 与拒读清单的重叠检查分两次：加载配置时做字面检查，探测时解析符号链接后再查一次。

**两个后端的落实方式**：

| 要求 | seatbelt | bubblewrap |
|---|---|---|
| 全盘可读 | `(allow file-read*)` | `--ro-bind / /` |
| 写入范围 | `(deny default)` 下按目录放行 `file-write*` | 对可写目录用 `--bind` 覆盖 |
| 拒读清单 | 放在放行规则之后的 `deny` 规则 | 目录用空的只读目录覆盖，文件用 `/dev/null` 只读覆盖；不存在的路径无需处理 |
| `.env*` | 不锚定路径的正则拒绝规则：磁盘上所有 `.env*` 都拒读，包括其他项目和命令新建的文件 | 启动前查找工作区内已存在的文件逐个遮盖（见下）；工作区之外的不遮盖 |
| `.git` 只读 | 工作区放行之后加 `deny file-write*` | 工作区 `--bind` 之后再对 `.git` 做 `--ro-bind` |
| 宿主 Unix 套接字 | 网络规则只放行私有 TMPDIR 内的套接字（2.4） | 用私有 tmpfs 遮盖 `/run`（含 `/var/run`）与 `/tmp`，挡住 Docker、D-Bus、ssh-agent、X11 等常见套接字；`~/.docker` 在拒读清单中。其他位置的套接字理论上仍可连接，如实列入保证清单 |

**bubblewrap 查找工作区 `.env*` 的方式**：
- Git 工作区用 `git ls-files` 列出已跟踪、未跟踪和被忽略的文件。被整体忽略的目录（如 `node_modules`）合并为一项，不深入其内部。
- 非 Git 工作区做有界遍历，有深度和条目上限。
- 遍历超限，或需要遮盖的文件超出参数容量时，拒绝执行并报 `workspace_secret_files_unmaskable`，结果中给出原因与解决办法。

**参数数量上限**：`ProcessSpec.argv` 最多 128 项，bubblewrap 每个绑定或遮盖占 3 项，固定部分约 40 项。
- 合并去重后超过上限时报 `sandbox_policy_too_large`。
- 本期不修改 `ProcessSpec` 合同。若常见机器上实测不够，再单独商定提高上限（涉及 `processes` 合同）。

### 2.4 网络

| | bubblewrap（Linux） | seatbelt（macOS） |
|---|---|---|
| 访问外网 | 不能：新网络命名空间，只有独立的回环接口 | 不能：非回环出站全部拒绝 |
| 本机回环 `localhost` | 可用，但是沙箱私有的，与宿主回环隔离，碰不到宿主上的任何服务 | **放行（用户决策）**，连到的是宿主真实回环；已配置的代理端口除外（见下） |
| Unix 套接字 | 见 2.3 | 只放行私有 TMPDIR 内的套接字 |
| DNS | 不可用 | 不可用 |
| 执行计划记录的网络模式 | `none` | `full`（理由见下） |

**macOS 放行回环的后果**：
- 本地服务端测试能在 macOS 上运行。
- 宿主上的本地代理（例如 Stash 的 127.0.0.1:7890）本来也可达，命令可以经它访问外网，绕过 ADR 0113 的逐条批准。为此加固（用户决策）：
  - 启动时从环境变量（`HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 及小写形式）和 macOS 系统代理设置中取得代理端口；
  - 在 Seatbelt 规则中单独拒绝连接这些回环端口，其余回环端口照常放行；
  - 代理端口集合计入 `SandboxBindingV2.profile_digest`，代理设置变化后计划失效、重新规划。
- 加固堵不住的：未登记在环境变量或系统设置中的代理（例如手动在其他端口开的代理），以及本机数据库、Docker Desktop 接口、本地模型服务等。
- 因此 Harnessix 仍不能证明 macOS 沙箱断网，执行计划如实记为 `full`，保证清单写明“非回环出站被拦截；已配置的代理端口被拦截；其余回环可达”。

**其他约定**：
- 代理环境变量不传入沙箱（2.2），工具不会自动走本地代理。但命令仍可显式指定代理，所以这只能防误用，不构成隔离。
- 命令需要联网（安装依赖、下载）时，改用 ADR 0113 的沙箱外命令，每次由用户批准。
- 本机代理加域名白名单（让常用包源免批准联网）本期不做，以后另议。

### 2.5 进程隔离与资源

- **bubblewrap**：
  - PID、IPC、UTS 命名空间隔离；新会话；不保留任何能力。
  - 命令看不见、也无法向 Harnessix 或宿主其他进程发信号。
  - `--die-with-parent` 加 PID 命名空间：监管进程终止 bwrap 时，命令的所有后代一并终止。
- **Seatbelt**：
  - 只允许向同一沙箱内的进程发信号、读进程信息，因此读不到 Harnessix 进程的环境变量和参数。
  - Mach 服务只放行固定的最小清单，不含 LaunchServices、钥匙串（SecurityServer）、剪贴板和 AppleEvents。
  - 子进程继承沙箱且无法解除。
  - 命令用 `setsid` 脱离进程组后不会被自动清理，但仍受同一沙箱约束（第 4 节）。
- **资源**：沿用超时、输出上限和进程组终止。本期不设内存与进程数硬上限，Codex、Claude Code 同样没有。

### 2.6 执行计划与资源声明

- **能力证据**（`ExecutionCapabilityEvidenceV2`）：
  - provider 为 `seatbelt` 或 `bubblewrap`；
  - 级别为 `host_sandboxed`；
  - 网络模式：bubblewrap 为 `none`，Seatbelt 为 `full`；
  - 不支持后台进程，支持进程树；
  - `provider_evidence_digest` 为探测 v2 的摘要。
- **`SandboxBindingV2.profile_digest`** 覆盖以下内容，任何一项变化都会让计划失效：
  - Profile 摘要；
  - 解析后的文件视图（可写目录真实路径、拒读清单、私有缓存路径）；
  - 后端身份；
  - POSIX Owner 能力摘要；
  - 环境。
- **资源声明**：
  - 进程执行（含 `cmd`）；
  - 工作区读或写（`mount` 为后端名）；
  - 每个 `write_roots` 一项 `external` 写资源；
  - Seatbelt 另声明一项网络连接资源（策略为回环）。现有策略要求网络模式非 `none` 时必须声明网络资源。
- **工具身份**：沿用 `exec_command.<profile_id>` 与 `product.command-profile.<profile_id>`。工具说明如实写明限制，模型不必靠试错发现：
  - 无外网；
  - `.git` 只读，提交由 Harnessix 交付流程完成；
  - 写入范围；
  - 需要联网时改用沙箱外命令。
- **审批**：沙箱内命令由宿主以系统身份自动批准，不逐条询问（ADR 0113 第 2.1 节）。

### 2.7 能力探测与保证清单

- 新增 `harnessix.host-sandbox-probe/v2`；v1 合同和 `spec/host-sandbox-probe-v1.schema.json` 保持不变。
- v2 记录：
  - 平台、后端、可执行文件身份与版本；
  - 原因码；
  - 当前环境实际生效的保证项（下表）。
- 预检用与正式执行相同形状的策略运行 `/usr/bin/true`。

| 保证项 | seatbelt | bubblewrap | 沙箱外命令（ADR 0113，对照） |
|---|---|---|---|
| 拒读凭据、历史与 Harnessix 状态 | 是 | 是 | 否 |
| 写入限于工作区、私有 TMPDIR 与缓存目录、`write_roots` | 是 | 是 | 否 |
| `.git` 只读 | 是 | 是 | 否 |
| 工作区 `.env*` 拒读 | 是，且磁盘上所有 `.env*` 都拒读 | 仅工作区内启动前已存在且被查到的文件 | 否 |
| 访问外网 | 不能直接访问；已配置的代理端口被拦截，未登记的本地代理仍可能被利用 | 不能 | 能 |
| 访问宿主本地服务 | 能（回环放行） | 不能 | 能 |
| 宿主 Unix 套接字 | 仅私有 TMPDIR 内 | 常见位置不可达，其余位置理论可达 | 能 |
| 看见、发信号或读取 Harnessix 进程 | 不能 | 不能 | 能（同一用户） |
| 命令结束后清理后代 | 进程组；`setsid` 脱离者不清理，但仍在沙箱内 | PID 命名空间整体终止 | 进程组 |
| 内存、进程数硬上限 | 无 | 无 | 无 |

### 2.8 诊断

- `harnessix code doctor` 输出以下内容，不新增顶层命令：
  - 探测 v2；
  - 保证清单；
  - 拒读清单与可写位置；
  - 原因码和修复提示。
- 修复提示举例：
  - 未安装 bubblewrap，给出安装命令；
  - 用户命名空间被系统策略禁用（例如 Ubuntu 23.10 起可用 AppArmor 限制非特权用户命名空间）；
  - Seatbelt 嵌套。

### 2.9 对 ADR 0066 的修订（实现合入时写回 0066 第 2 版）

- **第 3 条改为**：`host_sandboxed` 仅在以下后端通过预检时可用，每个后端实际生效的保证项写入能力报告；Windows 不宣称原生强隔离。
  - macOS Seatbelt；
  - 能创建用户命名空间的 Linux 宿主机上的 bubblewrap。
- **第 5 条补充**：
  - `none` 只能由网络命名空间或禁用的容器网络实现。
  - 放行宿主回环的 Seatbelt 沙箱不得记为 `none`，应记为 `full`，并在能力报告中列出“非回环出站被拦截”。
- **失败语义补充**：增加第 5 节的原因码。

## 3. 后置：容器内 Landlock + seccomp

- 本期不实现。Harbor 评测以任务容器为边界（ADR 0121），不需要它。
- 2026-11-06 在 247 默认 Docker 容器内的实测结论保留，供以后参考：
  - Landlock ABI 1 读写白名单可用；
  - seccomp 能只放行 `AF_UNIX` 套接字。
  - 局限一：ABI 1 不管具名 Unix 套接字的 `connect`。
  - 局限二：跨目录改名返回 `EXDEV`。
  - 局限三：无法在已授权目录中遮盖单个文件。
- 需要时另立 ADR（编号在 0114–0119 内），届时在 `backend` 字段增加取值。

## 4. 取舍

- **全盘可读加拒读清单，而不是白名单**（用户决策）：
  - 换来开箱即用：工作区外的同级目录（如 monorepo 中的 `../shared-lib`）、装在 HOME 下的工具链和用户配置都能直接用。
  - 代价一：清单外的文件（其他项目源码、清单未列的个人文件）可被命令读到，并随工具结果进入模型服务商。
  - 代价二：拒读清单需要持续维护。
  - Linux 上命令不能联网，外传途径只剩模型服务商；macOS 上已配置的代理端口被拦截，未登记的本地代理仍是残余途径。
- **macOS 放行回环并拦截已配置的代理端口**（用户决策）：换来本地服务端测试可用、最常见的经代理外连被堵住；代价是 macOS 沙箱仍不能证明断网，计划记为 `full`。
- **`sandbox-exec` 已被苹果标为弃用，但仍可用**，主流编码智能体同样依赖它。若将来被移除，macOS 退回容器 Profile 或沙箱外命令。
- **bubblewrap 的 `.env*` 遮盖只覆盖工作区内启动前已存在且被查到的文件。** 命令自己新建的文件本来就是它自己写的，不构成泄露。
- **不修改 `ProcessSpec` argv 上限**；超出即拒绝，实测不够时再商定。
- **Seatbelt 不清理 `setsid` 脱离者**：这些进程仍受沙箱约束，全面清理需要额外的进程跟踪，本期不做。
- **残余风险**：工作区内被忽略的可执行内容（如 `.venv/bin/activate`、`node_modules/.bin`）可被命令改写，之后在沙箱外运行时不受保护，且不出现在 Git 差异中。主流产品同样未处理，本期如实声明。

## 5. 失败语义

| 时机 | 原因码 | 结果 |
|---|---|---|
| 加载配置 | Profile 校验失败（可写目录非法或与拒读清单冲突、非沙箱后端携带沙箱字段、保留环境变量被覆盖） | 配置拒绝加载 |
| 探测、装配 | `native_backend_platform_mismatch`、`seatbelt_not_installed`、`seatbelt_nested_unavailable`、`seatbelt_preflight_failed`、`bubblewrap_not_installed`、`bubblewrap_namespaces_denied`、`bubblewrap_preflight_failed`、`sandbox_root_overlaps_denied`、`sandbox_policy_too_large`、`process_owner_unavailable` | 不提供沙箱命令工具，归入 ADR 0066 的 `sandbox_unavailable`。零配置下按 ADR 0113 第 2.3 节明示改为逐条批准的沙箱外命令；显式配置下省略该工具并在诊断中给出原因 |
| 启动前 | `sandbox_backend_changed`（后端身份漂移）、`workspace_secret_files_unmaskable` | 命令不执行，记为启动前失败 |
| 后端在执行命令前失败 | 沿用 `process_launch_failed` | 不改为无沙箱执行；如何与命令自身失败区分，实现时在两个平台实测确定 |

不静默降级：沙箱不可用时，任何无隔离执行都必须让用户看见，并逐条批准（ADR 0113）。

## 6. 验收

| 环境 | 后端 | 说明 |
|---|---|---|
| E-linux | bubblewrap | 247 宿主机。以 root 登录后用 `setpriv` 把测试进程降权为系统已有的 `nobody` 用户运行，不新建账号、不改系统配置；只把我们自己的测试目录 `/data/harnessix/sandbox-dev/` 下的一个子目录交给 `nobody` 读写 |
| E-linux-denied | bubblewrap | 247 上临时 Docker 容器，命名空间被拒，验证零配置改为逐条批准的沙箱外命令；用完删除我们创建的容器 |
| E-mac | Seatbelt | 本机，由用户在普通终端执行验收命令 |

- **A 安全**：
  - 拒读清单读不到；
  - 写入限于允许位置；
  - `.git` 与真实 HOME 写入失败；
  - `.env*` 遮盖；
  - Linux 外连失败、宿主回环不可达；
  - macOS 非回环外连失败，经已配置的代理端口外连失败，其余回环端口可连；
  - 宿主 Unix 套接字不可达；
  - 无法向 Harnessix 发信号或读取其环境；
  - 代理变量与模型凭据不进入命令环境。
- **B 兼容**（零配置下）：
  - `git status`/`diff`/`log`；
  - Python + pytest、Node、Go/Cargo 构建，验证缓存落在私有缓存目录、用户镜像配置生效；
  - monorepo 同级目录引用；
  - worktree 工作区；
  - macOS 本地服务端测试。
- **C 失败关闭**：
  - 后端缺失、命名空间被拒、Seatbelt 嵌套；
  - 后端身份漂移；
  - 可写目录冲突、策略过大、`.env*` 过多。
- **D 恢复**：命令执行中重启 Harnessix，按账本对账、不重放、清理残留的私有 TMPDIR。
- **E 开销**：记录每条命令的额外耗时（p50/p95）和大仓库上 `.env*` 查找的耗时，本期不设门槛。
- 证据放在 `docs/validation/native-sandbox-<日期>/`。

## 7. 实施顺序

1. 探测 v2 与合同：
   - Profile 字段与 `host` 后端；
   - `SandboxBinding` 规则；
   - 文件视图生成函数；
   - 环境变量白名单；
   - 保证清单。
2. bubblewrap 后端：在 247 宿主机测试，只跑单元测试和沙箱用例。
3. Seatbelt 后端：本机跑单元测试，由用户在普通终端完成验收。
4. ADR 0113：审批策略 v2、沙箱外命令；零配置装配；诊断。
5. 验收；回写 ADR 0066 第 2 版、`docs/modules/sandbox.md` 与 `docs/modules/product-config.md`。

## 8. 决策记录

### 8.1 用户已拍板（2026-11-07）

| 事项 | 决定 |
|---|---|
| 工作区语义 | 命令直接写工作区，改动保留（ADR 0112） |
| 后端范围 | macOS Seatbelt + Linux 宿主机 bubblewrap；Landlock 后置 |
| 文件读取 | 全盘可读加拒读清单（2.3） |
| macOS 回环 | 放行（2.4） |
| 网络 | 沙箱内不能访问外网；需要联网时走逐条批准的沙箱外命令（ADR 0113） |
| 审批 | 沙箱内命令自动执行，越界才问（ADR 0113） |
| 沙箱不可用 | 命令仍可用，但每条需批准并标注“未隔离”（ADR 0113） |
| 配置 | 零配置（2.2） |
| 247 验收身份 | 用 `setpriv` 临时降权为 `nobody`（第 6 节） |
| 改动记录、`.git` | 不记录、整个只读（ADR 0112） |

### 8.2 用户已拍板：macOS 拦截已配置的代理端口（2026-11-07）

- 背景：放行回环后，沙箱内命令可以执行 `curl -x http://127.0.0.1:7890 …`，经本机代理访问外网，绕过“联网必须走沙箱外命令并经用户批准”。
- 决定：采用加固方案 A，做法与局限见 2.4 节。工作量约 0.5 天，并入实施第 3 步。
