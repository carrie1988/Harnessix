---
doc_type: change-design
status: current
version: 2
code_revision: 277f38439b3ed21ed46908ebbf0625155cd58937
owners: [core]
modules: [product_config, context, evals, models]
related_adrs:
  - docs/adr/0054-context-planning-and-inspection.md
  - docs/adr/0088-controlled-real-provider-suite-baseline.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_coding_workflow_instructions.py
  - tests/product_config/test_agent_context.py
  - tests/product_config/test_server_and_cli.py
  - tests/evals/test_task_pack_execution.py
  - tests/evals/test_provider_verification_budget.py
supersedes: []
---

# R3：共享编码流程指令与失败后的有效观察

## 1. 需求背景与源码研究

固定`0813c58`的[原真实Suite中断](../validation/provider-suite-interruption-2026-09-30-v1/README.md)
形成8份报告：7份invalid、1份failed，另1份Session取消且没有Trial报告。原Session的纯Reducer回放
确认编码指令v2实际装配，但多个Trial先Patch后只运行一次Profile，缺少修改前基线；两Trial在失败
Profile后反复省略`read_artifact.artifact_id`。一个Trial具有有效失败基线、成功修改和通过的最终检查，
随后累计57752 Token超过原50000上限，严格结果仍是failed。不能将这些事实都归因为工具缺失或模型不会编码。

源码给出三个具体约束：

- [`process_action.py`](../../src/harnessix/product_config/process_action.py)将正常非零退出映射为
  `process_nonzero_exit`，仍发布包含`artifact.artifact_id`的输出引用；这不是Process无法启动。
- [`contracts.py`](../../src/harnessix/artifacts/contracts.py)要求`ReadArtifactInput.artifact_id`，
  `process_id`、路径和空参数不能替代；[安全字段反馈](m09-r3-safe-tool-argument-feedback.md)已保持严格校验。
- [`task_pack_trial.py`](../../src/harnessix/evals/task_pack_trial.py)从原实际调用提取首末Profile检查，
  不能把一次修改后检查补造为修改前基线与最终两次检查。

原`read_file`返回资源不存在或文件类型错误，不等同于工具目录没有公布该工具。原低敏投影中的
`unlisted_tool`是白名单未列入的工具分类，不能作为模型调用未知工具的证明；本次投影单独识别受管Patch。

## 2. 设计目标、非目标与约束

1. 在产品和正式评测共享的Runtime指令中明确首次修改前基线、正常测试失败与Artifact读取的衔接。
2. 明确最终验证必须对应最后一次修改后的工作区；单次检查不能承担两种阶段事实。
3. 对可纠正的参数/路径错误要求根据正式字段和只读目录证据修正，而非原样重复调用或过早退出。
4. 新指令UTF-8不超过原v2的2751字节；Context、Compaction、Tool Schema、批准及50000 Trial预算不变。
5. 记录新版本实际请求映射和Session指纹，但不把离线Prompt检查当作模型遵循率或R3质量提高。

非目标：新任务规划器、完成状态强制门禁、答案注入、自动调用测试、额外重试、改Task Pack/Grader、
扩大预算、重新结算原未知费用、自动重放原Suite或新增部署服务。

## 3. 总体架构与模块边界

```mermaid
flowchart LR
    Product[正式stdio产品] --> Factory[共享Context Factory]
    Eval[正式Task Pack Trial] --> Factory
    Instructions[编码流程指令v3] --> Factory
    Sources[原项目 工作区 环境Source] --> Factory
    Factory --> Runtime[原Agent Runtime]
    Runtime --> Inspection[(原Session Context指纹)]
    Runtime --> Adapter[原Chat或Anthropic映射]
    Adapter --> Model[模型选择新的动作]
    Model --> Tools[原严格工具与审批]
    Tools --> Runtime
```

只修改[`agent_context.py`](../../src/harnessix/product_config/agent_context.py)的指令版本与正文。
`build_product_agent_context`的签名、来源、窗口和压缩策略不变，产品与Eval没有第二份任务专用Prompt。
模型选择动作，原Runtime执行动作；自然语言规则不获得执行权限，也不成为外部结果的来源。

## 4. 核心流程、时序与数据流程

