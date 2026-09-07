# ADR 0057：Tool Result稳定模型视图与完整Artifact绑定

- 状态：Accepted
- 日期：2026-09-08
- 范围：0.6.2c
- 前置：ADR 0023、ADR 0037、ADR 0041、ADR 0054

## 1. 背景

Session允许单个工具结果达到Turn配置的接收上限，Context Window和Provider请求却需要更小、更稳定的模型视图。直接改写已完成Item会破坏事件事实、恢复和审计；直接截断序列化JSON会产生无效结构或改变字段含义；只保留摘要而没有完整结果，又使模型无法按需取回证据。

已有搜索、Process输出和Batch Diff具备不同用途的Artifact。它们的完整性语义并不相同：搜索`tool_result`正文可以代表完整记录集，Process Artifact只代表已捕获流，Diff Artifact只代表计划或效果差异。0.6.2c必须限制模型输入，同时禁止把局部证据误作整个工具结果的备份。

## 2. 决策

### 2.1 两层历史

持久Session Item是唯一事实历史。`AgentRuntime`在每个`PREPARING_CONTEXT`阶段从已完成Item构造瞬时模型历史；模型视图只替换深拷贝中的Tool Result `output`，不得修改原Item、旧Event或投影中的结果。

Context Engine的`history_documents`和Provider的`ModelRequest.history`必须使用同一份准备后历史，避免预算估算基于原文而Provider收到另一份内容。

### 2.2 单结果预算

`ToolResultViewPolicy v1`默认把每个Provider可见Tool Result限制为64 KiB，允许范围为1 KiB至1 MiB。规模按`outcome/output/error/diff_artifact`的稳定、排序规范JSON UTF-8字节计算，不使用Python字符数或供应商Tokenizer。

未超限结果采用`inline`策略，所有JSON层级保持不变。超限时禁止切割字符串或任意JSON子树；只有满足第2.4节的结果可采用`artifact_reference`，否则在Provider调用前失败。

本切片不设置跨全部历史的Tool Result聚合上限。已经进入模型的结果不能因后续历史增长改变视图；总Context超限由现有Context失败语义暴露，并由0.6.3 Compaction解决。

### 2.3 冻结决定与步骤检查

`ToolResultViewDecision v1`在结果第一次进入模型历史时记录：

- 来源Item ID与Call ID；
- 策略和生效字节上限；
- 来源/模型视图的SHA-256和UTF-8字节数；
- 最多两个Artifact用途绑定；
- `artifact_reference`策略下模型实际使用的完整替换`output`。

`inline`不复制原正文，模型视图直接来自不可变原Item。`artifact_reference`只保存固定省略元数据和Artifact manifest，不保存原preview。后续模型步骤、恢复及未来Fork必须复用既有决定并核对来源摘要；代码升级不能重算为不同内容。若宿主改用更小上限且旧冻结视图无法满足，只能明确失败或先做Compaction，不允许改变已见前缀。

每个模型步骤持久化一份`ModelHistoryInspection v1`，记录策略、Item/结果/策略计数、Artifact绑定计数、变换前后字节数、原历史与模型历史摘要及有序决定集摘要。`ModelHistoryPrepared`事件携带本步骤首次产生的决定；Reducer验证决定来源、唯一性、步骤顺序和全部摘要后才更新投影。

### 2.4 允许替换的Artifact

超限结果只有同时满足下列条件才能替换：

1. `output`严格为`preview`和`artifact`两个字段；
2. `artifact`符合`ArtifactRef v1`；
3. 引用用途为`tool_result`并绑定同一Thread、Call与完成Item；
4. manifest声明`complete=true`；
5. Artifact状态为published且未过期；
6. 正文存在，长度、SHA-256、JSONL记录和manifest一致。

替换后`output.preview`为`harnessix.tool-result-omission/v1`固定结构，包含省略原因、来源字节数和来源摘要；`output.artifact`保持原manifest。模型可通过`read_artifact`分页取回完整记录。替换结构本身也必须在策略上限内。

Artifact发布必须发生在Tool Result首次事务提交时。模型历史准备阶段不补写Artifact，因为此时无法在不改写原Item的前提下建立双向绑定。

### 2.5 独立证据类型

- `process_output`引用在发网前验证，但不用于通用结果替换。它只证明Process Runtime已捕获的stdout/stderr文档；`complete=false`仍可作为有界摘要的诚实引用。
- `batch_effect` Diff引用在发网前验证，但不代表Tool Result的其他`output/error`字段。
- 单文件Patch没有完整结果Artifact，超限时失败。
- 当前JSON Tool Result没有正式媒体块。图片、音频或其他Blob必须由未来带MIME、大小、摘要和专用Artifact的契约接入，通用字符串不做媒体识别或裁剪。

