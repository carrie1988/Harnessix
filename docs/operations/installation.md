---
doc_type: deployment-design
status: current
version: 17
code_revision: 65d7323b12db782bbf62f109545058256037584e
owners:
  - core
modules:
  - deployment
  - cli
  - product_ui
related_adrs:
  - docs/adr/0062-local-first-v1-commercial-boundary.md
  - docs/adr/0063-windows-v1-platform-support.md
  - docs/adr/0064-agpl-and-commercial-dual-licensing.md
  - docs/adr/0079-preflight-and-native-read-port.md
  - docs/adr/0081-single-coding-agent-product-boundary.md
related_tests:
  - tests/governance/test_secret_scan.py
  - tests/governance/test_installed_product_acceptance.py
  - tests/governance/test_distribution_artifact_boundary.py
  - tests/product_config/test_product_state_restore.py
  - tests/governance/test_repository_policy.py
  - tests/unit/test_cli_license.py
  - tests/product_config/test_server_and_cli.py
  - tests/product_config/test_preflight.py
  - tests/product_config/test_wizard.py
  - tests/tools/test_windows_native_runtime.py
  - tests/product_ui/test_cli.py
  - tests/product_ui/test_app.py
supersedes: []
---

# Harnessix Code安装与制品

## 1. 适用范围

本文描述当前内部`1.0.0rc1`候选、源码开发、本地Wheel和开发命令镜像。独立Action HTTP/Worker容器已按ADR 0081退出产品边界。0.9.1b～d的产品入口、Configure、Doctor与Windows原生只读链已有对应全矩阵CI证据。仓库尚未发布正式PyPI包、平台安装器、自动更新器或签名制品，因此本文不把
“可以从源码运行”表述为“产品已经完成安装交付”。
后继Windows本地NTFS审批写入端口及专项已实现，见[验证报告](../validation/windows-native-file-transactions-2026-09-28-v1/README.md)；
这不是消费者目标OS、脱离源码安装、升级及独立Beta完成的证明。
后继[Git/Owner与完整恢复原生焦点](../validation/windows-private-state-2026-09-28-v1/README.md)
已完成有限场景验证；不能从本地锁定Wheel导入、离线Doctor或焦点通过推导三平台正式安装完成。

## 2. 前置条件

| 依赖 | 最低要求 | 用途 | 强制范围 |
|---|---|---|---|
| Python | 3.12 | Runtime与CLI | 全部 |
| `uv` | 能解析当前`uv.lock`的版本 | 锁定依赖、开发命令和构建 | 推荐源码工作流 |
| Git | 当前系统可用版本 | 获取源码；Coding Tool的Git能力 | 源码安装必需，Agent Git能力按需 |
| Docker兼容后端 | 能拉取固定镜像并运行非Root容器 | Container Sandbox与开发命令镜像 | 可选 |
| PostgreSQL | 17为兼容回归基线 | 旧Action数据迁移测试 | 产品运行不需要 |
| Provider SDK | `openai`或`anthropic`可选依赖 | `agent-server`模型调用 | 按Product Config选择 |
| Textual | `>=8.2,<9`，当前锁定8.2.8 | `harnessix code`全屏终端 | `tui` Extra |

Python约束来自[`pyproject.toml`](../../pyproject.toml)的`requires-python = ">=3.12"`。Python 3.12/3.13、
macOS、Windows和后端测试矩阵不等于所有平台的完整产品支持，详见[平台矩阵](platforms.md)。

## 3. 安装路径决策

```mermaid
flowchart TD
    Start[选择运行目标] --> Product{目标入口}
    Product -- 开发或源码审计 --> Source[锁定Revision并uv sync]
    Product -- 开发命令镜像 --> Image[按Dockerfile本地构建]
    Product -- Coding Agent --> Agent[源码安装含Provider Extra]
    Agent --> Configure[code configure生成配置]
    Configure --> Diagnose[code doctor离线预检]
    Diagnose --> Platform{macOS/Linux/Windows?}
    Platform -- macOS/Linux --> Posix[POSIX受管编码与审批执行]
    Platform -- Windows --> Win[Handle读取 受管Git与本地NTFS审批Patch]
    Win --> GitGate[原生焦点已验证；完整安装待R4验收]
```

