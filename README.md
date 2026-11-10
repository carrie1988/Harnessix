# Harnessix Code

**本地运行、模型自选、每一步都受控的 Coding Agent。**

Harnessix Code 在你的终端里读代码、改代码、跑测试。改动要你批准，命令默认进沙箱，崩溃后能恢复，全过程可审计。模型由你选择，可以是云服务，也可以是你自己部署的模型。

> **当前是预览版，不是正式商用版本，不提供任何担保。** 请在可以通过 Git 回退的仓库里使用。

## 快速开始

需要 macOS 或 Linux、Python 3.12 以上，以及 [uv](https://docs.astral.sh/uv/)。

```bash
# 1. 安装（把地址换成发布页上的 wheel）
uv tool install <wheel 地址或本地文件>

# 2. 配置模型
export MODEL_API_KEY=你的Key
harnessix code configure --provider-kind openai_chat \
  --base-url https://api.deepseek.com/v1 --model deepseek-v4-pro --non-interactive

# 3. 检查并启动
harnessix code doctor /path/to/repo --probe-model
harnessix code /path/to/repo
```

- 使用百炼等其他服务，或完整的上手说明，见 [Beta 快速上手](docs/operations/beta-quickstart.md)。
- 使用自己部署的模型（vLLM、Ollama，同机或局域网），见[接入自托管模型](docs/operations/self-hosted-models.md)。
- 无人值守运行：`harnessix exec --trust-workspace --prompt "..." --approval-mode auto-edit`。

## 它能做什么

| 能力 | 说明 |
|---|---|
| 代码读写 | 读取、搜索、多文件事务修改，修改前显示完整差异 |
| 运行命令 | 默认在 macOS Seatbelt 或 Linux bubblewrap 沙箱内自动执行，需要联网时逐条请你批准 |
| 审批 | 逐次批准；也可以在本会话自动接受修改，或允许完全相同的操作 |
| 恢复 | 进程崩溃或断线后可继续，同一副作用不会被盲目重放 |
| 上下文 | 按模型窗口自动压缩，读取仓库中的 `AGENTS.md` |
| Git | 查看状态和差异；你要求时，经批准后提交 |
| Skills | 按需加载个人或仓库里的任务说明，见[使用 Skills](docs/operations/skills.md) |
| 模型 | OpenAI 兼容接口与 Anthropic 接口；已预设 DeepSeek 和百炼，支持自托管模型 |
| 集成 | 终端界面、无头模式（JSON 输出）、stdio App Server、Python SDK |

**还没有：** MCP、Hooks 的产品入口，IDE 插件，Web 搜索，子代理，图片输入，自动更新。

## 安全边界

- 沙箱内的命令不能联网，不能写 `.git` 和工作区以外的位置，也读不到 `~/.ssh` 等凭据目录。
- 沙箱外的命令每条都需要你批准。
- 首次打开目录时需要确认信任。
- API Key 只从环境变量读取，并在输出和记录中按机密保护。

沙箱内命令能读取拒读清单以外的文件，这些内容可能发送给模型服务商。完整说明和已知限制见 [SECURITY.md](SECURITY.md)。

**数据去向：** 代码和对话只发送给你配置的模型服务商。Harnessix 没有遥测，不向项目方发送任何数据。详见[隐私说明](PRIVACY.md)。

## 评测结果

使用同一个本地模型（Qwen3.8-Flash-Next），在 SWE-bench Verified 的 49 题留出集上单次运行，官方判分：

| 系统 | 解决题数 |
|---|---|
| Harnessix（当前） | 27 |
| mini-swe-agent | 29（运行时能访问公网，其中至少 4 题直接抄了上游修复，扣除后约 25） |

这个结果目前只能说明两者接近，mini-swe-agent 还需要在断网条件下重跑。此外，这 49 题在调优过程中都被用于诊断，结果偏乐观；评测配置使用了 SWE-bench 任务模板；单次运行的波动也不小。SWE-bench 也不代表你的真实项目。

## 文档

- [Beta 快速上手](docs/operations/beta-quickstart.md)：安装、配置、反馈
- [配置参考](docs/operations/configuration.md)、[诊断](docs/operations/diagnostics.md)、[状态备份与恢复](docs/operations/recovery.md)
- [架构](docs/architecture.md)、[设计文档索引](docs/README.md)、[路线图](docs/roadmap.md)
- [威胁模型](docs/threat-model.md)
- [更新记录](CHANGELOG.md)
- [项目早期的里程碑记录](docs/readme-milestone-history.md)

## 参与和许可

社区版按照 [AGPL-3.0-only](LICENSE) 发布。闭源使用见[商业许可说明](COMMERCIAL_LICENSE.md)，品牌规则见 [TRADEMARKS.md](TRADEMARKS.md)，第三方依赖见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

贡献代码前请阅读[贡献指南](CONTRIBUTING.md)。发现安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要公开提 Issue。