### 2.6 Artifact验证端口

新增只读`ArtifactReferenceVerifier`，输入Thread、Call、ArtifactRef和固定用途。SQLite实现从同一Session事务快照验证：

- 查询归属和用途；
- manifest与调用方引用完全一致；
- Session Item反向关联；
- published状态与TTL；
- 正文类型、长度、摘要、JSONL记录数；
- Process Artifact额外验证双流摘要和完整性。

端口不返回正文。无验证器时，任何带Artifact的历史均以`context_artifact_verifier_required`失败；`artifact_not_found`不区分不存在与跨归属，避免归档枚举。

### 2.7 顺序、取消与故障

每个模型步骤固定顺序为：

1. 进入`PREPARING_CONTEXT`；
2. 纯计算生成或复用模型视图决定；
3. 逐一验证模型可见Artifact，在验证前后检查取消；
4. 原子提交`ModelHistoryPrepared`；
5. 使用同一模型历史规划Context并提交`ContextPrepared`；
6. 进入`CALLING_MODEL`并调用Provider。

决定或Artifact失败、取消、超时、Session提交失败都不会调用Provider。宿主在检查事件提交后、Provider前退出时，现有启动恢复把活跃`PREPARING_CONTEXT` Turn标为`INTERRUPTED`，不会自动重发模型请求；检查记录和决定保留供审计。

### 2.8 版本与可观测性

Agent Event/Thread升级至v13，Session migration 15只推进最低reader标记，不改表、不改写旧Event、Item、Artifact或投影。v1-v12 Schema保持冻结；旧reader必须对migration 15返回`schema_too_new`。

新增模型历史指标只允许固定`strategy`和结果计数，不以Item ID、Call ID、Artifact ID、摘要、路径或正文作为标签。Span可记录Thread、Turn和模型步骤，Artifact验证错误只使用受控错误码与公开消息。

## 3. 失败语义

| 场景 | code | retryable | Provider请求 |
|---|---|---:|---:|
| 超限且没有完整结果Artifact | `context_tool_result_artifact_required` | 否 | 不发送 |
| Artifact声明不完整却用于替换 | `context_tool_result_artifact_incomplete` | 否 | 不发送 |
| Patch/Process/非标准结构超限 | `context_tool_result_unsupported` | 否 | 不发送 |
| 冻结决定与来源或当前更小预算冲突 | `context_tool_result_decision_mismatch` | 否 | 不发送 |
| 历史带引用但未配置验证器 | `context_artifact_verifier_required` | 否 | 不发送 |
| Artifact不存在或跨归属 | `artifact_not_found` | 否 | 不发送 |
| Artifact过期 | `artifact_expired` | 否 | 不发送 |
| manifest、关联或正文损坏 | `artifact_corrupt` | 否 | 不发送 |
| Turn取消/超时 | 既有`cancelled`/`time_budget_exceeded` | 不适用 | 不发送下一请求 |

## 4. 未采用方案

### 4.1 直接截断JSON字符串

拒绝。字符边界不等于UTF-8字节或语义边界，可能生成无效JSON、半条Diff和误导性状态。

### 4.2 每次按当前预算重新选择大结果

拒绝。旧结果可能先以内联形式进入Prompt Cache，后续再变成引用会破坏前缀稳定性，并使Resume行为依赖当前代码和配置。

### 4.3 模型调用前自动补写Artifact

拒绝。结果Item已经完成，补写引用需要改写事实或创建无法被原结果反向证明的旁路manifest。

### 4.4 用Process或Diff Artifact替代整个结果

拒绝。局部证据没有覆盖任意公开字段，删除无关内容会造成不可证明的信息丢失。

### 4.5 Artifact失效时继续发送引用

拒绝。模型会得到无法取回的能力承诺；过期和损坏必须在发网前显式暴露。

## 5. 验收门禁

- JSON、Unicode、多字节边界、空值、失败结果和确定性规范化；
- inline与artifact_reference首次决定、跨步骤复用、来源篡改、策略缩小和替换字节一致；
- 原Item/Event字节不变，Context与Provider使用同一准备后历史；
- Artifact Thread/Call/用途/manifest/状态/TTL/正文摘要/记录数及Process摘要校验；
- 缺验证器、不完整、过期、损坏、错归属、超限Patch/Process全部发网前失败；
- 检查事件顺序、重复、缺失决定、Replay、SQLite重开和提交后故障恢复；
- Event/Thread v13、migration 15、真实v12 wheel升级和旧reader拒绝；
- 取消、超时、Telemetry脱敏、双Provider映射及全仓回归；
- Schema连续生成、构建、仓库外独立wheel和远端CI矩阵通过。
