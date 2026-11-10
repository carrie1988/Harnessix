---
doc_type: deployment-design
status: current
version: 1
code_revision: 44d044fe12a04cb33482143bd5a03b1183685394
owners:
  - core
modules:
  - mcp
  - product_config
related_adrs:
  - docs/adr/0126-host-stdio-mcp-with-per-server-trust.md
related_tests:
  - tests/product_config/test_mcp_product.py
supersedes: []
---

# 使用 MCP

MCP Server 是给 Agent 提供额外工具的小程序，例如操作 GitHub、查询数据库。Harnessix 在你的电脑上直接启动它，经标准输入输出与它通信。

**先读这一句：** 添加一个 MCP Server，等于以你本人的权限在本机运行一个第三方程序。它能读写你能访问的文件、访问网络。只添加来源可信的 Server。

## 配置

在 `~/.harnessix/mcp.json` 里声明，格式与其他常见工具相同，可以直接复制：

```json
{
  "mcpServers": {
    "my-server": {
      "command": "npx",
      "args": ["-y", "<包名>@<固定版本>"],
      "env": { "API_TOKEN": "${MY_TOKEN}" }
    }
  }
}
```

- `command` 写命令名或绝对路径，不支持相对路径。
- `env` 里的 `${NAME}` 在会话启动时从你的环境变量读取，不会写进任何文件。Key 请这样传入，不要把明文写在配置里。
- `npx`、`uvx` 会在启动时下载包。请像上例一样在参数里固定版本，否则包更新后运行的就是另一份代码。所需的变量名以各 Server 的说明为准。

请先运行一次 `harnessix code configure`，再创建这个文件。

## 信任

配置里的 Server 默认不会启动，需要逐个信任：

```bash
harnessix mcp list               # 查看每个 Server 的状态
harnessix mcp trust my-server    # 显示命令、参数和环境变量名，然后记录信任
harnessix mcp untrust my-server  # 撤销
```

信任绑定当时的定义：命令、参数、环境变量名和值的摘要，以及命令文件本身。其中任何一项变化，`mcp list` 会显示“定义已变化”，需要重新信任。

## 使用

- 已信任的 Server 在每次会话开始时启动，会话结束时退出。某个 Server 启动失败不影响会话，只是没有它的工具。
- 每次调用 MCP 工具都要你批准，Server 自称“只读”也一样。终端界面里选择“本会话允许”后，只有工具和参数都完全相同的调用会自动通过。
- `harnessix exec` 不会自动批准 MCP 调用。
- 调用发出后如果超时或断开，结果按“未知”处理，Harnessix 不会自动重试，请自行检查。

## 不支持

仓库内的 MCP 配置、远端（HTTP）MCP、MCP 的 resources、prompts 和 sampling 暂不支持。

## 常见问题

| 现象 | 处理 |
|---|---|
| `mcp list` 显示“找不到命令” | 确认命令在 `PATH` 里，或改用绝对路径 |
| 显示“环境变量 X 未设置” | 在启动 Harnessix 的终端里设置该变量 |
| 显示“定义已变化，需重新信任” | 核对改动后运行 `harnessix mcp trust <名称>` |
| 会话里没有出现 MCP 工具 | 运行 `harnessix code doctor` 查看配置、命令和信任状态；都正常时，在终端里手动运行该命令，确认 Server 能启动 |