```mermaid
sequenceDiagram
    participant M as Model
    participant R as 原Runtime与Session
    participant P as 固定Profile
    participant A as Artifact Reader
    participant W as 受管Patch
    R-->>M: v3工程流程及当前工具合同
    M->>P: 修改前独立检查
    P-->>R: 确定终态及输出引用
    R-->>M: 非零退出仍是有效失败观察
    M->>A: artifact_id取自artifact.artifact_id
    A-->>M: 原有界低信任输出
    M->>W: 根据源码和日志提交精确变更
    W-->>R: 原批准及执行事实
    M->>P: 最后修改后的最终检查
    P-->>R: 原实际结果
    M->>R: 有依据的交付或具体阻塞
```

图表示指令要求的执行次序，不是新增强制状态机。模型仍可能忽略要求，只有真实完整Suite能验证行为。
基线与修改不能并发提交；独立只读检查可以由模型在同一步提出，不改变原执行并发上限或工具顺序合同。

核心逻辑伪代码：

```text
构造原Runtime片段(kind=runtime_instruction, source=v3, content=固定正文)
沿原Factory读取项目/工作区/环境并核对一致性
按原Context预算形成instructions与Inspection
将Inspection持久化；完整请求沿原公开保护送入原Adapter
模型自行提出Tool Call；原工具严格验证并执行
持久化真实结果；下一步骤保留失败反馈，不自动修正或伪造完成
```

## 5. 接口设计、领域契约与重点字段

| 元素 | 当前职责与限制 |
|---|---|
| `CODING_INSTRUCTIONS_VERSION` | 改为`harnessix.coding-instructions/v3`，绑定新固定正文；不是Tool版本 |
| `CODING_INSTRUCTIONS` | 中文工程流程；说明前后检查、引用取值与失败纠正，不包含Case答案或实例UUID |
| `ProductAgentContext.context` | 沿原`SourcedContextEngine`规划指令及动态来源，不额外读取模型或环境变量 |
| `ProductAgentContext.compaction` | 原触发阈值131072、目标65536、最近2组及最多2048摘要输出均保留 |
| `instruction_fingerprint` | 原完整instructions字节SHA-256；新请求包含v3，旧事件保留旧指纹 |
| `artifact.artifact_id` | 正式Profile输出中的引用字段，仅由模型原样转交`ReadArtifactInput.artifact_id` |
| `state/stop_reason/returncode/complete` | 原真实Process事实；非零退出、启动失败、截断和未知副作用不能混同 |

没有新增公开DTO、配置、数据库表或迁移。固定指令版本变更会改变Fragment身份与请求指纹，不能冒充
旧版本复验或跨版本拼接成绩。字节护栏是输入尺寸检查，不声明供应商精确Tokenizer用量下降。

## 6. 失败、恢复、取消与超时

- 正常退出且`process_nonzero_exit`：读取实际诊断并继续定位；不是把失败检查计为通过。
- `read_file`资源不存在/类型错误：使用已公布只读目录工具定位；不绕过路径策略或猜造路径。
- `tool_invalid_arguments`：按正式Schema与安全字段反馈纠正新调用；不自动补参数，不原样重复。
- 启动失败、未知状态、输出不完整：明确未验证结论，不能据此宣称测试通过。
- 审批拒绝、取消、超时、预算耗尽：沿原终态和停止合同，不产生额外模型请求或自动副作用重放。
- 重开Session：旧Turn/旧指纹不重写，后续新Turn使用当前共享v3；原Compaction恢复合同不变。

没有从提示词新增隐式重试，也没有新阶段状态可恢复；恢复权威仍是原持久事件、批准和效果事实。

## 7. 持久化、事务与可观测性

Context指纹、来源摘要与预算Inspection沿原`ContextPrepared`事件持久化，不保存新指令正文到审计快照。
Chat映射生成首个system消息，Anthropic映射生成system字段，两者必须与实际`ModelRequest.instructions`
逐字节一致。源码、指令摘要、RED/回归和只读原Session投影独立固定，原失败报告保持只读。

错误分类不改变：测试失败仍是Process/Tool失败，Task评分仍由原Grader决定；离线送模映射不是供应商
实际收包或费用证明。未决费用保持停止线，0次新增真实API请求。