当前没有可直接下载的官方二进制。任何第三方Wheel、镜像或安装脚本必须单独核对来源、Revision、许可证和摘要。

## 4. 源码开发安装

### 4.1 固定Revision

```bash
git clone https://github.com/carrie1988/Harnessix.git
cd Harnessix
git checkout <经过评审的40位提交>
```

正式部署不得直接追踪可移动的`main`。应记录提交、`uv.lock`摘要、Python版本和构建主机。

### 4.2 锁定全部依赖

```bash
uv sync --locked --all-extras --dev
```

`--locked`确保解析结果与[`uv.lock`](../../uv.lock)一致；`--all-extras`安装OpenAI、Anthropic、
OpenTelemetry和Textual可选依赖；`--dev`安装测试和静态检查工具。仅运行某一入口时可以安装更小依赖集，
但对应环境必须单独验收，不能借用全量开发环境结论。

仅验证源码终端入口时，显式选择`tui`以及目标Provider Extra；Wheel安装按第5节固定输入：

```bash
uv sync --locked --extra tui --extra openai
uv run harnessix code /srv/project --config /srv/harnessix-private/config.json
```

基础Wheel不会隐式安装Textual。缺少Extra时`harnessix code`输出稳定`tui_dependency_missing` JSON并退出2，不会
运行时联网下载。上述命令是源码工作流，不从尚未发布的PyPI项目安装；独立候选安装必须使用第5节的实际Wheel及原锁哈希。

### 4.3 安装后验收

```bash
uv run harnessix --help
uv run harnessix code --help
uv run harnessix code doctor --help
uv run harnessix code configure --help
uv run harnessix license
uv run python -c 'import harnessix; print(harnessix.__file__)'
make spec
make check
```

验收必须确认命令解析、许可证文本、导入位置、生成规格无漂移和全量门禁均成功。`harnessix --help`成功只证明
CLI可导入，不证明Provider、数据库、Workspace或Sandbox可用。

## 5. 本地Wheel与锁定安装输入

当前内部候选文件名为`harnessix-1.0.0rc1-py3-none-any.whl`，不是正式商用Release。
以已评审固定Revision为起点；以下独立验收目录必须尚不存在。`python`指前置条件中的Python 3.12解释器。
命令复用[规范Wheel工作流](../../.github/workflows/installed-product-acceptance.yml)的锁定依赖、
精确Wheel哈希及`--no-deps`，不依赖pip重新解析浮动版本。源码外环境不借用开发venv。

```bash
python -c "from pathlib import Path; Path('../harnessix-wheel-verify').mkdir(mode=0o700)"
uv export --locked --no-dev --all-extras --no-emit-project --format requirements.txt --output-file ../harnessix-wheel-verify/requirements.txt
uv build --offline --wheel --out-dir ../harnessix-wheel-verify/wheel
python -c "from pathlib import Path; import hashlib; r=Path('../harnessix-wheel-verify'); w=list((r/'wheel').glob('*.whl')); assert len(w)==1; p=w[0].resolve(); (r/'wheel-requirement.txt').write_text(p.as_uri()+' --hash=sha256:'+hashlib.sha256(p.read_bytes()).hexdigest()+'\n', encoding='utf-8')"
uv venv --python 3.12 ../harnessix-wheel-verify/venv
```

macOS/Linux：

```bash
cd ../harnessix-wheel-verify
uv pip install --python ./venv/bin/python --require-hashes --no-deps -r requirements.txt
uv pip install --python ./venv/bin/python --require-hashes --no-deps --offline -r wheel-requirement.txt
./venv/bin/python -I -m harnessix --help
./venv/bin/python -I -m harnessix code --help
./venv/bin/python -I -m harnessix license
./venv/bin/python -I -c "import harnessix; from importlib.metadata import version; print(version('harnessix')); print(harnessix.__file__)"
```

Windows PowerShell在相同公共构建步骤后执行：

