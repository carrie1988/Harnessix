# Harnessix Code 0.8 产品运行时与扩展详细设计

## 1. 文档状态

- 适用版本：0.8；
- 当前状态：0.8.1已完成本地验收，0.8.2实施中，0.8.3～0.8.6待前序切片关闭后实施；
- 总体目标：把0.7已经完成的可信执行与工程交付能力开放为可恢复的本地产品服务，并在相同Runtime、Permission和Sandbox边界内接入CLI、SDK、MCP、Skills、Hooks与Provider配置。

本文只把已经实现并验证的切片标记为完成。每个切片必须依次完成源码研究、架构决策、领域契约、最小正式实现、失败恢复测试、真实场景验证和文档同步。

## 2. 范围与非目标

### 2.1 0.8范围

1. Agent Protocol v1公共合同、兼容与事件恢复；
2. stdio Headless App Server和Python Agent SDK；
3. 不复制Runtime状态机的薄CLI与双向交互；
4. MCP Client和可选MCP Server；
5. Skills、Hooks及供应链边界；
6. Provider、Profile、Secret引用和配置迁移。

### 2.2 非目标

- 完整TUI视觉、安装器、签名发行物和自动更新属于0.9；
- WebSocket、HTTP Agent Server、IDE、Web和集中式多租户控制面不属于0.8；
- MCP、Skill或Hook不得获得0.7可信Action入口之外的文件、进程、网络或Secret权限；
- 0.8不以增加模型API调用次数替代确定性协议和恢复测试。

## 3. 总体结构

```text
Thin CLI / Python Agent SDK / Automation
                  │
      Agent Protocol v1 (JSON-RPC 2.0)
                  │
       stdio Headless App Server
                  │
   Agent Application Service / AgentRuntime
        │                 │
 Session Store       Extension Runtime
                          │
          MCP / Skills / Hooks / Provider Config
                          │
                 TrustedActionRouter
                          │
      Workspace / Process / Sandbox / Delivery
```

App Server只做协议验证、身份绑定、幂等、投影、订阅和Runtime编排，不重新实现Agent Loop。CLI和SDK只消费公共协议，不直接打开Session SQLite、Effect Journal或Secret Store。

## 4. 0.8.1 Agent Protocol v1详细设计

### 4.1 生命周期

连接状态固定为`new → initialized_pending_ack → ready → closing → closed`。只有`initialize`可从`new`进入协商态；只有`notifications/initialized`可进入`ready`。EOF、输出失败或显式关闭进入`closing`，拒绝新请求，等待已接受命令完成到持久边界后关闭。

### 4.2 Envelope与限制

- 标准JSON-RPC 2.0；不接受Batch；
- Request ID为1～128字符字符串或绝对值不超过`2^53-1`的整数；
- 方法名1～128字符，参数只接受对象；
- 默认单帧1 MiB、字符串1 MiB、数组8192项、对象递归64层；
- stdout仅承载JSONL，stderr仅承载脱敏诊断；
- 解析错误`id=null`，已识别Request的错误回传原ID，Notification不返回Response。

### 4.3 初始化协商

`initialize`输入包含精确协议版本、客户端名称/版本、稳定`clientInstanceId`和能力。服务端返回自身信息、已实现方法、通知、双向请求、Artifact、Replay和双方有效限制。v1只接受精确`1.0`；未来次版本通过显式兼容集合协商，不用字符串大小比较猜测兼容。

### 4.4 公共投影

- `ThreadView`：Thread身份、Workspace、当前游标、活动Turn、归档、时间和Turn投影；
- `TurnView`：请求身份、状态、预算、用量、Item、错误和时间；
- `ItemView`：公共内容联合、状态和白名单错误；
- `PublicEvent`：Thread/Turn/Item/Usage公开变化，绑定event ID、Thread、可选Turn和单调cursor。

内部模型尝试、Context检查、Compaction账本等事件不逐条公开。Replay仍扫描全部内部事件并返回`scannedThrough`，因此公开事件游标允许跳跃且不会导致重复扫描。

