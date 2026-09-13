---
doc_type: deployment-design
status: current
version: 4
code_revision: 93723773676349fbfbe0ef42c26d9000cce379c8
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
related_tests:
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

本文描述代码Revision `93723773676349fbfbe0ef42c26d9000cce379c8`的源码安装、开发环境、本地Wheel和Action Plane容器路径。0.9.1b的既有路径已由
[CI 34721082419](https://github.com/carrie1988/Harnessix/actions/runs/34721082419)验证；0.9.1d新增Configure、Doctor与Windows
原生只读候选仍等待本轮全矩阵CI。仓库尚未发布正式PyPI包、平台安装器、自动更新器或签名制品，因此本文不把
“可以从源码运行”表述为“产品已经完成安装交付”。

## 2. 前置条件

| 依赖 | 最低要求 | 用途 | 强制范围 |
|---|---|---|---|
| Python | 3.12 | Runtime与CLI | 全部 |
| `uv` | 能解析当前`uv.lock`的版本 | 锁定依赖、开发命令和构建 | 推荐源码工作流 |
| Git | 当前系统可用版本 | 获取源码；Coding Tool的Git能力 | 源码安装必需，Agent Git能力按需 |
| Docker兼容后端 | 能拉取固定镜像并运行非Root容器 | Action Plane镜像和Container Sandbox | 可选 |
| PostgreSQL | 17为当前CI基线 | 多Worker共享Action Journal | queued生产拓扑 |
| Provider SDK | `openai`或`anthropic`可选依赖 | `agent-server`模型调用 | 按Product Config选择 |
| Textual | `>=8.2,<9`，当前锁定8.2.8 | `harnessix code`全屏终端 | `tui` Extra |

Python约束来自[`pyproject.toml`](../../pyproject.toml)的`requires-python = ">=3.12"`。Python 3.12/3.13、
macOS、Windows和后端测试矩阵不等于所有平台的完整产品支持，详见[平台矩阵](platforms.md)。

## 3. 安装路径决策

```mermaid
flowchart TD
    Start[选择运行目标] --> Product{目标入口}
    Product -- 开发或源码审计 --> Source[锁定Revision并uv sync]
    Product -- Action HTTP服务 --> Container{是否使用容器}
    Container -- 否 --> Source
    Container -- 是 --> Image[按Dockerfile本地构建]
    Product -- Coding Agent --> Agent[源码安装含Provider Extra]
    Agent --> Configure[code configure生成配置]
    Configure --> Diagnose[code doctor离线预检]
    Diagnose --> Platform{macOS/Linux/Windows?}
    Platform -- macOS/Linux --> Posix[POSIX只读Runtime]
    Platform -- Windows --> Win[Handle只读已验证；Git失败关闭]
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
OpenTelemetry、LangGraph和Textual可选依赖；`--dev`安装测试和静态检查工具。仅运行某一入口时可以安装更小依赖集，
但对应环境必须单独验收，不能借用全量开发环境结论。

仅验证终端入口时，在构建好的Wheel或源码包上显式安装`tui`以及目标Provider Extra：

```bash
python -m pip install 'harnessix[tui,openai]'
harnessix code /srv/project --config /srv/harnessix-private/config.json
```

基础Wheel不会隐式安装Textual。缺少Extra时`harnessix code`输出稳定`tui_dependency_missing` JSON并退出2，不会
运行时联网下载。上述命令只说明入口和依赖关系；当前没有PyPI发布证据，应使用本地Wheel或锁定源码安装。

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

## 5. 本地Wheel

构建系统由Hatchling声明，Wheel只包含`src/harnessix`包：

```bash
uv build --wheel
```

本地Wheel用于独立环境升级/兼容测试时，应在全新虚拟环境安装并记录文件摘要：

```bash
python -m venv ./wheel-verify
./wheel-verify/bin/python -m pip install ./dist/harnessix-0.1.0-py3-none-any.whl
./wheel-verify/bin/harnessix --help
```

Windows将第二、三行替换为虚拟环境的`Scripts`路径。基础Wheel不自动安装模型Provider、Observability、
LangGraph或TUI Extras；验收某项能力时必须显式安装相应Extra，并确认依赖解析没有越过项目上限。

### 5.1 Wheel边界

- 当前包版本为`0.1.0`，尚未与路线图0.9完成度建立正式发布映射；
- 没有Release签名、来源证明、SBOM或可复现构建声明；
- 没有PyPI发布证据；
- Wheel不携带外部`git`、搜索工具、容器后端或Provider凭据；
- Python Wheel可安装不等于Windows Coding Agent达到正式产品支持；0.9.1d仅验证原生四项只读能力。

## 6. Action Plane容器

### 6.1 构建

```bash
docker build --pull --tag harnessix:<revision> .
```

[`Dockerfile`](../../Dockerfile)基于`python:3.12-slim`，创建UID 10001的非Root用户，安装基础依赖和
`observability` Extra，把`/data`声明为Volume，并默认执行`harnessix serve`。

### 6.2 本地SQLite启动

```bash
docker run --rm \
  --name harnessix-action-plane \
  --publish 127.0.0.1:8787:8787 \
  --volume harnessix-data:/data \
  harnessix:<revision>
```

只发布到回环地址。当前HTTP API没有应用内认证和TLS，不得把`8787`直接发布到公网。

### 6.3 queued启动边界

API和Worker必须使用相同`HARNESSIX_DATABASE_URL`及`HARNESSIX_EXECUTION_MODE=queued`。数据库凭据通过
编排平台Secret注入，不放入镜像层、Compose文件或命令历史。Worker可通过覆盖容器命令启动：

```bash
docker run --rm \
  --env HARNESSIX_EXECUTION_MODE=queued \
  --env HARNESSIX_DATABASE_URL \
  harnessix:<revision> worker
```

当前镜像不包含OpenAI/Anthropic Extra，也没有内置Agent Workspace、Git或完整Container Sandbox装配，
不能作为`agent-server`正式镜像。

## 7. 目录与权限

| 对象 | 建议位置 | POSIX权限 | 禁止事项 |
|---|---|---|---|
| 源码Checkout | 只读或受控构建目录 | 按构建用户 | 与运行状态混放 |
| Action SQLite | 专用持久卷 | 目录`0700`、文件最小权限 | 网络文件系统、多主机并发共享 |
| Product Config | Workspace外私有目录 | 文件`0600` | Symlink、多硬链接、组/其他可读 |
| 产品客户端状态根 | 默认用户级`.harnessix/workspaces/<workspace-fingerprint>`或显式目录 | 目录`0700` | 与Workspace互相包含、多个产品进程共享写入 |
| Agent Runtime状态目录 | 客户端状态根的`runtime/`子目录 | 目录`0700` | 与客户端状态文件混成同一Schema、位于Workspace内 |
| Workspace | 独立用户仓库 | 由仓库所有者控制 | 存放Provider Secret或状态数据库 |

产品入口会在POSIX校验Product Config和状态目录，但Action Plane通用SQLite路径没有同等级的Owner/Mode入口校验；
部署层必须补足文件系统权限和运行用户隔离。

## 8. 更新与卸载

更新前先按[升级与回退](upgrade-and-rollback.md)建立一致备份并完成目标Revision兼容检查。源码环境更新不得直接
`git pull && uv sync`覆盖正在运行的进程；应构建新环境、停流量、停止旧进程、迁移副本数据、验收后再切换。

卸载程序不等于删除状态。清理前必须区分源码/虚拟环境、Action Journal、Agent Session、配置审计、Workspace和
外部效果系统。没有经过保留期与审计批准，不得删除Journal、Session或验证证据。

## 9. 安装验收矩阵

| 场景 | 必须执行 | 通过标准 |
|---|---|---|
| 源码开发 | `uv sync --locked --all-extras --dev`、`make check` | 锁定依赖、静态检查和全量测试通过 |
| 基础Wheel | 全新环境安装、`harnessix --help`、`license` | 不依赖源码目录也能导入和运行命令 |
| Provider Wheel | 安装目标Extra、`code configure`、`code doctor --json` | 配置原子生成，SDK、Secret引用、Workspace和平台检查通过 |
| TUI源码/Wheel | 安装`tui` Extra、`harnessix code --help`、无头UI和真实stdio恢复 | Textual可导入，会话/输入/恢复/关闭合同通过 |
| 容器 | 非Root身份、持久卷、Health/Readiness | 重启后状态保留，端口只按预期暴露 |
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
| 镜像 | [`Dockerfile`](../../Dockerfile) | [`tests/integration/test_api.py`](../../tests/integration/test_api.py) |

## 11. 未完成的制品治理

0.9后续必须补齐三平台正式安装器、版本通道、升级/回退编排、签名与验证、SBOM、来源证明、恶意依赖扫描、
离线安装策略和自动更新失败恢复。在这些门禁关闭前，源码安装和本地Wheel只用于开发、审计和受控候选验证。
