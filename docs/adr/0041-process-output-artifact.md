# ADR 0041：Process输出Artifact契约与事务发布

- 日期：2026-09-05
- 基线：`8387741`，CI33972815446四项通过，开工fetch一致
- 状态：已采纳并实现（0.5.4b2c2）

## 1. 问题

0.5.4b2c1只把进程生命周期、退出码和stdout/stderr计数摘要暴露给模型。完整`ProcessResult`保存在Effect Journal，不能直接塞入模型上下文：双流可能是二进制，单流最多1 MiB，重复进入历史会持续消耗上下文；Action Journal又是外部效果事实，不能被当作面向模型的分页存储。

现有Artifact表与Session事件在同一SQLite数据库，但用途约束只接受只读结果和Batch Diff。Process输出必须同时解决：原始字节、截断真实性、调用/Action归属、引用原子性、配额/TTL、分页和损坏检测，同时不能因为归档失败重写或重放已经发生的Action。

## 2. 决策

新增独立`process_output`用途和`SQLiteProcessArtifactPublisher`。宿主必须把发布器绑定到同一个`SQLiteSessionStore`、同一个`ProcessRuntime`以及用于读取的工作区作用域；`AgentRuntime`不自动发现或创建这些能力。

Action Result仍是唯一进程效果事实。Process Artifact只是Action已捕获输出前缀的模型展示副本：

- 只从已经通过调用、计划、批准和Action快照核对的终态`ProcessObservation`生成；
- Artifact正文、manifest、`ToolResult.output.artifact`以及Process终态Session事件在一个`BEGIN IMMEDIATE`事务提交；
- Effect Journal与Session数据库仍是Saga，不宣称跨库原子；
- 配额不足、文档超限或可恢复的发布失败时，提交不带Artifact引用的真实Process终态，绝不改变Action结果或再次执行命令。

## 3. `process-output/v1`正文

正文是规范JSONL，第一条必须为唯一`summary`，后续为有序`chunk`：

```text
summary(stdout元数据, stderr元数据, complete)
stdout chunk(offset=0, Base64原始字节)
stdout chunk(offset=...)
stderr chunk(offset=0, Base64原始字节)
...
```

每个分片最多12 KiB原始字节，保证单条JSONL不超过既有24 KiB分页上限。stdout必须全部位于stderr之前，每条流从offset 0连续排列；Base64必须是规范编码。摘要保存captured/observed字节数、捕获前缀SHA256、观察SHA256、truncated和EOF。

`complete=true`仅表示两条流都自然EOF且没有丢弃已观察字节，不表示命令成功、退出码为0或没有进程副作用。正文完整保存`ProcessResult`中已经捕获的双流前缀；若Base64与元数据无法整体放入单个1 MiB Artifact，则不发布，而不是再次截断后谎称完整。该限制不改变Effect Journal中的原`ProcessResult`。

独立冻结：

- `spec/process-output-record-v1.schema.json`；
- `spec/process-output-document-v1.schema.json`。

Agent Event/Thread继续为v9；Artifact引用复用现有`ArtifactRef`，不新增事件字段。

## 4. 读取与完整性

读取继续使用`read_artifact`及原有Thread、工作区作用域、分页、TTL和活跃Turn保护。Process用途额外核对：

1. 表中manifest与Artifact ID、大小、过期时间一致；
2. 同一Turn存在唯一完成的Process批准请求和同Call完成结果；
3. 结果、批准计划与私有Process效果绑定同一Action ID及指纹；
4. `output.artifact`逐字段等于表中manifest；
5. 正文SHA256、记录数、规范JSONL、分片顺序/偏移/Base64/捕获摘要全部一致；
6. 正文双流公开元数据与Tool Result中的有界摘要一致，`complete`与引用一致。

任何一项失败均返回`artifact_corrupt`，不能把损坏、错绑或已过期正文当作空成功。将Process行改成旧`tool_result`用途同样会被拒绝，不能借用途降级绕过专用核对。

## 5. 提交、失败与恢复

发布器先冻结并用Reducer验证原事件批次，再在同一Session事务内检查配额、插入正文并追加终态事件。提交前异常回滚正文和事件；普通运行时随后以无引用终态降级。提交后确认丢失时，发布器通过原Event ID识别已提交批次并返回当前投影，不重复插入。

真实`os._exit`覆盖正文插入后、Session提交前和提交后三个窗口：

- 提交前退出：数据库保持WAITING_ACTION且无Artifact；重开只读取已成功Action，重新生成一次正文并继续，不运行Worker；
- 提交后退出：正文、引用和终态事实同时存在；重开按既有Kernel规则将尚未完成的模型循环记为interrupted，不观察或重放Action；
- 两种路径均保持一个Action、一次进程执行、至多一个`process_output`行及Session Replay一致。

## 6. Session migration11

`0011_process_output_artifacts.sql`在单个迁移事务中复制Artifact表，唯一变化是把`process_output`加入用途白名单。旧行的purpose、正文、manifest、事件和投影原字节保留。复制、删除旧表、重命名和提交后四个真实退出点只能留下完整migration10或完整migration11；重开后升级幂等。

真实v8升级探针现允许当前wheel连续追加migration10/11。旧v8 reader仍以`schema_too_new`拒绝，不能删除marker伪装降级。

## 7. 不在本片完成的能力

- 不把`host.process`加入默认Agent工具表；
- 不实现WAITING_ACTION取消、跨进程并发决定或完整Session×Action崩溃矩阵；
- 不实现后台进程、PTY、OS Sandbox、网络隔离或宿主死亡后的外部监督；
- 不实现Git、`run_tests`、任意Shell、源目录合入或真实编码Eval；
- 不把SHA256、UUID、SQLite文件权限或工作区scope宣称为同UID恶意进程防护。

上述运行时恢复与双SDK离线HTTP闭环进入b2c3；Git/测试执行进入0.5.4c。
