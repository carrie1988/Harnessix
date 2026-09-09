# ADR 0070：Agent Protocol v1公共投影、游标与兼容边界

- 状态：已接受
- 日期：2026-09-09
- 实施：0.8.1

## 背景

ADR 0009已经选择标准JSON-RPC 2.0与stdio JSONL。0.8开始前，Session内部`AgentEvent v17`、Thread和Item已经包含恢复、Context、模型账本和工具私有事实，不能直接作为长期公共协议。协议还必须区分连接响应关联与跨重启命令幂等，并定义内部事件过滤后游标不连续的合法语义。

## 决策

1. Agent Protocol v1保留标准`jsonrpc: "2.0"`，Request、Notification和Response严格互斥；关联ID只接受有界字符串或安全整数，不接受`null`、浮点和布尔值。
2. v1只接受单个JSON对象，不接受Batch数组；stdio每行一个UTF-8对象，空行、超长帧、无效UTF-8和行内多对象均失败。
3. 每条连接必须按`initialize → notifications/initialized → operation`进入工作态；初始化只协商协议、客户端身份、能力和限制，不创建Thread或读取凭据。
4. 公共`ThreadView`、`TurnView`、`ItemView`和`PublicEvent`独立于内部Session Schema版本化；Provider原始事件、Context正文、Secret、宿主环境和私有执行载荷不得直接透传。
5. 公开事件使用Thread内部sequence作为单调不透明`cursor`。过滤内部事件后游标允许跳跃；客户端以Replay返回的`scannedThrough`和Snapshot的`cursor`判断恢复位置，不得通过`cursor + 1`推断丢失。
6. JSON-RPC `id`只关联一次连接响应；所有持久写Command另带`requestId`，以初始化时固定的`clientInstanceId + requestId`形成幂等域。同方法同规范参数返回原结果，不同方法或参数返回`idempotency_conflict`。
7. 客户端输入Envelope、已知方法参数和Command拒绝未知字段，避免把拼写错误静默降级。客户端遇到未知服务端Notification必须忽略并保留当前游标；服务端结果只允许以新增可选字段向后兼容。
8. JSON-RPC标准错误保留标准码；领域失败使用`-32010`，幂等冲突、未初始化、过载、慢客户端和关闭分别使用稳定服务端错误码，并在`error.data`中只暴露白名单`code/retryable/path/cursor`。
9. 客户端不能提交`AgentEvent`、`ToolResult`、`PolicyDecision`、事件sequence或运行时状态。审批和用户答复必须绑定服务端发出的请求身份、Thread、Turn及指纹。

## 方法基线

v1基线包含：

- 初始化：`initialize`、`notifications/initialized`；
- Thread：`thread/create`、`thread/get`、`thread/list`、`thread/resume`、`thread/fork`、`thread/archive`；
- Turn：`turn/start`、`turn/retry`、`turn/resume`、`turn/cancel`；
- 交互：`approval/respond`、后续0.8.3接入的`question/respond`与`turn/steer`；
- 恢复与Artifact：`events/replay`、`artifact/read`。

未实现的方法仍不得出现在服务端协商能力中。Schema定义方法合同不等于运行时已经开放该能力。

## 失败与恢复

- 解析、Request、方法和参数错误在进入Runtime前返回标准错误；
- 写Command只有在持久幂等记录和领域接受边界完成后才返回成功；
- 响应丢失后，客户端用相同`requestId`重试，不能生成新的领域命令；
- 客户端断线不隐式取消Turn；重连先读取Thread Snapshot，再从Snapshot前已知游标Replay；
- 未知公开事件或结果扩展不改变客户端已经确认的领域状态。

## 取舍

- 不直接复用内部模型，减少Session迁移对客户端的破坏，但需要显式投影和双份Schema测试；
- 不支持Batch，牺牲同连接批量吞吐，换取写命令顺序、背压和幂等的可证明性；
- 游标不连续，避免把内部诊断伪装成公共事件，但SDK必须实现`scannedThrough`语义。

## 验证

- JSON-RPC Envelope、ID、单消息和标准错误Golden测试；
- 初始化状态机、版本/能力协商和未知字段测试；
- 公共投影不包含Provider原文、Secret和私有Context；
- 过滤内部事件后的游标跳跃、Replay与Snapshot一致；
- 同`requestId`相同载荷幂等、不同载荷冲突；
- 旧客户端忽略未知Notification并接受新增可选结果字段。