```powershell
Set-Location ../harnessix-wheel-verify
uv pip install --python ./venv/Scripts/python.exe --require-hashes --no-deps -r requirements.txt
uv pip install --python ./venv/Scripts/python.exe --require-hashes --no-deps --offline -r wheel-requirement.txt
./venv/Scripts/python.exe -I -m harnessix --help
./venv/Scripts/python.exe -I -m harnessix code --help
./venv/Scripts/python.exe -I -m harnessix license
./venv/Scripts/python.exe -I -c "import harnessix; from importlib.metadata import version; print(version('harnessix')); print(harnessix.__file__)"
```

最后必须得到`1.0.0rc1`及独立venv中的导入地址。Help、版本、License与SDK依赖安装不代表模型调用、
真实编码或消费者Windows11验收通过。配置与离线Doctor按[产品运行手册](../m08-product-runtime-and-extensions.md)执行；发模型请求另需满足费用及认证门禁。

此处自算哈希证明安装字节与本地受控构建件一致，不是签名或官方来源证明。下载候选时应先核对可信发布
记录给出的独立SHA256及Revision；规范CI消费者校验构建Job摘要后才生成相同安装输入。
基础Wheel不隐式启用Provider、Observability或TUI；本例显式安装原锁全部Extras用于候选验收，
正式精简环境必须固定其自身安装输入并独立验证。

### 5.1 Wheel边界

- 当前包版本为`1.0.0rc1`，只用于预发行验收，不等于路线图1.0商用已完成；
- 没有Release签名、来源证明、SBOM或可复现构建声明；
- 没有PyPI发布证据；
- Wheel不携带外部`git`、搜索工具、容器后端或Provider凭据；
- Python Wheel可安装不等于Windows Coding Agent达到正式产品支持；原生读取、NTFS审批写入及业务恢复专项也不能替代Windows 11完整编码验收。

正式首发通道是单一Wheel。三个源码CI构建入口和`Makefile`的`supply-chain`使用
`uv build --offline --wheel --out-dir dist/secret-gate`，随后扫描全部受管源码及实际Wheel。
不额外构建或发布未认证的Sdist，不修改Secret扫描4096条Archive记录等原上限。
既有目录残留的其他制品仍由原扫描器严格检查，不隐式删除、忽略或豁免；正式构建使用干净检出。

### 5.2 源码制品与验证制品边界

首发正式通道仍只有Wheel；源码制品仅供显式开发/边界回归，不是当前CI正式构建通道。
Hatch源码制品继续包含源码、正式文档及验证README/Manifest等可读资料，排除
`docs/validation/**/artifacts`内已经构建的验证制品，并保留原Task Pack解答目录排除规则。
实际验证Wheel仍以原字节保留在Git及对应交付Manifest中，不删除、不重签、不改摘要。

需求来自`d470ca6`的实际CI失败：新归档验证Wheel被默认源码制品再次打包，形成
gzip→tar→验证Wheel→包内Task Pack tar层级，现行Secret扫描正确返回`scan_archive_depth_limit`，而非完成覆盖。
不提高扫描深度、不忽略扫描失败；分离源码与已构建输出才是边界修复。
仓库扫描仍以`git ls-files`覆盖原验证Wheel，发行物扫描仍覆盖每个实际产物。

[实际构建回归](../../tests/governance/test_distribution_artifact_boundary.py)在独立临时项目中
运行同一Hatch配置的离线源码构建，验证源码/正式安装文档/验证README仍存在、制品目录不存在；
同时让仓库扫描发现原验证Wheel并捕获其中的合成规则命中，证明该排除不是Secret扫描白名单。
真实发行物完整扫描未通过前，不宣称源码或Wheel已通过发布门禁。

### 5.3 当前源码外安装与完整恢复证据

