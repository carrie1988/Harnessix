# 更新记录

## 0.10.0b1（预览版，未发布）

### 新增

- 发布到 PyPI：`uv tool install harnessix` 即可安装，国内可经镜像下载；推送版本标签后自动构建、三平台冒烟，经维护者批准后发布
- 一行安装：终端界面和模型 SDK 成为默认依赖
- 接入局域网自托管模型：`code configure --self-hosted`，已实测 vLLM 和 Ollama
- 终端界面可在本会话自动接受修改，或允许完全相同的操作（`Ctrl+E`）
- 首次打开目录时确认信任；`harnessix exec` 用 `--trust-workspace`
- 显示已核对牌价模型的费用上限，`harnessix exec --max-cost` 按金额限制
- 用户要求时，经批准提交 Git
- 支持 Skills：把任务说明放进 `~/.harnessix/skills` 或仓库的 `.harnessix/skills`，Agent 按需读取；`harnessix skills list` 查看结果
- `harnessix data`：列出、导出、删除本机数据，生成不含代码的诊断文件
- Windows：零配置提供沙箱外的 PowerShell 命令，每条都需批准（仅经 Windows CI 验证，尚未在真机上试用）
- 支持本机 MCP Server：在 `~/.harnessix/mcp.json` 里声明，`harnessix mcp trust` 逐个信任，每次工具调用都需批准
- 自动探测 git，自动填写 DeepSeek、百炼模型的上下文窗口
- 模型请求读取 `HTTPS_PROXY`、`HTTP_PROXY`、`NO_PROXY`；`code configure --proxy-url` 可指定本机代理并优先

### 改变

- `code doctor` 结论为可启动时，不影响启动的问题（如沙箱不可用、改为逐条批准）标为“警告”而不是“失败”
- 交互模式在本地模型或费用可见时不再限制累计 Token，步数和时限仍然生效
- 新配置默认使用紧凑工具结果，重试 4 次
- 确定未计费的失败（HTTP 错误响应、连接未建立）不再结束任务，并遵循 `Retry-After`
- `harnessix exec` 默认开启完成守卫，费用未知时按预留额继续
- 版本号从内部候选 `1.0.0rc1` 改为预览版 `0.10.0b1`，正式发布前不使用 1.0 号；装过 `1.0.0rc1` 的环境须显式指定新 wheel 安装

### 修复

- `doctor --probe-model` 与实际请求的代理设置不一致，导致探测通过、任务却连不上
- 已有配置时重新配置无从下手，现在提示里给出替换所需的摘要
- Linux 上使用默认状态目录时，沙箱内命令全部无法启动（bubblewrap 报只读文件系统）
- Linux 只读工作区位于 `/tmp` 下时，沙箱内命令看到的是空目录
- Task Pack 评分器不认聚合工具 `git_review`
- 小窗口模型的配置无法启动
- 过短的本地占位 Key 导致每次任务失败，现在启动前就会提示
