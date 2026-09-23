---
doc_type: validation-evidence
status: current
version: 5
code_revision: 4097229e9fca700ba6a70c2d05de02cf0bb53a9b
owners:
  - core
modules:
  - documentation
  - evals
  - smoke
related_adrs:
  - docs/adr/0019-controlled-model-smoke.md
  - docs/adr/0047-coding-eval-campaign-evidence.md
  - docs/adr/0048-controlled-real-eval-campaign-execution.md
  - docs/adr/0087-deterministic-offline-eval-suite-composition.md
  - docs/adr/0088-controlled-real-provider-suite-baseline.md
  - docs/adr/0092-reproducible-local-soak-and-release-thresholds.md
related_tests:
  - tests/smoke
  - tests/evals
  - tests/benchmarks/test_soak_many_threads.py
supersedes: []
---

# Harnessix Code验证证据索引

## 1. 文档定位

本索引登记仓库内已冻结的确定性离线Suite、真实Provider Smoke与Coding Eval证据。它只说明证据身份、执行边界、结果和替代关系，
不把历史结论升级为当前版本的生产承诺。当前测试方法和发布判定以[测试与Eval规范](../testing-and-evals.md)为准，
当前运行时行为以[模块详细设计](../README.md#3-当前事实源)为准。

## 2. 证据清单

| 日期 | 证据 | 代码Revision | 固定环境与预算 | 冻结结论 | 关系 |
|---|---|---|---|---|---|
| 2026-09-23 | [macOS 500 Thread单次Soak诊断事实](soak-macos-2026-09-23-v1/README.md) | `d64054638a03bca008f5b392fd0edf23aa4cd63b` | macOS Python 3.13.8、`c16-m48`、500 Thread、3次正式重启、30个列表页样本；无模型请求 | Manifest和原始数值已独立重算；启动P95 440247292 ns、列表页P95 427911708 ns；对应Revision跨平台CI未通过 | 历史诊断证据，不用于冻结Profile，不关闭Soak或商用发布门禁 |
| 2026-09-20 | [工程Task Pack v2真实Provider完整Suite](provider-engineering-2026-09-20-v1/README.md) | `fb4a0ea8f7ffcd14113212fb77b2028143af9914` | 北京精确模型、3仓10 Case、每Case 2 Trial、无自动重试、CNY 40 Trial边界停止线 | 20/20 Trial终结；任务成功0/20、测试通过0/20；81请求、318,478/12,148输入/输出Token、CNY 1.46828完整已知成本 | 0.9.2e当前冻结真实质量基线；证明失败可审计，不证明模型可用成功率 |
| 2026-09-20 | [工程Task Pack v2完整离线Suite](offline-engineering-2026-09-20-v2/README.md) | `505bc537f74bd59e891c605ff4114856991f1783` | 3仓10 Case、每Case 2 Trial、固定Digest无网Container、Recorded Provider零费用 | 20/20 Trial通过；120请求、60自动审批；首Case证据与Suite报告两个崩溃窗口均恢复 | 0.9.2d3当前冻结离线证据；不证明真实模型能力 |
| 2026-09-03 | [百炼北京受控Smoke](bailian-2026-09-03.md) | `9f24961840fa704e7c7a344c648164d8afe793b7` | 北京兼容端点；每请求最多128输出Token；零重试；总计7次请求 | 文本、内存工具、审批重开三个固定场景通过；费用和其他Provider未验证 | Smoke独立证据 |
| 2026-09-06 | [Coding Eval v1](bailian-2026-09-06-coding-eval/README.md) | `bbfd446707acbd9f945657ad96a6556d24af5df5` | 固定模型；3个Run；16步骤、20000累计Token、600秒；费用停止线¥10 | 0/3，均因任务Token预算结束；不能形成编码成功率 | 被v2诊断推进，但原证据保留 |
| 2026-09-06 | [Coding Eval v2](bailian-2026-09-06-coding-eval-v2/README.md) | `397542942be8474d99feb190a901e8b336a19bdd` | 固定模型；3个Run；100000累计Token；费用停止线¥10 | 0/3，暴露分页参数错误反馈不可自纠正 | 被v2纠正后Campaign推进 |
| 2026-09-06 | [Coding Eval v2纠正后](bailian-2026-09-06-coding-eval-v2-corrected/README.md) | `7e58c15a4be9780de5837c9706f986a8613b1420` | 同等固定模型、Run数和预算；加入专用分页纠错 | 严格0/3；其中2/3完成代码闭环，但模型不可见最终回答Schema | 被v3消除测量偏差 |
| 2026-09-06 | [Coding Eval v3](bailian-2026-09-06-coding-eval-v3/README.md) | `9d0be66e197a82506cf0d0dcbf59832d8865f1e2` | 同等固定模型、Run数和预算；Prompt公开最终回答Schema | 固定历史缺陷3/3严格通过；不得外推到任意仓库或任务 | 该任务序列的最新冻结证据 |

## 3. 证据谱系

```mermaid
flowchart LR
    Smoke[2026-09-03 Smoke] --> SmokeConclusion[三个固定场景通过]
    V1[Eval v1] --> Budget[预算不适配]
    Budget --> V2[Eval v2]
    V2 --> Feedback[错误反馈不可自纠正]
    Feedback --> V2C[Eval v2纠正后]
    V2C --> PromptGap[模型不可见评分Schema]
    PromptGap --> V3[Eval v3]
    V3 --> Baseline[固定任务3/3严格通过]
    Offline[2026-09-20工程离线Suite] --> OfflineResult[20/20执行与恢复通过]
    OfflineResult --> Provider[2026-09-20真实Provider Suite]
    Provider --> ProviderResult[0/20严格质量基线]
```

图中的箭头表示问题诊断和任务版本演进，不表示后一个目录覆盖前一个目录。所有失败、预算终止和测量偏差均保留，
以便复查架构决策如何形成。

## 4. 固定字段与脱敏边界

每份证据必须固定或显式声明：

1. 代码Revision、执行日期、Provider端点/地域、协议和精确模型；
2. Task、Campaign、Run、参数、重试、Token、时间、请求次数和费用边界；
3. 完成、失败、取消、预算结束和未知成本的分类；
4. 证据文件摘要、聚合方式、适用结论和不能外推的范围；
5. 凭据、Header、供应商正文、私有Session、工作区和工具输出的保存策略。

仓库只保存允许公开的白名单投影。API Key、Authorization Header、供应商响应正文、私有Session和个人环境路径
不得进入Git历史。历史证据发现脱敏缺陷时，应先阻断发布并清理敏感数据；不得以“保持历史原文”为由保留Secret。

## 5. 使用与维护规则

1. 日期化证据生成后保持`historical`，不得覆盖原JSON、Run身份、结果或失败分类；
2. 后续验证创建新目录或新文档，并在本索引记录谱系和适用Revision；
3. 新证据可以取代某项结论，但不能删除产生该结论的旧失败证据；
4. 费用为基于版本化价格和完整Usage的估算时，必须明确不等同于供应商账单；
5. 单个Provider、地域、模型和任务通过，只证明该固定组合，不构成产品级泛化结论；
6. 当前结论必须同时核对对应模块设计、路线图和最新CI，不得直接引用历史数字作为发布状态。
