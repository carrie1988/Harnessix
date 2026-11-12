# Harnessix Code

**在本机终端运行、模型自选、每一步受控的 Coding Agent。**

Harnessix Code 根据任务描述自主完成读代码、改代码、运行测试并给出总结。文件修改须经用户批准，命令默认在系统沙箱内执行，进程崩溃后可恢复，全过程在本机留有记录。模型由用户自行选择，既可使用云服务，也可使用自行部署的模型。

> **当前为公开预览版（Beta），不是正式商用版本，不提供任何担保。** 请在可以通过 Git 回退的仓库中使用。

## 产品定位

- **面向对象：** 希望在本机代码仓库中使用 Coding Agent，同时对模型选择、执行权限和数据去向保持控制的开发者与团队。
- **典型任务：** 缺陷修复、功能修改、补充测试、代码阅读与排查。
- **使用方式：** 终端全屏界面交互使用，或以无头模式接入脚本与 CI。

## 核心特点

| 特点 | 说明 |
|---|---|
| 模型自选 | 不绑定任何模型或厂商：支持 OpenAI 兼容接口与 Anthropic 接口，云服务与自托管模型均可接入 |
| 每一步受控 | 修改文件前展示完整差异并等待批准；沙箱外的命令逐条批准 |
| 默认沙箱 | 命令默认在系统原生沙箱内执行：不能联网，不能写 `.git` 和工作区以外的位置，读不到凭据目录 |
| 可恢复、可审计 | 会话、审批和每一步操作都记录在本机；崩溃或断线后可以继续，已发生的副作用不会被盲目重放 |
| 本地优先 | 代码和对话只发送给用户配置的模型服务商；没有遥测，不向项目方发送任何数据 |
| 开放集成 | 提供终端界面、无头模式、stdio App Server 和 Python SDK，支持 Skills 与 MCP |

## 功能

