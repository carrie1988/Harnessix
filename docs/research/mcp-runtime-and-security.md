# MCP运行时与安全边界源码研究

## 1. 研究范围与冻结基线

本文为Harnessix Code 0.8.4提供协议与实现依据，研究范围包括协议代际、Client/Server生命周期、Tool目录、Schema漂移、进程清理、超时、取消以及Tool执行安全边界。研究基线如下：

| 来源 | 提交/版本 | 证据范围 |
|---|---|---|
| MCP规范 | `aa8ce049f089f92618340190d4ece141f663310d`，规范`2026-07-28` | `server/discover`、Tool、订阅、缓存、分页、MRTR、stdio与Streamable HTTP |
| MCP Python SDK | `9972c21aa42054fb1450c5fc614761ed11847ec6`，`v2.2.0` | 双时代Client、stdio进程树清理、低层Server、结果Schema校验 |
| Codex | `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67` | MCP目录绑定、模型可见名称、连接管理、调用前版本复核 |
| OpenCode | `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5` | 本地/远端连接、分页、超时、list-changed刷新与进程清理 |
| Claude Code逆向仓库 | `2ca5ddabfed5f220812ea11f029eda03b21bc4c1` | 连接状态、名称规范化、超时、重连与Tool结果处理的行为佐证 |

Claude Code仓库不是官方源码，不作为协议或安全结论的唯一依据。参考实现只用于提取机制和失败语义，不复制其实现代码。

## 2. MCP `2026-07-28`协议事实

### 2.1 无会话核心与双时代兼容

`2026-07-28`删除了`initialize/notifications/initialized`握手和协议级Session。每个请求在`_meta`中携带协议版本与客户端能力；服务端必须实现`server/discover`，客户端可以先发现，也可以直接调用并处理版本错误。跨请求状态必须由服务端签发并通过普通参数显式传递，不能依赖连接身份。

旧服务端仍使用握手时代协议。官方Python SDK的`Client(mode="auto")`先探测`server/discover`，不支持时退回旧握手，因此Harnessix不自行实现协议协商或复制两套消息类型。

### 2.2 Tool目录不是授权事实

服务端通过`tools`能力声明Tool支持，通过可分页的`tools/list`返回目录。目录应稳定排序，可以携带缓存TTL和作用域，并可能发生变化。Tool名称只在单个服务端内唯一；服务端报告的名称也不保证跨实例唯一。聚合器必须保留原始服务端和Tool身份，并生成独立、确定性的模型可见名称。

Tool的`annotations`是未受信提示。读写性、破坏性、幂等性、审批、资源和恢复策略不能从描述或Annotation推导，只能来自Harnessix宿主维护的可信绑定。

### 2.3 Schema与结果

`inputSchema`必须是JSON Schema对象，未声明方言时按2020-12解释；`outputSchema`可选。实现不得自动解引用外部`$ref`，并应限制Schema深度和验证耗时。`tools/list`可分页，重复游标和无限分页必须失败。

Tool执行错误以成功JSON-RPC响应中的`isError=true`表达；协议错误使用JSON-RPC Error。客户端必须区分二者，并在信任`structuredContent`前检查`isError`及`outputSchema`。写入Tool在请求发送后发生超时、断链或不确定错误时，不能因为收到`isError`或异常就假定没有副作用。

### 2.4 目录变化与MRTR

现代协议的`notifications/tools/list_changed`只会进入客户端主动打开的`subscriptions/listen`流；旧协议使用连接内通知。通知是缓存失效提示，不是安全证明。调用前重新读取完整目录并核对摘要，才能把模型所见Schema和将要调用的服务端状态绑定。

`tools/call`可以返回`input_required`，客户端携带`inputResponses`和不透明`requestState`，使用新的JSON-RPC ID重试。0.8.4不把远端服务端请求的交互直接映射为Harnessix审批；批准权仍由本地Agent Runtime持有。需要持久MRTR时必须新增独立恢复合同，不能把SDK内存回调当作持久事实。

## 3. 官方Python SDK事实

1. `Client`统一支持URL、`StdioServerParameters`、自定义Transport和进程内Server，并以异步上下文管理生命周期；同一Client退出后不能复用。
2. `Client.list_tools`返回强类型、可分页结果；`Client.call_tool`处理协议结果、Tool错误和输出Schema验证。
3. stdio Transport按换行JSON-RPC读写，默认只继承有限环境变量；关闭顺序为关闭stdin、等待、终止进程树、强制清理，且各阶段有界。POSIX和Windows使用不同的进程树实现。
4. 高层Client会自动驱动MRTR；需要跨进程保存`requestState`时必须使用低层Session显式处理。
5. 低层`Server`允许应用提供原始Tool Schema和Result，适合Harnessix导出已经冻结的Tool合同；低层Server不会替应用验证输入参数。
6. SDK会校验成功结果的`structuredContent`是否满足Tool的`outputSchema`，但Harnessix仍需限制总字节数、内容块数和进入Agent上下文的字段。

## 4. 参考实现事实

### 4.1 Codex

- Tool目录同时保留原始MCP身份和模型可见身份；名称规范化冲突使用确定性Hash消歧。
- 模型请求捕获不可变目录Revision和精确Client绑定；实际调用前再次比较当前Revision，变化时在不可逆调用前拒绝。
- 必需与可选服务端采用不同启动失败语义；目录可缓存，连接关闭和取消会结算待决调用。
- stdio帧有字节上限，stdin由单一写锁串行；关闭先等待，再终止子进程。