固定`1bc3794`Wheel已在macOS ARM64/Python3.12.7全新环境中安装；实际依赖从锁文件导出，
以精确哈希及`--no-deps`安装，执行工作目录与源码分离，正式产品由`python -I -m harnessix agent-server`启动。
实际SDK握手、Thread持久化、活跃Owner备份拒绝、停机六库及独立Key备份/验真、整体Root恢复、
原Key保留与相同restore ID不回退新状态均通过。数据和Key未进入公开交付。
详见[安装与恢复原件](../validation/product-restart-release-boundary-2026-09-29-v1/README.md#6-脱离源码安装与完整状态恢复)。
该结果没有真实模型Turn，不证明真实编码任务、Linux/Windows安装、版本升级、卸载或Beta。
三平台消费者安装验收、正式来源证明和1.0发行仍开放；开发Wheel不可称为已发布商用包。

后继[统一安装生命周期详设](../changes/m09-r4-installed-product-acceptance.md)将正式SDK/CLI恢复流程
推广至三平台独立Job，并新增指定venv卸载、全新解释器导入消失、原Key/库/Workspace保持和同一Wheel重装读取。
执行始终使用安装解释器`-I -m harnessix`，不把构建Checkout放入产品导入路径。
三平台实际结果、版本升级及消费者目标OS未验收前仍不关闭R4。

[当前三平台独立生命周期实测](../validation/installed-product-three-platform-2026-09-29-v1/README.md)
已完成源码外安装、完整备份恢复、指定venv卸载与同Wheel重装；原Key、库、备份和Workspace保持。
三平台各自构建，Windows摘要不同；规范单一发行Wheel及消费者目标OS、真实编码和版本升级仍需验收。

后继[规范Wheel工作流](../../.github/workflows/installed-product-acceptance.yml)改为Ubuntu构建/扫描一次，
同一Run的三个平台下载相同Artifact，安装前校验构建Job的原字节SHA256；消费者不重复构建。
仍使用原隔离安装、正式CLI/SDK及七文件低敏证据路径，未修改产品、状态权限或恢复合同。
源码实现不替代实际运行结果；规范制品跨平台通过与版本升级、Windows11及真实编码分别验收。

[规范Wheel三平台实际结果](../validation/canonical-wheel-three-platform-2026-09-29-v1/README.md)
绑定`a4f7f33`：唯一构建件在三个原生Job完成上述生命周期，三份安装摘要一致。
当前验证包仍为`0.1.0`；未发布1.0，也未完成版本升级、消费者Windows11或真实编码验收。

[不同版本升级、恢复与回退原件](../validation/different-version-upgrade-2026-09-29-v1/README.md)
已固定原`0.1.0`与`1.0.0rc1`、唯一规范Wheel及三平台实际消费者Job；原Key与六库匹配恢复通过。
这是固定`ec356aa`的安装生命周期切片，不包含后继源码的完整商用验收，Windows11与真实编码/Beta仍开放。

## 6. 开发命令镜像

```bash
docker build --pull --tag harnessix:<revision> .
docker run --rm harnessix:<revision>
```

[`Dockerfile`](../../Dockerfile)基于`python:3.12-slim`，使用UID 10001非Root用户并默认执行
`harnessix --help`。镜像不声明服务端口、Action数据库Volume或HTTP/Worker启动命令，也不构成正式Coding Agent发行物。
运行真实Agent仍需显式提供Workspace、Product Config、Provider Extra和受控状态目录；Container Sandbox的被执行镜像与
该开发命令镜像是两个不同信任域。

正式Agent容器镜像延期1.1+，不作为1.0发行物；未来发布前仍需上述装配、来源、资源、状态卷及升级验证。受管Container Sandbox后端不因镜像延期而取消。

## 7. 目录与权限

| 对象 | 建议位置 | POSIX权限 | 禁止事项 |
|---|---|---|---|
| 源码Checkout | 只读或受控构建目录 | 按构建用户 | 与运行状态混放 |
| 旧Action SQLite | 隔离归档目录 | 只读、最小权限 | 由当前产品自动消费或删除 |
| Product Config | Workspace外私有目录 | 文件`0600` | Symlink、多硬链接、组/其他可读 |
| 产品客户端状态根 | 默认用户级`.harnessix/workspaces/<workspace-fingerprint>`或显式目录 | 目录`0700` | 与Workspace互相包含、多个产品进程共享写入 |
| Agent Runtime状态目录 | 客户端状态根的`runtime/`子目录 | 目录`0700` | 与客户端状态文件混成同一Schema、位于Workspace内 |
| Workspace | 独立用户仓库 | 由仓库所有者控制 | 存放Provider Secret或状态数据库 |

产品入口会在POSIX校验Product Config和状态目录。旧Action SQLite不进入产品启动链，只能按迁移说明在隔离维护环境读取。

## 8. 更新与卸载

更新前先按[升级与回退](upgrade-and-rollback.md)建立一致备份并完成目标Revision兼容检查。源码环境更新不得直接
`git pull && uv sync`覆盖正在运行的进程；应构建新环境、停流量、停止旧进程、迁移副本数据、验收后再切换。

卸载程序不等于删除状态。清理前必须区分源码/虚拟环境、旧Action归档、Agent Session、配置审计、Workspace和
外部效果系统。没有经过保留期与审计批准，不得删除Journal、Session或验证证据。

## 9. 安装验收矩阵

| 场景 | 必须执行 | 通过标准 |
|---|---|---|
| 源码开发 | `uv sync --locked --all-extras --dev`、`make check` | 锁定依赖、静态检查和全量测试通过 |
| 基础Wheel | 全新环境安装、`harnessix --help`、`license` | 不依赖源码目录也能导入和运行命令 |
| Provider Wheel | 安装目标Extra、`code configure`、`code doctor --json` | 配置原子生成，SDK、Secret引用、Workspace和平台检查通过 |
| TUI源码/Wheel | 安装`tui` Extra、`harnessix code --help`、无头UI和真实stdio恢复 | Textual可导入，会话/输入/恢复/关闭合同通过 |
| 开发命令镜像 | 非Root身份、默认Help命令、无监听端口 | 启动不进入已退役Action服务 |
| 升级 | 旧版本建库、新版本迁移、重开、旧Reader拒绝 | 旧字节和失败语义符合合同 |
| 平台 | 对应CI与原生Dogfooding | 文件、进程、终端、Git和取消矩阵通过 |

## 10. 源码与测试映射

| 设计元素 | 源码 | 验证 |
|---|---|---|
| 包与Extra | [`pyproject.toml`](../../pyproject.toml) | [`tests/governance/test_repository_policy.py`](../../tests/governance/test_repository_policy.py) |
| 锁定依赖 | [`uv.lock`](../../uv.lock) | [CI workflow](../../.github/workflows/ci.yml) |
| CLI入口 | [`src/harnessix/cli.py`](../../src/harnessix/cli.py)的`main` | [`tests/unit/test_cli_license.py`](../../tests/unit/test_cli_license.py) |
| TUI Extra与产品入口 | [`pyproject.toml`](../../pyproject.toml)、[`src/harnessix/product_ui/cli.py`](../../src/harnessix/product_ui/cli.py)的`code_main` | [`tests/product_ui/test_cli.py`](../../tests/product_ui/test_cli.py)、[`test_app.py`](../../tests/product_ui/test_app.py) |
| 产品配置与Doctor | [`src/harnessix/product_config/wizard.py`](../../src/harnessix/product_config/wizard.py)、[`preflight.py`](../../src/harnessix/product_config/preflight.py) | [`test_wizard.py`](../../tests/product_config/test_wizard.py)、[`test_preflight.py`](../../tests/product_config/test_preflight.py) |
| 产品启动 | [`src/harnessix/product_config/server.py`](../../src/harnessix/product_config/server.py)的`run_product_stdio` | [`tests/product_config/test_server_and_cli.py`](../../tests/product_config/test_server_and_cli.py)、[`test_windows_native_runtime.py`](../../tests/tools/test_windows_native_runtime.py) |
| 镜像边界 | [`Dockerfile`](../../Dockerfile) | [`tests/governance/test_product_runtime_convergence.py`](../../tests/governance/test_product_runtime_convergence.py) |

## 11. 未完成的制品治理

首发按[范围收敛计划](../changes/m09-to-v1-release-scope-convergence.md)采用统一Wheel通道和有限三平台目标：
固定版本、精确安装输入、来源/校验、实际发行物SBOM/依赖/Secret检查、脱离源码安装、手动升级及同机恢复必须通过。
MSI/DMG、多包管理器、自动更新、额外架构和正式Agent镜像延期1.1+，不能被误列为首发前置条件。
当前Wheel仍是开发/候选制品，不因计划收敛自动变为正式商用发行物。

## 默认产品首次启动与持久密钥

`agent-server`及TUI托管Server在独立私有状态中创建`session-auth/key.v1`；稳定Store ID/Key ID与32字节认证Key
独立随机生成，不从Provider API Key派生。模型配置不增加原Key正文，安装配置与诊断包不得包含密钥文件。
macOS/Linux检查Owner、规范权限和对象身份，Darwin额外拒绝扩展ACL；Windows实现用户DPAPI/私有DACL，
原生测试与三平台正式安装仍须独立验收。POSIX私有Key文件不声称密文或不可导出Keyring。
仅全新无库状态可以生成新Key。存在原DB/WAL/SHM而无Key、未证明历史、Key格式或权限失效均拒绝启动；
不得通过清空原状态、改权限追认旧事实、生成替代Key或手工补签解除门禁。
Doctor成功只代表配置及能力预检，不证明原Key/Session有效。完整产品备份与原来源验真见下文；
同机整体状态恢复见本手册末节；跨机器Key迁移继续延期。
新Artifact正文在同一逻辑Store/Key重启后依赖Migration 0030原行Seal恢复，旧NULL行不能因此被读取；
安装与升级应将数据库/WAL一致状态和独立Key作为同一保留单元，不能只复制数据库或重建Key。
不把复制DB或本地Wheel消费者视为安装与恢复完成。
[总体与详细设计](../changes/m09-4a-managed-session-key-and-root.md)、
[Artifact原行来源认证](../changes/m09-4a-authenticated-artifact-body.md)、
[固定macOS验证](../validation/managed-session-key-2026-09-28-v1/README.md)与[升级边界](upgrade-and-rollback.md)是当前操作依据。

## 完整状态停机备份与只读验真

停止使用同一状态目录的产品宿主和嵌入式Writer，在已存在的受信父目录下选择新备份目录。
目标不得与产品状态、根外信任锚点或任何原认证Thread的Workspace重叠；命令不覆盖已有目标。

```bash
harnessix state backup --state-directory "$STATE_DIRECTORY" \
  --backup-directory "$BACKUP_DIRECTORY" --timeout 120
harnessix state verify --state-directory "$STATE_DIRECTORY" \
  --backup-directory "$BACKUP_DIRECTORY" --timeout 120
```

备份保存完整受管库、原Key、事务Blob和可选Process事实。制品与原Root外私有信任锚点共同保留，
不加入Git或普通诊断包；仅制品自带Key不足以取得原来源授权。
整体恢复使用`state restore/recover`，见下节；不得逐库覆盖或用旧单库维护接口代替。
POSIX完整产品验证与Windows原生端口测试分开，源码存在和Wheel可安装不代表三平台商用支持。
[完整设计](../changes/m09-r1-product-state-backup.md)、[固定验证资料](../validation/product-state-backup-2026-09-28-v1/README.md)
与[恢复手册](recovery.md)分别说明操作合同、实际证据和仍开放的恢复要求。

## 完整停机恢复与安装前未决状态

先停止使用原Root的产品实例，保存原备份、根外锚点及显式恢复UUID，执行
`harnessix state restore --state-directory "$STATE_DIRECTORY" --backup-directory "$BACKUP_DIRECTORY"
--restore-id "$RESTORE_ID" --confirm-backup "$BACKUP_ID"`（命令参数写在同一行）。
指针未决时产品拒绝启动，不在缺失窗口生成新Key；必须以原UUID执行
`state recover --confirm-restore "$RESTORE_ID" --mode complete`或`--mode rollback`，并提供原状态目录。
两个方向不能顺序执行。恢复成功保留Previous，当前不自动删除。
[完整操作与失败说明](recovery.md#完整产品停机恢复与明确结算)及
[正式设计](../changes/m09-r1-product-state-restore.md)定义原来源、取消、确认丢失和平台边界。

## 原生Windows Git读取验收入口

安装候选可显式绑定本机Git for Windows绝对EXE后运行Doctor及固定Status/Diff。
Native SDK、取消、签名观察、输出保护和原备份验证入口见
[Windows Git读取详设](../changes/m09-r4-windows-native-git-read.md)。
目前仅增加实现候选，不宣称脱离源码Wheel安装、三平台升级恢复或完整商用发布已通过。