| 功能 | 说明 |
|---|---|
| 代码读写 | 读取与搜索代码；多文件修改作为一个事务整体生效；可撤销 Agent 自己做过的修改 |
| 运行命令 | 沙箱内命令（如运行测试）自动执行；需要联网或写 `.git` 时申请在沙箱外执行，由用户批准 |
| 审批 | 逐次批准；可在本会话自动接受文件修改；沙箱外的只读命令不询问，并可按命令前缀保存“以后不再询问”规则 |
| 上下文 | 按模型上下文窗口自动压缩较早的内容；读取仓库中的 `AGENTS.md` 作为项目指令 |
| Git | 查看状态和差异；用户明确要求时，经批准后提交；不会推送 |
| 费用 | 已内置牌价的模型显示费用上限；无头模式可用 `--max-cost` 限制单个任务的费用 |
| Skills | 按需加载个人或仓库内的任务说明，见[使用 Skills](https://github.com/carrie1988/Harnessix/blob/main/docs/operations/skills.md) |
| MCP | 在本机启动用户逐个信任的 MCP Server，每次工具调用都需批准，见[使用 MCP](https://github.com/carrie1988/Harnessix/blob/main/docs/operations/mcp.md) |
| 集成 | 终端界面 `harnessix code`、无头模式 `harnessix exec`（输出 JSON 摘要）、stdio App Server、Python SDK |

**尚未提供：** Hooks、IDE 插件、Web 搜索、子代理、图片输入、自动更新。

## 平台与兼容性

| 项目 | 支持情况 |
|---|---|
| macOS | 支持；使用系统自带的 Seatbelt 沙箱，无需额外安装 |
| Linux | 支持；沙箱依赖 bubblewrap，安装方法见[用户手册](https://github.com/carrie1988/Harnessix/blob/main/docs/operations/user-manual.md) |
| Windows | 预览支持，暂不建议用于日常工作：没有本机沙箱，命令在 PowerShell 中执行，除只读命令和已保存规则的命令外逐条批准；需要沙箱时可在 WSL2 中按 Linux 方式使用 |
| Python | 3.12 及以上；通过 [uv](https://docs.astral.sh/uv/) 安装时自动准备，无需单独安装 |
| Git | 推荐安装；工作区位于 Git 仓库内时自动启用 Git 工具 |
| 模型（向导预设） | DeepSeek 官方、阿里云百炼（通义千问、DeepSeek）、智谱 GLM |
| 模型（其他云服务） | 兼容 OpenAI Chat Completions 接口的服务，以及 Anthropic 接口 |
| 模型（自托管） | 同机或局域网内的 OpenAI 兼容端点，需支持工具调用并在流式响应中返回用量；已实测 vLLM 和 Ollama，见[接入自托管模型](https://github.com/carrie1988/Harnessix/blob/main/docs/operations/self-hosted-models.md) |

沙箱不可用时不会静默降级：所有命令改为在沙箱外执行，并逐条要求批准。

## 安装与使用

**前置条件：** 已安装 [uv](https://docs.astral.sh/uv/)，并持有一个模型服务的 API Key（或自托管模型的端点地址）。

```bash
# 1. 安装（国内网络可追加 --index-url https://mirrors.huaweicloud.com/repository/pypi/simple）
uv tool install harnessix

# 2. 配置模型：按向导选择服务商和模型
harnessix code configure

# 3. 设置 API Key（变量名以向导提示为准，以下以 DeepSeek 为例）
export DEEPSEEK_API_KEY=你的Key
#    Windows PowerShell：$env:DEEPSEEK_API_KEY="你的Key"

# 4. 启动前检查：确认配置、沙箱、Git 和模型均可用
harnessix code doctor /path/to/repo --probe-model

# 5. 启动
harnessix code /path/to/repo
```

启动后的使用步骤：

1. **确认信任目录。** 首次在某个目录运行时需要确认，只信任自己的或来源可靠的代码。
2. **描述任务。** 在底部输入框输入任务并按 `Enter` 发送，例如“运行单元测试并修复失败的用例，不要修改测试文件”。
3. **审批。** 需要批准时审批窗口自动弹出，显示完整差异或待执行的命令，可批准或拒绝；拒绝时可以写明原因，Agent 会据此调整。
4. **查看结果。** 任务结束后 Agent 给出总结，用 `git diff` 检查改动。
5. **退出。** 按 `Ctrl+Q`；下次在同一目录启动时自动回到上次的会话。

常用按键：`Ctrl+A` 审批，`Ctrl+E` 切换自动接受修改，`Ctrl+S` 追加说明，`Ctrl+X` 取消任务，`F1` 帮助。

常用命令：

| 命令 | 作用 |
|---|---|
| `harnessix exec --trust-workspace --approval-mode auto-edit --prompt "..."` | 无头模式：运行一个任务，自动接受文件修改，结束后输出 JSON 摘要 |
| `harnessix code approvals` | 查看、撤销已保存的“以后不再询问”规则 |
| `harnessix data diagnostics /path/to/repo --output diag.json` | 生成不含代码和对话的诊断文件，用于反馈问题 |
| `uv tool upgrade harnessix` | 升级 |
| `uv tool uninstall harnessix` | 卸载（不会删除 `~/.harnessix` 中的配置和会话） |

完整的安装、配置、功能和排障说明见[用户手册](https://github.com/carrie1988/Harnessix/blob/main/docs/operations/user-manual.md)。

## 安全与隐私

- 沙箱内的命令不能联网，不能写 `.git` 和工作区以外的位置，读不到 `~/.ssh` 等凭据目录和工作区内的 `.env` 文件。
- 沙箱外的命令与用户本人权限相同，因此需要批准。
- API Key 只从环境变量读取，不写入配置文件，并在输出和记录中按机密保护。
- 代码和对话只发送给用户配置的模型服务商。Harnessix 没有遥测，不向项目方发送任何数据。
- 会话数据保存在本机，默认位于 `~/.harnessix`。

**已知限制：** 预览版尚未经过独立的第三方安全审计；沙箱内的命令可以读取拒读清单以外的文件，这些内容可能随工具结果发送给模型服务商。完整的安全边界和限制见[安全说明](https://github.com/carrie1988/Harnessix/blob/main/SECURITY.md)、[隐私说明](https://github.com/carrie1988/Harnessix/blob/main/PRIVACY.md)和[威胁模型](https://github.com/carrie1988/Harnessix/blob/main/docs/threat-model.md)。

## 评测结果

使用同一个本地模型（Qwen3.8-Flash-Next），在 SWE-bench Verified 的 49 题留出集上单次运行，官方判分：

| 系统 | 解决题数 |
|---|---|
| Harnessix | 27 |
| mini-swe-agent | 29（运行时能访问公网，其中至少 4 题直接采用了上游修复，扣除后约 25） |

该结果只能说明两者水平接近：mini-swe-agent 尚需在断网条件下重跑；这 49 题在调优过程中曾用于诊断，结果偏乐观；评测配置使用了 SWE-bench 任务模板；单次运行存在波动。基准成绩不代表在真实项目中的表现。

## 文档

- [用户手册](https://github.com/carrie1988/Harnessix/blob/main/docs/operations/user-manual.md)：安装、配置、使用步骤和全部功能
- [Beta 快速上手](https://github.com/carrie1988/Harnessix/blob/main/docs/operations/beta-quickstart.md)：最简步骤与常见问题
- [配置参考](https://github.com/carrie1988/Harnessix/blob/main/docs/operations/configuration.md)、[诊断](https://github.com/carrie1988/Harnessix/blob/main/docs/operations/diagnostics.md)、[状态备份与恢复](https://github.com/carrie1988/Harnessix/blob/main/docs/operations/recovery.md)
- [架构](https://github.com/carrie1988/Harnessix/blob/main/docs/architecture.md)、[设计文档索引](https://github.com/carrie1988/Harnessix/blob/main/docs/README.md)、[路线图](https://github.com/carrie1988/Harnessix/blob/main/docs/roadmap.md)
- [更新记录](https://github.com/carrie1988/Harnessix/blob/main/CHANGELOG.md)、[项目早期的里程碑记录](https://github.com/carrie1988/Harnessix/blob/main/docs/readme-milestone-history.md)

## 反馈与支持

- 使用问题和建议：在 [GitHub Issues](https://github.com/carrie1988/Harnessix/issues) 提交，可附上诊断文件。
- 安全问题：请勿公开提交 Issue，按[安全说明](https://github.com/carrie1988/Harnessix/blob/main/SECURITY.md)私下报告。
- 反馈中请勿附带密钥、客户数据或未公开的源码。

## 许可

社区版按 [AGPL-3.0-only](https://github.com/carrie1988/Harnessix/blob/main/LICENSE) 发布。闭源使用见[商业许可说明](https://github.com/carrie1988/Harnessix/blob/main/COMMERCIAL_LICENSE.md)，品牌规则见 [TRADEMARKS.md](https://github.com/carrie1988/Harnessix/blob/main/TRADEMARKS.md)，第三方依赖见 [THIRD_PARTY_NOTICES.md](https://github.com/carrie1988/Harnessix/blob/main/THIRD_PARTY_NOTICES.md)。贡献代码前请阅读[贡献指南](https://github.com/carrie1988/Harnessix/blob/main/CONTRIBUTING.md)。
