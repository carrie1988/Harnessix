---
doc_type: adr
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [mcp, trusted_actions, product_config, product_ui]
related_adrs:
  - docs/adr/0073-mcp-catalog-binding-and-sandbox.md
  - docs/adr/0113-sandboxed-command-approval-and-host-escalation.md
  - docs/adr/0125-skill-product-wiring.md
related_tests:
  - tests/product_config/test_mcp_product.py
supersedes: []
---

# ADR 0127 本机stdio MCP与逐个信任

- 状态：评审中
- 日期：2026-11-10
- 修订：[ADR 0073](0073-mcp-catalog-binding-and-sandbox.md)第2条（生产MCP只能在容器内运行）

## 背景

ADR 0073规定生产MCP Server只能跑在强Container Sandbox里。社区MCP几乎都以`npx`、`uvx`等本机命令发布，
要求用户先准备不可变镜像，等于1.0没有可用的MCP。2026-11-10用户决定（A1=C）：MCP Server在本机以stdio
方式启动，用户逐个信任，模型的每次工具调用走现有审批。Claude Code、Codex、Cursor采用同一模式。

这样做的代价是Server进程拥有用户本人的权限。审批管得住“模型能不能调用某个工具”，管不住Server程序自己
做什么；一个被投毒的npm包在启动时就能读文件、联网。所以信任必须是显式的、绑定到具体定义的，并在
SECURITY.md中写明。

## 决策

1. **配置位置**：只读用户级`~/.harnessix/mcp.json`，格式沿用通行的
   `{"mcpServers": {"<id>": {"command": "...", "args": [...], "env": {...}}}}`，用户可从其他工具直接复制。
   1.0不读仓库内的MCP配置，避免克隆一个仓库就能让本机启动程序。
2. **信任**：配置里的Server默认不启动。`harnessix mcp trust <id>`把定义的指纹写入信任记录；指纹覆盖
   解析后的可执行文件路径、参数、环境变量名和字面值的摘要。定义任何一项变化即视为未信任。
   `npx`、`uvx`这类启动器下载的包版本不在指纹内，文档提示用户在参数里固定版本。
3. **启动**：经官方SDK的stdio客户端在本机启动，工作目录为工作区根目录；环境只继承PATH、HOME、语言和
   临时目录等少数变量，加上配置中声明的变量。`${NAME}`形式的值启动时从用户环境读取，不写入任何持久文件。
4. **工具与审批**：Server自报的只读标注不可信，所有MCP工具都按“可能有外部副作用”处理：每次调用都要审批，
   终端界面可选“本会话允许”，只放行工具和参数完全相同的调用。`harnessix exec`不自动批准MCP调用。调用发出后的超时、断链或
   `isError`对模型报告为结果未知，不自动重放。模型可见名称带Server前缀，沿用ADR 0073的消歧规则。
5. **生命周期**：会话启动时依次连接已信任的Server（SDK的stdio上下文必须在同一任务里进入和退出），单个30秒期限；失败不阻塞会话。`harnessix code doctor`
   报告配置、命令和信任方面的原因；运行时启动失败目前只写入后台日志，终端界面提示留待后续。调用前刷新目录并比较摘要的规则（ADR 0073第8条）保留。
6. **不在1.0范围**：仓库级配置、远端HTTP MCP与OAuth、resources/prompts/sampling、Server向客户端发起的请求。
7. **容器路径保留**：ADR 0073的容器目标继续可用，供需要隔离的部署使用；产品默认不要求容器。

## 被拒绝方案

- **维持容器**（A1-A）：最安全，但用户基本无法使用。
- **本机沙箱运行**（A1-B）：多数MCP要联网，须逐个配置网络与文件策略，工作量约两周；留待企业需求。
- **读取仓库内配置并在首次打开时询问**：交互成本低，但把“打开仓库”变成“可能启动程序”的入口，1.0不做。

## 结果

- 用户可以使用社区MCP，风险由显式信任承担，并在SECURITY.md的信任边界中列出。
- 未配置`mcp.json`时产品行为不变。
- Windows上SDK经pywin32管理子进程，许可已按精确Archive登记（m09-4b第7.1a节）。

## 验证要求

- 定义变化、可执行文件变化后信任失效；未信任Server不启动。
- 真实stdio Server夹具：目录读取、调用审批、拒绝、超时与断链、会话结束时进程回收。
- 环境变量最小化，`${NAME}`值不进入持久数据、审计或事件。
- `doctor`报告未信任、定义变化、命令或环境变量缺失、配置错误等原因。