## 8. 安全、权限与信任边界

Runtime规则仍高于项目来源；AGENTS目录作用域、用户原修改、Secret公开保护、固定Profile及受管Patch
保持原规则。工具/仓库/历史摘要属于低信任资料；日志建议不获得权限，不能跳过审批、预算和Sandbox。
模型只能使用当前目录公布的工具，不能自行获得Shell、联网、安装或公网Push能力。
前置内容摘要仍取自可信完整快照，分页revision不能替代SHA-256，缺少摘要时不得修改。

## 9. 测试设计、验证与验收

| 场景 | 验收依据 |
|---|---|
| 原v2与新流程要求不一致 | 同一新测试先在原实现RED，原件保留 |
| 9项实际失败衔接要求 | 从实际Factory产出的Runtime片段检查，不只测试源码字符串 |
| 大小和安全边界 | 原2751字节上限、可信摘要、目录作用域、批准、停止与Compaction断言 |
| 新建与重开Turn | 真实SQLite持久化、正式Runtime、两种请求映射及指纹逐字节核对 |
| 默认stdio产品与正式Eval | 既有组合根回归，保持同源Context与严格评分/批准 |
| 已有可纠正失败、Provider协议、费用保护 | 独立并行运行原测试，不改断言、预算或价格 |
| 完整R3质量 | 新干净候选、新预注册、3仓10 Case/20 Trial、至少12严格成功、每仓成功及零越界 |

完整Suite不执行前，不声明Prompt整改提高了成功率。真实测试必须先按原规则核对费用和宿主环境；
不得回填基线、挑选成功Trial、跨版本拼接或放宽原验收标准。

## 10. 源码映射、部署兼容、回退与风险取舍

推荐阅读：[`agent_context.py`](../../src/harnessix/product_config/agent_context.py) →
[`Context合同`](../../src/harnessix/context/contracts.py) →
[`Runtime`](../../src/harnessix/agent/runtime.py) →
[`Chat映射`](../../src/harnessix/models/_chat_mapping.py) /
[`Anthropic映射`](../../src/harnessix/models/_anthropic_mapping.py) →
[`流程回归`](../../tests/product_config/test_coding_workflow_instructions.py)。
产品接线见[`server.py`](../../src/harnessix/product_config/server.py)，正式评测见
[`task_pack_trial.py`](../../src/harnessix/evals/task_pack_trial.py)。

随原内部RC Wheel发布，无依赖、配置或Store升级。回退使用原受控候选程序，不改写旧Session或预算。
选择小范围共享指令整改而非新图编排/强制完成框架：可直接针对已观察行为，并保持产品架构收敛。
代价是不能硬保证模型遵循规则；真实质量、累计Token控制、原费用核对及商用门禁继续开放。

## 11. 实测失败后的目录与收尾约定（v7）

完整新候选20 Trial的严格成绩仍为1/20：7次正式未知工具拒绝，13个无有效基线／最终检查对的
Trial；正常失败Profile已有完整诊断预览时仍追加日志读取。分类可重叠，不能等同Docker故障。
目录广告、历史映射和解码共用[`tool_alias`](../../src/harnessix/models/_history.py)，别名
基于精确逻辑名生成；不能将拒绝的原始模型名字或参数重新写入持久历史来修复提示。

本次仅修改共享`CODING_INSTRUCTIONS`为v7：实际调用逐字取本次目录`name`；根据预览决定是否
读取Artifact；确定允许修改路径；最终检查后依次`git_status`、`git_diff`、交付正文。
不改别名算法、拒绝DTO、工具数量、Task Pack、Grader、50000 Token、60／120秒或审批合同。
2751字节原护栏及新建／重开Turn、Chat／Anthropic映射、目录身份和Token发布边界均回归。
提示词送达证明不是模型遵循率；新真实Suite独立记录，不回填旧报告、不合并候选成绩。

## 12. v7 真实结果与 v8 低歧义输入／反馈

