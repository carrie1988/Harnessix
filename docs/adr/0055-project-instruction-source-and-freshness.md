# ADR 0055：受控项目指令 Source 与持久 freshness

- 状态：Accepted
- 日期：2026-09-07
- 范围：0.6.2a
- 前置：ADR 0023、ADR 0054

## 1. 背景

0.6.1建立了静态Fragment、输入预算和无正文检查记录，但纯`ContextEngine`不能执行文件I/O。把`Path.read_text`直接加入规划器会绕过Workspace路径、链接、竞态、超时和取消边界；只把动态正文拼入模型请求又无法证明每个模型步骤实际观察了哪个版本。

项目指令还具有高于普通Workspace内容的模型影响力。文件正文不能自行选择trust、priority或required，读取失败也不能等价于“仓库没有规则”。

## 2. 决策

### 2.1 端口分层

保留同步`ContextPlanner`及纯`ContextEngine.prepare`。新增：

- `ContextSource.observe(request, cancel)`：异步、可取消的来源端口；
- `ContextSourceObservation`：瞬时`workspace_scope/source_revision/documents`；
- `SourcedContextEngine.prepare(request, cancel)`：按配置顺序刷新来源，把正文转为Fragment，再调用纯规划器；
- `AsyncContextPlanner`：Agent Runtime的显式异步入口，与同步`context`参数互斥。

Source返回值不含kind。kind由宿主注册的Source对象固定，且动态端口只接受Project/Workspace/Git/Environment四类，拒绝Runtime/User提权。

### 2.2 项目指令发现

`ProjectInstructionSource`在构造时绑定规范根和工作目录。一次观测执行：

1. 校验Thread Workspace严格解析后等于绑定根；
2. 构造从根到工作目录的祖先链；
3. 记录每层目录读取前revision；
4. 每层依次尝试`AGENTS.override.md`、`AGENTS.md`，选择首个存在候选；
5. 通过既有`read_file`分页读取完整UTF-8正文，后续页携带首个revision；
6. 检查总正文不超过默认64 KiB；
7. 再次读取目录revision，任一变化则整次观测失败；
8. 计算绑定读取契约、目录和文档revision的`source_revision`。

不递归搜索其他目录，不越过Workspace根，不跟随符号链接，不接受多硬链接文件，不读取deny-path。空文件或空白文件会被观测但不生成Fragment；同目录override存在时，即使空白也不会回退到普通文件。

### 2.3 freshness与持久化

新增`ContextInspection v2`和`ContextSourceSnapshot v1`。每个快照记录：

- 稳定source ID和固定kind；
- `available`或`empty`；
- workspace scope和source revision；
- 每个已观测文档的相对source、文件revision、UTF-8字节数和可选fragment ID。

快照不保存正文。Source引用的fragment ID必须存在于同一Inspection的Fragment决策中，且一个动态Fragment不能属于多个Source。

Agent Event/Thread升级至v11，Session migration 13仅推进最低reader标记，不改表、不改写旧Event或快照。`ContextInspection v1`继续可读；v2只能写入Event v11。

### 2.4 失败语义

| 场景 | code | retryable | Provider请求 |
|---|---|---:|---:|
| Source与Thread Workspace不一致 | `context_source_workspace_mismatch` | 否 | 不发送 |
| 链接、硬链接、错类型、非法UTF-8、二进制或拒绝路径 | `context_source_invalid` | 否 | 不发送 |
| 文件/组合结果超过边界 | `context_source_too_large` | 否 | 不发送 |
| 超时、观测期间变化、分页变化或暂时I/O失败 | `context_source_unavailable` | 是 | 不发送 |
| Source返回错误契约或重复身份/Fragment | `context_source_invalid` | 否 | 不发送 |
| Turn取消 | 既有`cancelled` | 不适用 | 不发送下一请求 |

“未发现候选”是成功的`empty`，不是错误。当前没有持久正文可供安全回退，因此任何unavailable都失败关闭；不得把旧revision当成仍有效正文继续发送。

### 2.5 取消与资源回收

受控文件读取通过`run_read_operation`在线程执行。外层CancelToken取消协程后，读取协程设置`ReadOperation.stopped`，等待线程退出并回收Workspace FD，随后传播`TurnCancelled`。同一辅助函数也替代CodingToolRuntime原有等价读取逻辑，保持工具和Context Source一致。

### 2.6 可观测性

继续使用`harnessix.agent.context` Span。新增`harnessix.agent.context.sources`计数器，仅使用固定`kind/status`标签。source ID、路径、正文、revision、workspace scope、Thread和用户字符串不作为Metric标签；路径与revision仅存在于受控Session诊断结果。

## 3. 取舍

### 3.1 不在ContextEngine中直接读取文件

拒绝。它会让纯规划测试依赖宿主I/O并混淆预算失败与来源失败。

### 3.2 读取失败时当作空配置

拒绝。缺失安全/构建规则可能导致模型在错误约束下执行。

### 3.3 超限时截断项目指令

拒绝。尾部可能包含例外、禁止项或更深层约束，截断后继续无法证明语义完整。

### 3.4 把正文写入ContextPrepared

拒绝。会复制敏感仓库文本并放大Session；当前通过fragment ID、文件revision、source revision和模型指令指纹提供可审计绑定。

### 3.5 首次实现自动查找Git项目根

暂不采用。Thread和工具已由宿主绑定Workspace根，擅自以`.git`改变边界会导致Context与工具能力根不一致。项目根探测应作为独立配置契约设计。

## 4. 兼容与回退

- 静态`context=ContextEngine(...)`入口行为保持不变，但新写Event统一使用v11；
- 动态入口使用`async_context=SourcedContextEngine(...)`，两个入口同时配置时构造失败；
- v1-v10事件和v1-v10投影继续读取，旧Schema文件冻结；
- 一旦新程序向Session追加v11事件，旧wheel不能继续读取；回退必须恢复一致备份，禁止删除migration 13或手工下调投影版本。

## 5. 验收门禁

- 根到工作目录顺序、override优先级、缺失与空白语义；
- 路径逃逸、符号链接、硬链接、超限、Workspace错配和读取竞态失败关闭；
- 取消停止并join读取线程，超时错误脱敏且可重试；
- 两个模型步骤之间文件变化产生不同source/document revision和不同当前指令；
- v2快照无正文、Event v11、migration 13、SQLite重开和Replay一致；
- Source指标只有低基数kind/status且无路径、正文和revision；
- 全仓回归、严格异步/警告、Schema、构建和独立wheel验证通过后，0.6.2a才可关闭。