**结论**：Harnessix必须冻结“服务端配置身份 + 原始Tool身份 + 完整Schema + 目录摘要”，不能只保存规范化名称，也不能在调用时按名称重新选择任意Client。

### 4.2 OpenCode

- MCP目录最多读取1000页，重复Cursor立即失败；模型可见名称以服务端前缀隔离。
- 连接、目录读取和Tool调用均有超时；连接关闭会删除Client、目录和Instructions，并发布目录变化。
- `tools/list_changed`触发重新读取；进程关闭时显式处理stdio后代并关闭Client。
- Tool错误转换为模型可见错误；有结构化输出但无文本内容时生成文本表示。

**结论**：分页上限、停滞Cursor、连接状态和清理必须属于正式合同；刷新失败时不能继续使用未知新旧状态混合的目录。

### 4.3 Claude Code逆向仓库

- 连接状态区分connected、failed、needs-auth、pending和disabled；Tool模型名称保留原始名称以便反向映射。
- Tool调用同时使用SDK超时和外层超时兜底；连接错误必须触发Transport关闭，才能结算SDK内部待决调用。
- stdio清理额外跟踪子进程，Tool、Prompt和Resource变化分别刷新。

**结论**：连接关闭不能只修改UI状态；必须先使传输层结算所有请求，再持久记录失败。逆向代码中的自动重连和超长Tool默认超时不作为Harnessix安全默认值。

## 5. Harnessix现有能力与差距

0.7.5已经提供`TrustedActionRouter`和`ExtensionActionPort`，能够冻结Tool版本、输入Schema摘要、资源、Policy、Sandbox、Secret版本、Workspace Snapshot、审批和UNKNOWN恢复。0.8.3提供持久审批和用户输入交互。仍缺少：

1. MCP服务端身份、能力与Tool目录的不可变公共合同；
2. 跨重启目录和连接状态存储；
3. 调用前强制刷新与Schema漂移拒绝；
4. 受约束的MCP进程生命周期；
5. 将动态JSON Schema接入可信Action参数验证的通用边界；
6. 只导出明确允许的Harnessix只读Action的可选MCP Server。

## 6. 设计结论

1. 使用官方`mcp>=2.2,<3` SDK承担Wire Protocol、版本协商和跨平台stdio关闭；Harnessix不自研JSON-RPC MCP协议栈。
2. 0.8.4生产本地Client只接受由0.7 `ContainerCommandBuilder`产生、携带执行身份且可核对残留的强隔离启动对象。直接宿主stdio只用于受控测试，不作为生产配置。
3. 远端Streamable HTTP需要0.8.6的Provider/Profile/Secret引用和受管出口配置；在该边界完成前不开放任意URL、Header或OAuth值。
4. 每次调用在持有单服务端调用锁时强制重新读取完整目录；当前目录摘要、原始Tool摘要或Schema摘要与捕获快照任一不符，均在`tools/call`前失败关闭。
5. Tool描述、Title、Annotation和服务端Instructions全部视为未受信模型内容，只能影响展示，不能赋予Permission、Sandbox、Secret、风险或恢复能力。
6. MCP Tool仅通过`ExtensionActionPort`计划与执行。宿主显式提供资源Resolver和可信效果合同；未绑定Tool不向模型暴露，也不可调用。
7. 只读调用在超时或断链后进入`failed`；写调用一旦进入`tools/call`，超时、取消、断链、过大结果或`isError`均进入`unknown`，只有显式外部Reconcile合同才能继续。
8. 目录快照、状态和事件进入独立SQLite存储；运行中进程中断后状态恢复为`failed`，历史快照只供审计，不能恢复为可执行Client。
9. 可选MCP Server只导出宿主明确选择的低风险只读Action；Policy不是`ready`时返回Tool错误，不允许MCP调用方替本地用户审批。

## 7. 源码索引

- MCP规范：[`tools.mdx`](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/aa8ce049f089f92618340190d4ece141f663310d/docs/specification/2026-07-28/server/tools.mdx)、[`discover.mdx`](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/aa8ce049f089f92618340190d4ece141f663310d/docs/specification/2026-07-28/server/discover.mdx)、[`stdio.mdx`](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/aa8ce049f089f92618340190d4ece141f663310d/docs/specification/2026-07-28/basic/transports/stdio.mdx)、[`schema.ts`](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/aa8ce049f089f92618340190d4ece141f663310d/schema/2026-07-28/schema.ts)
- MCP Python SDK：[`client.py`](https://github.com/modelcontextprotocol/python-sdk/blob/9972c21aa42054fb1450c5fc614761ed11847ec6/src/mcp/client/client.py)、[`stdio.py`](https://github.com/modelcontextprotocol/python-sdk/blob/9972c21aa42054fb1450c5fc614761ed11847ec6/src/mcp/client/stdio.py)、[`low-level-server.md`](https://github.com/modelcontextprotocol/python-sdk/blob/9972c21aa42054fb1450c5fc614761ed11847ec6/docs/advanced/low-level-server.md)
- Codex：[`binding.rs`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/codex-mcp/src/binding.rs)、[`tools.rs`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/codex-mcp/src/tools.rs)、[`bounded_stdio_transport.rs`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/rmcp-client/src/bounded_stdio_transport.rs)
- OpenCode：[`mcp/index.ts`](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/opencode/src/mcp/index.ts)、[`mcp/catalog.ts`](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/opencode/src/mcp/catalog.ts)
- Claude Code逆向仓库：[`client.ts`](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/services/mcp/client.ts)、[`types.ts`](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/services/mcp/types.ts)
