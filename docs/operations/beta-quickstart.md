---
doc_type: deployment-design
status: current
version: 2
code_revision: a8a4a6c7f831de606baa7237436fcb64e8380b38
owners:
  - core
modules:
  - deployment
  - product_ui
related_adrs:
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_ui/test_workspace_trust.py
  - tests/product_ui/test_cost_estimate.py
supersedes: []
---

# Beta 快速上手

Harnessix Code 目前是预览版，不是正式商用版本，不提供任何担保。感谢你帮忙试用。

本页是最简步骤。完整的安装、配置、功能和排障说明见[用户手册](user-manual.md)。

## 1. 适用范围

需要 macOS 或 Linux、Python 3.12 以上，以及 [uv](https://docs.astral.sh/uv/)。请在你有权发送给模型服务商的仓库里使用，并确保改动可以通过 Git 回退。

## 2. 部署步骤

**安装：**

```bash
uv tool install harnessix
```

国内下载慢时加镜像：`uv tool install harnessix --index-url https://mirrors.huaweicloud.com/repository/pypi/simple`。镜像同步新版本可能有数小时延迟。

**配置模型：**

```bash
harnessix code configure      # 按提示选服务商（DeepSeek、阿里云百炼、智谱）和模型
```

完成后按提示设置 API Key，例如百炼：`export DASHSCOPE_API_KEY=你的Key`（DeepSeek 用 `DEEPSEEK_API_KEY`，智谱用 `ZAI_API_KEY`）。在脚本里配置时用 `--vendor`：

```bash
harnessix code configure --vendor bailian --non-interactive   # deepseek、bailian 或 zhipu
```

自己部署的模型见[接入自托管模型](self-hosted-models.md)。

需要经代理访问模型服务时，设置 `HTTPS_PROXY`（遵守 `NO_PROXY`），或在配置时加 `--proxy-url http://127.0.0.1:端口`（优先）。只支持 HTTP 代理；本机和内网自托管模型总是直连。

**检查并启动：**

```bash
harnessix code doctor /path/to/repo --probe-model
harnessix code /path/to/repo
```

首次打开目录时需要确认信任。常用按键：`Ctrl+A` 审批，`Ctrl+E` 切换自动接受修改，`Ctrl+X` 取消，`Ctrl+S` 追加说明，`F1` 帮助，`Ctrl+Q` 退出。

## 3. 数据去向

- 代码和对话只发送给你配置的模型服务商。
- Harnessix 没有遥测，不会向项目方发送任何数据。
- API Key 只从环境变量读取，不写入配置文件。
- 会话数据保存在本机，默认位于 `~/.harnessix`。完整说明见[隐私说明](../../PRIVACY.md)。

## 4. 已知限制

- 还不支持 Hooks，没有 IDE 集成。Skills 见[使用 Skills](skills.md)，MCP 见[使用 MCP](mcp.md)。
- 云模型显示的是费用上限（未扣除缓存折扣），不是账单。
- 单个任务默认最多 100 步、1 小时，等待审批的时间也计入。交互模式不限累计 Token（费用未知的云模型限 300 万），`harnessix exec` 默认限 300 万（本地模型 600 万），也可用 `--max-cost` 按金额限制。
- 网络中断导致费用未知时，交互模式会停止，发送新消息即可继续。
- 沙箱内的命令不能联网。需要联网时会申请在沙箱外运行，由你逐条批准。

更多安全边界见 [SECURITY.md](../../SECURITY.md)。

## 5. 反馈方式

每个任务结束后，请记录以下 6 项：

| 项目 | 示例 |
|---|---|
| 任务 | 修复登录超时的缺陷 |
| 结果 | 直接可用／修改后可用／放弃 |
| 人工干预次数 | 2 |
| 耗时 | 25 分钟 |
| 费用 | 约 0.5 元 |
| 最糟的一点 | 反复读同一个文件 |

请不要在反馈中附带密钥、客户数据或未公开的源码。诊断文件不含这些内容。遇到数据损坏、越权操作或凭据泄露，请立即停止并告诉我们。

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| 配置时提示配置目录的权限不是 700 | 按提示里给出的命令修正（如 `chmod 700 ~/.harnessix`），并确认目录属于当前用户 |
| `doctor --probe-model` 失败 | 检查 Key、`--base-url` 和模型名；需要代理时设置 `HTTPS_PROXY=http://…`，只设了 SOCKS 代理不会生效 |
| 自托管模型提示 Key 太短 | 使用至少 16 字节的 Key，见[接入自托管模型](self-hosted-models.md) |
| 自托管模型很快报错或回答被截断 | 检查模型的上下文窗口，Ollama 默认只有 4096，见[接入自托管模型](self-hosted-models.md) |
| Linux 上 `doctor` 提示沙箱不可用 | 安装 bubblewrap 并开启非特权用户命名空间；Ubuntu 24.04 起还要为 bwrap 配置 AppArmor 例外。沙箱不可用时，命令不会自动执行 |
| `harnessix exec` 提示工作区未被信任 | 确认仓库可信后加 `--trust-workspace` |
| 任务因费用未知而停止 | 发送新消息即可继续 |
| 任务以“会话内容已经放不进配置的上下文窗口”结束 | 退出后用 `harnessix code configure --context-window-tokens` 调大窗口（如 131072），重新打开即可在原会话继续；不能调大时按 `Ctrl+N` 新建会话 |
| 自己写的 Skill 没有生效 | 运行 `harnessix skills list`，会列出未被采用的文件和原因，见[使用 Skills](skills.md) |

## 7. 回退

用 Git 回退工作区改动，用 `uv tool upgrade harnessix` 升级，用 `uv tool uninstall harnessix` 卸载。卸载不会删除 `~/.harnessix` 中的配置和会话。

管理本机数据：`harnessix data list` 列出各工作区的数据，`harnessix data export` 导出会话，`harnessix data delete --workspace 路径` 按提示确认后删除。遇到问题时，`harnessix data diagnostics <仓库> --output diag.json` 生成不含代码和对话的诊断文件，可以直接发给我们。