v7 的新独立 Suite `5d865146-af93-48cc-8754-59b848f54a6d` 完整20 Trial，严格1/20、必需测试7/20，
1 passed／7 failed／12 invalid，质量没有达标。141新增请求均completed，估算3.956308元、非账单；
同一38元累计剩余29.102992元，原60元周期和20.77824元旧预留不变。204条历史前缀保留。
正式20报告与结果投影封存在外部 `r3-full-quality-workflow-20261009-v2`，清单SHA-256为
`1d37a8eb3876bed8e4504beb10b40ed71938559e8faffadec81f7767e48918bc`；旧失败不合并、不删除。

**根因：** 15次Artifact读取中10次与已展示、未截断的Process预览逐字节相同；通用错误仍建议
查审计记录。首轮实际输入4714～4763 Token，输入占本Suite总Token98.35%。完整Descriptor
的10490是宿主UTF-8字节估算，不是实际wire或供方Token；重建首Case广告10工具为8917字节。
只在模型输入与已验证结果反馈处收敛，不能删除工具或审计来减少表面数字。

**实现与正式边界：**
- [`tool_alias`](../../src/harnessix/models/_history.py)直接广告合法ASCII原名（1～64字符）；含点号、
  Unicode或超长名字仍用原精确字节摘要别名。拒绝历史占位名保留专用含义，不直接广告。
  目录、历史重编码和反向映射共用同一算法；自然 canonical／fallback 碰撞也先于网络拒绝。
  仅改变Provider临时名字，不改Session业务名、Tool版本、审批身份或执行权限，不接受旧别名作隐藏后门。
- [`terminal_result`](../../src/harnessix/trusted_actions/agent_gateway_output.py)只有在原Owner输出、
  Router双Hash、Secret和正式DTO均通过后，对已知Process非零正常退出提供固定反馈。
  两路预览可见、未截断且归档完整时提示无需重复读取；不完整／null仍允许有界读取原Artifact。
  不复制日志正文到错误，不改变失败状态、错误码、retryable、归档引用或审计；v1、无Owner、超时和
  UNKNOWN不获“完整诊断”声明，也不自动重放。恢复同样先验证后反馈。
- Profile描述指向已有预览。内部共享指令v8用等价英文表达工程闭环，明确按用户语言回复；
  文档仍简体中文。原每条信任、权限、审批、SHA取值、基线／最终检查、依赖顺序、分页与失败规则
  逐条保留，原2751字节护栏及Context／压缩阈值不变。英文切换不是另增权限或供方容量承诺。

**负控与交付：** [目录身份矩阵](../../tests/models/test_tool_alias_identity.py)含合法名、大小写、Unicode、
64／65字符、占位名、真实自然碰撞、两SDK流、原Kernel及持久原名；未知名字／错误大小写不具执行权。
[反馈矩阵](../../tests/trusted_actions/test_process_preview_projection.py)核对两种来源／Owner与inline、
完整／截断／null／归档未完整／v1／超时／UNKNOWN，保留原输出、错误分类、双Hash和不重执行断言；
原坏DTO与Hash负控仍执行。[共享Context测试](../../tests/product_config/test_coding_workflow_instructions.py)
核对原约束逐条送达、新建／重开持久指纹及Chat／Anthropic实际映射。
离线送达、字节节流或反事实估算不代表模型质量达标；必须在原Task Pack／Grader／50000 Token和
完整20 Trial下另行实测，不回填当前1/20。

最终候选非editable安装包关联回归 **1228 PASS**（三个record_property警告），564生产成员在源码／构建输入／Wheel／安装件间逐字节一致；Wheel SHA-256 `e33d6f8e804d1d519df589ce3954072bb220877629727b0ea9afa31b56404ea4`。不包含R4存量失败的旧阶段观察测试，不据此宣称全仓绿或真实质量通过。

## 13. v8 中止事实与审批前确定拒绝整改

新独立Suite `e1b44eec-18b1-4f50-afb3-08791b3272de`只发布14/20份报告：5 passed／3 failed／6 invalid；部分严格5个、必需测试8个，不能报成完整20评分。第八Case第七请求为`provider_rate_limit`，没有response_id/完整usage；107新增请求中106 completed、1 unknown，已知估算2.706172元、新预留20.77824元均非账单。旧记录保留且终态停止、不重试。外部原件包 `r3-feedback-quality-interrupted-20261009-v1`，清单SHA-256 `41dc49994430e2bf42443c12aa2c047e106620690040d18ea14fc07ead6b7cfa`。

