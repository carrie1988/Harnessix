# ADR 0073：MCP目录绑定与Sandbox执行边界

- 状态：已接受
- 日期：2026-09-09
- 实施：0.8.4

## 背景

MCP把外部Tool目录和调用协议标准化，但不提供Harnessix所需的授权、效果分类、审批、Workspace快照、Secret最小注入或UNKNOWN恢复。当前稳定规范`2026-07-28`又已从会话握手迁移到无会话请求，直接实现旧版Client会制造短期即失效的协议分叉。另一个风险是：stdio MCP Server本身就是长期运行的不可信进程，即使每个Tool调用都经过Policy，服务端仍可能在启动时读取文件或建立网络连接。

## 决策

1. MCP Wire Protocol由官方Python SDK `mcp>=2.2,<3`实现，默认`mode="auto"`兼容现代`server/discover`和旧握手时代。Harnessix只持有SDK适配接口，不复制协议消息类型。
2. 生产本地MCP Server只能通过0.7强Container Sandbox的已准备启动对象运行；对象必须绑定不可变镜像、Workspace模式、网络策略、资源限制、执行身份和残留清理。任意宿主命令不进入生产公共构造器。
3. 每个服务端使用宿主配置的稳定`server_id`作为授权身份；远端报告的`serverInfo.name`只用于显示。原始Tool名称单独保存，模型可见名称采用服务端前缀和确定性Hash消歧。
4. 首次连接完整读取`tools/list`全部分页，限制页数、Tool数量、Schema大小、递归深度和描述长度，拒绝重复Cursor、重复原始名称、外部`$ref`及不支持的Schema。
5. 能力快照按规范排序并计算摘要，进入独立SQLite快照和事件存储。数据库重开把运行中状态恢复为失败；历史快照不能代表活Client。
6. 每个模型可见Tool必须由宿主提供`McpTrustedToolPolicy`，其中冻结效果类别、风险、恢复方式和资源Resolver。MCP Annotation与描述不参与此绑定。未绑定Tool不注册到`TrustedActionRouter`。
7. MCP动态参数使用快照中的原始JSON Schema验证；`TrustedActionDefinition`增加可选的显式Schema和参数Decoder，但既有Pydantic Tool继续使用原路径。Binding摘要绑定显式Schema，执行时对持久参数再次解码。
8. MCP调用通过`ExtensionActionPort`计划、审批、执行和Reconcile。Executor在发送调用前持锁强制刷新完整目录，并比较目录、Tool和Schema摘要；漂移时保存新快照并以`mcp_tool_schema_changed`拒绝，禁止按新目录静默执行旧计划。
9. 进入`tools/call`前的目录失败是确定性失败；进入调用后的断链、超时、取消或结果边界失败，对只读Tool为`failed`，对写Tool为`unknown`。写Tool注册必须提供外部Reconcile实现，禁止自动重放。
10. Tool Result只保留有界JSON表示。`isError=true`对只读调用是普通失败；对写调用按可能已有部分副作用进入`unknown`。任何结果都不能改变已冻结的Permission或Sandbox。
11. 可选MCP Server使用官方低层Server导出显式白名单，只允许低风险只读Binding。输入按公开Schema验证，调用仍走`ExtensionActionPort`；若计划需要审批、被拒绝或状态不一致，返回`isError=true`，不允许远端调用方作审批主体。
12. 0.8.4不开放任意远端URL、Header或OAuth明文配置。Streamable HTTP、Secret引用和受管出口随0.8.6配置纵向切片一起交付。

## 失败语义

| 场景 | 结果 |
|---|---|
| 连接或首次目录读取失败 | 持久状态`failed`，无Tool注册 |
| 分页Cursor重复、页数或Tool数超限 | `mcp_catalog_invalid`，失败关闭 |
| Tool名称重复、Schema过大/过深或外部`$ref` | `mcp_tool_schema_invalid`，整个目录不发布 |
| 调用前目录与计划快照不同 | 保存新快照，旧计划`failed`且未发送Tool调用 |
| 调用前服务端崩溃或目录超时 | `failed`且未发送Tool调用 |
| 只读Tool调用超时、取消或断链 | `failed`，允许上层显式重新计划 |
| 写Tool调用超时、取消、断链或`isError` | `unknown`，只允许显式Reconcile |
| MCP进程退出或宿主关闭 | SDK有界关闭，随后按Container执行身份核对并清理残留 |
| 重启后读取到旧`connected`状态 | 原子恢复为`failed`，必须重新连接和捕获目录 |
| MCP Server收到未导出或非只读Tool | `isError=true`，不创建Action计划 |
| MCP Server规划得到审批/拒绝 | `isError=true`，不代替用户批准，不执行 |

## 取舍

- 调用前完整刷新比仅依赖`list_changed`增加一次目录往返，但提供现代、旧版和通知丢失场景一致的安全判断。
- 单服务端调用锁降低并行度，但能把“刷新目录—检查摘要—发送调用”压缩为明确临界区；后续只有在协议提供可证明Revision前才放宽。
- 强Container要求比Codex、OpenCode当前常见的直接stdio配置更严格，但这是阻止恶意服务端在Tool调用之外越权的必要条件。没有强后端时明确不可用，不降级为宿主用户权限。
- 可选MCP Server暂不导出写Tool，避免在缺少远端主体认证和持久审批回路时制造审批绕过。写Tool仍可作为Harnessix MCP Client消费端的受控能力。

## 验证

- 现代协议发现与旧协议协商由官方SDK集成测试覆盖；
- 多页目录、重复Cursor、重复名称、Schema边界和名称冲突；
- 调用前Schema漂移、服务端崩溃、目录超时和Tool超时；
- 写调用结果丢失进入UNKNOWN、外部Reconcile且不自动重放；
- 恶意Description/Annotation不能降低风险、跳过审批或扩大资源；
- 进程关闭、取消和Container残留清理；
- SQLite重开恢复、事件链和快照损坏拒绝；
- 可选MCP Server白名单、参数拒绝、Policy非ready和成功只读调用。
