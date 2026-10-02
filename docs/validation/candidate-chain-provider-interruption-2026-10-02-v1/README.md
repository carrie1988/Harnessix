---
doc_type: validation-evidence
status: current
version: 1
code_revision: db7567e318375cb1c1fddc20584ce27eb2ed20bc
owners: [core]
modules: [evals, models]
related_adrs:
  - docs/adr/0088-controlled-real-provider-suite-baseline.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/evals/test_provider_reverification_chain.py
  - tests/evals/test_provider_verification_host.py
  - tests/models/test_tool_alias_identity.py
  - tests/product_config/test_coding_workflow_instructions.py
supersedes: []
---
# 候选链首次实际登记与真实复验中断报告

## 1. 结论与验收边界

固定候选`db7567e318375cb1c1fddc20584ce27eb2ed20bc`已经完成首次实际追加登记，
并启动原3仓、10 Case、20 Trial的新Suite。运行在首个Case的第5次模型请求中断，
终态`cancelled`，完成Case为0，没有Case或Suite质量完成报告。**本次仍为NO-GO，不是新0/20评分，也不是质量通过。**
原`7bbce10`完整Suite的严格成功0/20、必需测试通过1/20保持，不覆盖、不续跑。

本轮5次请求中4次取得完整Usage，价格估算合计0.096616元；第5次失败后费用无法确认，
其20.77824元预留全额保留。原20.77824元未知费用预留也保留，两项均未认定为实际费用。
没有自动重试，没有重新启动Suite，没有释放未知预留。

## 2. 需求、方案与源码映射

管理能力及总体架构沿用[候选链总体与详细设计](../../changes/m09-r3-reverification-candidate-chain.md)：
原Grant、首次绑定和完整请求前缀不可变，显式追加只授权末尾Suite。
本包验证该实现的实际登记和新未决停止语义，不引入新的管理API、预算豁免或产品功能。
正式评分仍要求20 Trial中至少12项严格成功、至少12项必需测试通过，且每仓至少一项严格成功。

| 实际入口 | 源码职责 |
|---|---|
| 管理CLI | [`authorize_provider_reverification.py`](../../../scripts/authorize_provider_reverification.py)：私有追加计划、有限结果，不发送模型请求 |
| 候选链合同 | [`provider_reverification_chain.py`](../../../scripts/provider_reverification_chain.py)：直接前驱、全部请求前缀及累计费用 |
| 原预算Owner | [`provider_verification_budget.py`](../../../scripts/provider_verification_budget.py)：独占、原子登记、先预留后发送、未知不退款 |
| 请求Guard | [`provider_verification_guard.py`](../../../scripts/provider_verification_guard.py)：完整Usage、模型与响应终态一致才结算；否则取消Suite |
| 正式运行宿主 | [`run_engineering_provider_suite_budgeted.py`](../../../scripts/run_engineering_provider_suite_budgeted.py)：固定Source/镜像/价格预检、Keychain内存注入和正式Suite |
| 官方Adapter | [`openai_chat.py`](../../../src/harnessix/models/openai_chat.py)：原请求协议及有界单次尝试 |

架构、管理流程、时序及费用数据流见原设计对应的
[架构图](../reverification-candidate-chain-2026-10-02-v1/architecture.png)、
[流程图](../reverification-candidate-chain-2026-10-02-v1/binding-flow.png)、
[时序图](../reverification-candidate-chain-2026-10-02-v1/registration-sequence.png)和
[数据流图](../reverification-candidate-chain-2026-10-02-v1/cost-data-flow.png)。
图描述同一已实施管理合同；实际模型传输中断由本报告的运行事件补充，不重新绘制目标架构冒充执行证据。

## 3. 实际流程、持久化与数据

1. 在新的固定源码树准备锁定依赖，冻结1043项输入；关联预检188通过，前后无漂移。
2. 核对原价格窗口、固定缓存镜像和Keychain凭据；未拉取镜像，凭据未打印或存储。
3. 封存原账本完整字节，以原Owner执行一次`--append-binding-plan`。
4. 耐久回读确认：仅Schema v2升级v3和追加一条候选；原136条请求、首次绑定、原Grant及其他字段不变。
5. 启动新Suite，不使用`--resume`；原TaskPack、模型、4096输出上限、单次尝试和评分不变。
6. 前4次请求完整Usage结算；第5次出现传输失败且无可靠结算事实，原Guard保留全额预留并取消Suite。
7. 读取权威终态后停止，不依据观察超时重新启动，不为补齐报告重放请求。

管理登记是私有文件Owner的独占原子发布；每次请求是独立先预留、发送、结算。
费用账本与Suite完成报告具有不同覆盖范围：没有完成Case时，运行报告的`known_cost_amount`为0，
**不能据此将已发生的4次请求估算0.096616元改写为免费或零成本。**

| 字段／事实 | 实际值 |
|---|---|
| 候选／绑定 | 新Suite及新binding ID，见[facts.json](facts.json) |
| 实际模型请求 | 5次：4 completed、1 unknown |
| 新已知价格估算 | 0.096616元，不是供应商账单 |
| 新旧未知全额预留 | 各20.77824元，合计41.55648元 |
| 原周期已知估算 | 3.646408元 |
| 原70元周期剩余空间 | 24.797112元，已经扣除两笔预留 |
| 原40元Grant累计占用 | 22.901924元，包含新增未知全额预留 |
| 原Grant剩余空间 | 17.098076元；费用空间不豁免未决停止规则 |
| 真实质量完成报告 | 未产生，不填造成功数或测试分母 |

## 4. 中断观察与失败语义

固定冷Session共70个事件，第5次`model_attempt_finished`记录`outcome=failed`、
`error.code=provider_transport`。这是Adapter归类的传输层失败信号，**不是唯一HTTP根因证明，
也不能证明请求未发送或供应商未计费**。真实终态为取消，不改写为完整Suite任务失败。
原Guard在缺少可靠结算事实时保留最大预留并停止；管理登记成功不允许覆盖这一停止线。

补充诊断仅在运行终态且没有WAL/SHM时采用SQLite`immutable=1`，主库SHA前后一致、未创建辅助文件。
该有限元数据投影未独立验证Session MAC，不将它提升为新的认证执行能力或质量评分证据。
不公开模型正文、工具参数、Artifact正文、数据库文件、原配置或凭据。

## 5. 验证、恢复与下一步

- 已验证：首次实际登记、原请求及费用事实保留、新运行身份、预检188通过、1043输入零漂移、原未决停止线生效。
- 保留的验证宿主初始失败：一次模块名输入错误、一次测试选择器路径错误；仅修正验证命令，未修改源码或断言，原日志保留。
- 独立中断复核尚未完成；底层传输根因、实际账单及完整20 Trial质量均未证明。
- 当前不继续发送请求、不自动追加候选、不重放未知请求、不按0结算、不删除失败或预留。
- 后继费用处置需要可核对的供应商Usage／账单证据；其他离线研发、Windows及完整Git交付继续推进。
- 原始资料限私有归档，公开文件提供有限事实、原件SHA及明确覆盖范围；估算不替代实际账单。

## 6. 验证包索引

| 文件 | 内容 |
|---|---|
| [facts.json](facts.json) | 实际登记、终态、费用及证明边界 |
| [verification.json](verification.json) | 原件摘要、预检结果和未建立项 |
| [review-packet.json](review-packet.json) | 当前NO-GO及下一步，不冒充独立放行 |
| [manifest.json](manifest.json) | 完整公开文件摘要，排除自哈希 |

该包不关闭R3、Windows消费者、完整Git产品交付、独立Beta或最终R1～R6商用门禁。