### 4.5 Command与Query

Command必须携带`requestId`，包括Thread创建/分叉/归档、Turn开始/重试/取消、审批答复以及后续Steering和提问答复。Query不创建领域事实，包括Thread读取/列表、事件Replay和Artifact分页。`thread/resume`只重新附着并触发已定义恢复，不创建新Turn。

JSON-RPC ID与`requestId`独立。SDK在响应超时后重用原`requestId`并分配新的JSON-RPC ID；服务端返回原领域结果或冲突，不重复副作用。

协议命令账本以`clientInstanceId + requestId`为主键，只保存方法名、规范参数SHA-256和有界公开终态，不保存Prompt或原始参数。Session migration20追加`protocol_requests`表；`accepted`记录在调用Runtime之前提交，`completed/failed`终态具备结果摘要校验。相同键和相同参数返回既有记录，相同键绑定不同方法或参数以`idempotency_conflict`失败。`accepted`状态不等于副作用未发生，后续App Server必须使用领域确定性身份或查询恢复，禁止直接重放。

### 4.6 错误

| JSON-RPC码 | 稳定data.code | 含义 |
|---|---|---|
| `-32700` | `parse_error` | JSON或UTF-8不可解析 |
| `-32600` | `invalid_request` | Envelope或消息形态非法 |
| `-32601` | `method_not_found` | 方法未实现或未协商 |
| `-32602` | `invalid_params` | 参数Schema失败 |
| `-32603` | `internal_error` | 未分类服务端错误，正文脱敏 |
| `-32010` | 领域错误码 | Runtime确定性拒绝或失败 |
| `-32011` | `idempotency_conflict` | 幂等键已绑定其他命令 |
| `-32012` | `not_initialized` | 未完成握手 |
| `-32013` | `server_overloaded` | 入站或执行容量已满，可重试 |
| `-32014` | `client_too_slow` | 关键出站消息无法在界限内排队 |
| `-32015` | `server_closing` | 已开始关闭，不再接受命令 |

错误data不保存Prompt、参数正文、Provider响应、路径正文或Secret；字段路径只使用Schema字段名和数组下标。

### 4.7 0.8.1验收

- 公共Pydantic合同和提交仓库的JSON Schema逐项相等；
- Envelope、ID、方法、参数、未知字段、版本和错误Golden测试；
- Thread/Turn/Item/Event投影与内部Event v17解耦；
- 隐藏事件导致游标跳跃时，Replay返回正确`scannedThrough`；
- 旧客户端解析未知通知与新增可选结果字段的兼容测试；
- 默认测试不启动模型、不访问网络、不读取用户凭据。

0.8.1实现位于`harnessix.protocol`，包括严格公共合同、JSON帧Codec、兼容解码、内部事件白名单投影、Replay投影和SQLite协议请求账本。提交仓库的13份Schema由同一运行时模型生成。确定性测试覆盖非法UTF-8/JSON、Batch、重复字段、深度和尺寸、ID边界、未知输入字段、旧客户端新增输出字段、未知通知、隐藏内部事件造成的游标跳跃、重复Command、持久重开、终态冲突和账本篡改；不连接模型或网络。

## 5. 后续切片冻结入口

0.8.2只能依赖本章公共合同和既有`AgentRuntime`/`SessionStore`端口；0.8.3只能依赖Agent SDK；0.8.4～0.8.5只能调用`ExtensionActionPort`；0.8.6的配置只能保存Secret引用，不能把凭据值写入协议、Session或配置文件。具体设计在对应源码研究和ADR完成后追加。

## 6. 参考资料

- [Agent Protocol与产品运行时源码研究](research/agent-protocol-product-runtime.md)
- [ADR 0009：JSON-RPC 2.0与stdio JSONL](adr/0009-app-server-protocol.md)
- [ADR 0070：Agent Protocol v1边界](adr/0070-agent-protocol-v1-boundaries.md)
- [JSON-RPC 2.0规范](https://www.jsonrpc.org/specification)