用户随后明确忽略该次费用、重新授予百炼验证60元。新独立周期 `9bd01f96-6db5-4c89-800a-18380eb5bb73` 从零开始；原452请求、已知估算12.329136元和41.55648元未决预留原件不改写、不假结清。新的未决仍停止，不自动追加额度。

### 13.1 根因与失败语义

三项`uncertain_effect`实为Patch审批前摘要不匹配：原调用目标存在，但expected_sha256不等于冻结Source摘要，且没有匹配的完整read_file证据。持久Route停在pending_approval，审批、执行操作、事务、Profile及changed_files皆0。历史内层异常已被归一化，不能把后继离线复现当历史原始错误；可确认未进入Executor，而非Docker/Profile失败。

原链：`_validate_mutation`前置拒绝→Review通用错误→没有专用失败ToolResult→Runtime对未结算写调用保守unknown。整改不改全局unknown：仅原纯SHA检查的私有工厂附加进程内来源标记、原内置Review绑定Plan，Gateway核对精确类型/来源标记/Review/内置Binding及当前未批准Route，原Router拒绝CAS成功后才返回failed。错误反馈只给固定字段规则，不含参数、路径或第三方异常正文。

```mermaid
flowchart LR
  A[冻结Source SHA校验] -->|已知不匹配| B[原Review绑定专用拒绝与Plan ID]
  B --> C{精确来源且仍未批准}
  C -->|是| D[原Router决定 CAS关闭Route]
  D -->|确认denied| E[failed ToolResult 可纠正新调用]
  C -->|否或状态竞态| F[原保守失败与unknown边界]
```

实现入口：[纯前置校验](../../src/harnessix/delivery/trusted_action.py)、[专用错误](../../src/harnessix/delivery/workspace_patch_errors.py)、[原Review](../../src/harnessix/product_config/workspace_patch_review.py)、[拒绝结算](../../src/harnessix/trusted_actions/preparation_rejection.py)。不自动批准，不修改旧Run/Route，不重放unknown，不放宽SHA或Source验证。

### 13.2 无验证能力损失的wire节流

[临时Schema投影](../../src/harnessix/models/_tool_schema.py)只在标准Schema节点去除字符串title注解；不删除名为title的业务属性，不改const/enum/default/examples/扩展数据、引用、必填/类型/范围。未知方言/词汇和错误类型标题原样保留。两Provider使用同一深复制投影，原ToolDescriptor、指纹、Session和输入校验不变。九个实际内置输入Schema少865 UTF-8字节，**不是Token实测或质量承诺**。原单次Context容量与累计预算未调整。

### 13.3 限流处置边界

[验证宿主](../../scripts/run_engineering_provider_suite_budgeted.py)可选`--minimum-request-interval-seconds`（默认0、0～60有限数），同一Suite共享[准入节奏](../../scripts/provider_verification_guard.py)，策略绑定进Provider证据指纹。等待在费用预留前，取消回收整个锁临界段；不生成已发送/费用记录，不自动重试、不延长Turn，不是跨进程或账户级限流。缺少本次服务子码，不能确定RPM/TPM或其他调用方根因；[百炼官方限流说明](https://help.aliyun.com/zh/model-studio/rate-limit)不能替代账户实际配额证据。

回归：[SHA拒绝](../../tests/product_config/test_patch_precondition_rejection.py)、[Schema等价与两Provider](../../tests/models/test_tool_schema_projection.py)、[准入取消/费用/锁](../../tests/evals/test_verification_request_pacing.py)。源级定向回归分别为SHA拒绝97 PASS、Schema及映射240 PASS、准入及原费用链116 PASS。负控覆盖直接构造同类错误、泛化同code、外部Review、已批准/执行态、审批竞态；原Route拒绝后、Session失败反馈落盘前的崩溃，通过原恢复链得到failed且Executor调用0。此进程内标记不是恶意宿主Python代码的隔离边界。三类改动须以最终安装件回归和新完整20 Trial验收；不回填部分成绩、不缩小分母，缺失和业务失败均保持开放。
