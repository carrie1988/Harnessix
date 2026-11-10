# 更新记录

## 0.10.0b1（预览版，未发布）

### 新增

- 一行安装：终端界面和模型 SDK 成为默认依赖
- 接入局域网自托管模型：`code configure --self-hosted`，已实测 vLLM 和 Ollama
- 终端界面可在本会话自动接受修改，或允许完全相同的操作（`Ctrl+E`）
- 首次打开目录时确认信任；`harnessix exec` 用 `--trust-workspace`
- 显示已核对牌价模型的费用上限，`harnessix exec --max-cost` 按金额限制
- 用户要求时，经批准提交 Git
- 支持 Skills：把任务说明放进 `~/.harnessix/skills` 或仓库的 `.harnessix/skills`，Agent 按需读取；`harnessix skills list` 查看结果
- `harnessix data`：列出、导出、删除本机数据，生成不含代码的诊断文件
- 自动探测 git，自动填写 DeepSeek、百炼模型的上下文窗口

### 改变

- 交互模式在本地模型或费用可见时不再限制累计 Token，步数和时限仍然生效
- 新配置默认使用紧凑工具结果，重试 4 次
- 确定未计费的失败（HTTP 错误响应、连接未建立）不再结束任务，并遵循 `Retry-After`
- `harnessix exec` 默认开启完成守卫，费用未知时按预留额继续
- 版本号从内部候选 `1.0.0rc1` 改为预览版 `0.10.0b1`，正式发布前不使用 1.0 号；装过 `1.0.0rc1` 的环境须显式指定新 wheel 安装

### 修复

- Task Pack 评分器不认聚合工具 `git_review`
- 小窗口模型的配置无法启动
- 过短的本地占位 Key 导致每次任务失败，现在启动前就会提示
