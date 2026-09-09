# Agent Protocol 与产品运行时源码研究

## 1. 研究范围与基线

本文为 Harnessix Code 0.8.1～0.8.3提供源码事实，关注公共Agent协议、Headless进程、客户端恢复、双向请求和背压。扩展与配置另行研究。研究继续使用[冻结基线](baselines.md)：

| 项目 | 提交 | 本主题证据定位 |
|---|---|---|
| Codex | `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67` | App Server、协议Schema、连接初始化、通知与有界队列 |
| OpenCode | `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5` | HTTP命令、SSE事件、durable游标、OpenAPI与SDK边界 |
| Claude Code逆向仓库 | `2ca5ddabfed5f220812ea11f029eda03b21bc4c1` | NDJSON结构化输入输出、双向控制请求和单写者队列的行为佐证 |

本研究只提取机制、失败语义和取舍，不复制参考实现代码。Claude Code仓库不是官方源码，不作为公共协议或安全决策的唯一依据。

## 2. 标准约束

### 2.1 JSON-RPC 2.0

[JSON-RPC 2.0规范](https://www.jsonrpc.org/specification)明确规定：

- Request必须携带`jsonrpc: "2.0"`、方法名，可选的结构化参数和关联ID；
- Notification没有`id`，服务端不得为其返回Response；
- Response必须且只能包含`result`或`error`之一，并回传原请求ID；
- `-32700`、`-32600`、`-32601`、`-32602`、`-32603`分别用于解析、请求、方法、参数和内部错误；`-32000`～`-32099`保留给服务端错误。

Harnessix保留标准`jsonrpc`字段，不照搬参考实现的线上省略形式。v1不接受Batch数组，避免在同一连接内引入未定义的命令并行和响应排序语义。

### 2.2 公共协议与持久事件

内部`AgentEvent v17`是Session恢复事实，包含Context、模型尝试、私有执行计划等实现信息；公共Agent Protocol必须投影而不是直接序列化该事件。公共游标可以使用内部单调sequence作为不透明位置，但客户端不得假设相邻公开事件的游标差恒为一。

## 3. 参考实现事实

### 3.1 Codex

**事实**

- App Server通过双向JSON-RPC-like协议驱动Thread、Turn、Item；默认传输是stdio JSONL，日志写stderr；
- 每条连接先执行一次`initialize`，初始化前的领域请求和重复初始化都会失败；
- `turn/start`与流式通知分离，Thread/Turn/Item是公共投影，不直接暴露Provider原始事件；
- 传输入站、请求处理和出站写之间使用有界队列；过载返回专用可重试错误；
- stdio连接关闭会结束单客户端App Server进程，但Thread事实由独立存储和Runtime管理，不把EOF解释为任意领域事件；
- 协议类型可以生成TypeScript与JSON Schema，生成物与具体App Server版本绑定。

**推断**

- 初始化握手、Schema生成和连接级能力应进入Harnessix公共合同；Codex不断扩张的方法集合不应成为Harnessix v1兼容承诺；
- 对本地优先产品，stdio能先证明进程隔离、恢复和双向审批，无需提前承担端口认证与Origin攻击面。

### 3.2 OpenCode

**事实**

- Server将Session命令、Permission回复和事件订阅分为HTTP端点；Session列表和历史使用显式游标；
- `/api/event`通过SSE发送Schema约束事件，订阅容量固定为256，并发送心跳；
- durable事件携带aggregate ID、sequence和version，实时连接不是唯一事实来源；
- Server公开OpenAPI，客户端与服务端共享生成协议类型。

**推断**

- 命令响应与事件恢复必须分开设计；“收到成功响应”不代表客户端已经消费对应事件；
- 公开游标需要指向持久聚合位置，而不是只在内存连接中递增。

### 3.3 Claude Code逆向仓库

**事实，仅作行为佐证**

- `StructuredIO`按换行拆分输入，跳过空行，并在输入关闭时拒绝全部待决控制请求；
- 控制请求与流事件进入同一个出站队列，由唯一drain路径写stdout，避免消息互相超越；
- 已处理Tool Use ID使用有界集合去重，迟到或重复控制响应不会再次注入消息。

**推断**

- 双向协议必须对待决请求、重复响应、EOF和唯一写者给出显式语义；仅用多个协程直接写stdout无法保证帧原子性和顺序。

## 4. Harnessix差距

0.7结束时，`AgentRuntime`已经提供Thread创建、恢复、分叉、归档，Turn运行、重试、继续、取消和审批答复；`SQLiteSessionStore`已经提供单调事件与`events(after=...)`。仍缺少：

1. 与内部Event v17解耦的公共Thread/Turn/Item/Event投影；
2. 标准JSON-RPC Envelope、初始化协商、方法注册和错误映射；
3. 跨进程持久请求幂等和已接受命令的重开语义；
4. stdio单读者/单写者、消息上限、有界队列和优雅关闭；
5. 断线后以Snapshot和游标续传的Agent SDK；
6. 不拥有状态机的薄CLI及双向审批、提问、取消和Steering。

## 5. 设计结论

1. Agent Protocol v1使用标准JSON-RPC 2.0和stdio JSONL，保留`jsonrpc`字段；
2. JSON-RPC关联`id`只属于连接，请求幂等`requestId`属于持久命令，两者不能混用；
3. v1公开投影独立版本化，内部Session升级不自动改变公共Schema；
4. 公开事件游标单调但不要求连续，Replay响应返回服务端已经扫描到的权威游标；
5. 客户端提交的Envelope、方法参数和Command严格拒绝未知字段；客户端必须忽略未知通知方法，并允许服务端结果新增可选字段；
6. 关键事件和双向请求不可丢弃；可合并Delta只能作为live-only优化，Snapshot和持久事件始终是恢复事实；
7. v1不开放TCP/WebSocket，不接受JSON-RPC Batch，不允许客户端提交内部Event、Tool Result或Policy结果。

具体决策见[ADR 0070](../adr/0070-agent-protocol-v1-boundaries.md)，实现详设见[0.8设计](../m08-product-runtime-and-extensions.md)。

## 6. 源码索引

- Codex：[App Server README](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/app-server/README.md)、[`rpc.rs`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/app-server-protocol/src/rpc.rs)、[`lib.rs`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/app-server/src/lib.rs)、[`transport.rs`](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/app-server/src/transport.rs)
- OpenCode：[`event.ts`](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/server/src/handlers/event.ts)、[`session.ts`](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/server/src/handlers/session.ts)、[`permission.ts`](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/server/src/handlers/permission.ts)
- Claude Code逆向仓库：[`structuredIO.ts`](https://github.com/carrie1988/claude-code-source-code/blob/2ca5ddabfed5f220812ea11f029eda03b21bc4c1/src/cli/structuredIO.ts)
