---
doc_type: validation-evidence
status: historical
version: 1
code_revision: fb4a0ea8f7ffcd14113212fb77b2028143af9914
owners:
  - core
modules:
  - evals
  - models
  - sandbox
related_adrs:
  - docs/adr/0082-multi-repository-eval-suite-and-transcript-evidence.md
  - docs/adr/0088-controlled-real-provider-suite-baseline.md
related_tests:
  - tests/evals/test_provider_suite_cli.py
  - tests/evals/test_provider_suite_evidence.py
  - tests/evals/test_suite.py
  - tests/integration/test_task_pack_execution.py
supersedes: []
---

# 工程Task Pack v2真实Provider完整Suite验证证据

## 1. 证据定位

本目录冻结0.9.2e在代码Revision `fb4a0ea8f7ffcd14113212fb77b2028143af9914`上的受控真实Provider
Suite证据。该运行通过唯一Suite、Case、Agent、Session、产品Trusted Action、Artifact、Grader和Campaign主链，按固定顺序
完成3个仓库、10个Case和20个Trial；所有Turn均正常终结，Provider未失败，Token与成本均完整。

模型没有完成任何Trial，也没有形成通过的必需检查。该结果是严格质量基线，不是基础设施成功率：Harnessix没有重跑挑选结果，
没有放宽Task Pack、Grader或安全策略，也没有人工补造Profile与测试事实。0/20必须保留，以驱动后续Agent策略、工具可发现性和
Prompt改进。

## 2. 固定身份与执行边界

| 项目 | 固定值 |
|---|---|
| 实现门禁 | [CI 35491527318](https://github.com/carrie1988/Harnessix/actions/runs/35491527318)；六个作业全部通过 |
| Harnessix Revision | `fb4a0ea8f7ffcd14113212fb77b2028143af9914` |
| Suite ID | `ae8cdfb4-87ba-4474-bf1c-7858cdf5e631` |
| Task Pack | `harnessix-engineering/v2` |
| Pack SHA-256 | `6ab16bf0a305c5d5a590be02f028184a9510e941146f3b8d27a3ef99ee7b7c46` |
| Plan Fingerprint | `c2d615f20f62305a87779343a5ac15bfd48744770c60d07737987479b07c658b` |
| Report SHA-256 | `232cd80fa7412b886b60ab7ba632fbc321c84d672c9bbb41af150585c4dfab6f` |
| Provider / 模型 | `openai_chat` / `qwen3-coder-plus-2025-09-23` |
| 地域 / 推理模式 | `cn-beijing` / `non-thinking` |
| 价格快照 | 单请求输入不超过32K；输入CNY 4/百万Token、输出CNY 16/百万Token |
| Provider执行 | 宿主HTTPS；每Trial独立Client；串行Tool Call；无自动重试；单请求最多4096输出Token |
| 代码检查 | 固定Container Profile；任务声明的必需检查缺失时按适用失败计分 |
| 费用停止线 | CNY 40，在完整Trial边界生效；不是供应商账户硬上限 |

## 3. 验证结果

| 指标 | 结果 |
|---|---:|
| Repository | 3 |
| Case | 10/10完成 |
| Trial | 20/20完成 |
| Turn正常终结 | 20/20 |
| 通过Trial | 0/20 |
| 适用测试Trial | 20/20 |
| 测试通过Trial | 0/20 |
| Provider失败 | 0 |
| Agent运行时失败 | 0 |
| 人工干预 | 0 |
| 模型请求次数 | 81 |
| 输入Token | 318,478 |
| 输出Token | 12,148 |
| 已知成本 | CNY 1.46828；完整 |
| Trial时延P50 / P95 / 最大值 | 6.343721秒 / 19.687319秒 / 20.316679秒 |

20个Trial均被严格评分为`invalid`，Campaign分类为`eval_infrastructure`；每个Trial同时记录
`correctness`、`eval_infrastructure`、`final_answer`和`forbidden_edit`失败类别。该分类来自现行Grader对缺失必需Profile/
测试事实的定义，不表示Provider、Suite Runner、Session或Trusted Action发生运行故障。全部Trial均有完整Usage和成本，
Suite报告因此可以稳定发布，而不是把质量失败升级成`runtime_failed`。

## 4. 证据文件与摘要

| 文件 | 文件SHA-256 | 内容 |
|---|---|---|
| [`suite-plan.json`](suite-plan.json) | `42b47a69cd0e1bf4f28a34bdebc8d6796dfb089a4603857bed9981c5392725ec` | Suite、Case、Campaign、环境与计价计划 |
| [`suite-report.json`](suite-report.json) | `232cd80fa7412b886b60ab7ba632fbc321c84d672c9bbb41af150585c4dfab6f` | 20 Trial可重算低敏报告 |
| [`evidence-manifest.json`](evidence-manifest.json) | `7b9f1402153bbf8aca0731b9f26a3da1168711cd62c1972c76f0ba20a4289ef1` | Pack、Revision、模型、价格、摘要、Token与成本白名单索引 |

冻结前已完成以下检查：

1. 只发布上述三份JSON，不复制私有配置、Suite State、Campaign、Session、Artifact、Workspace或Container输出；
2. 用正式合同严格重读Plan、Report与Manifest，并验证`report.plan == plan`；
3. 重算Report SHA-256并与Manifest逐字一致；
4. 递归拒绝正文型字段、POSIX/Windows绝对路径和私有运行身份；
5. 验证10个Case均包含精确2份Transcript与2份测试证据，成本完整性为`complete`；
6. API Key只临时存在于受控进程环境，未进入配置、命令历史、日志或Git。

Git不保存私有文件的`0600`权限语义；从仓库复制证据到受限运行目录后，如需交给生产Reader，应由操作者恢复最小权限。

## 5. 适用结论与限制

本证据证明：

- 真实Provider可以通过Harnessix唯一Coding Agent主链完成完整多仓库Suite；
- 终态模型未产生必需测试事实时，系统仍能保留真实失败、连续进度、Token和费用，并发布可重算报告；
- 测试缺失会进入适用分母并严格失败，不会被写成`not_applicable`或人工补证据；
- 在本次固定模型、地域、价格窗口和Task Pack下，实际已知成本远低于授权停止线。

本证据不证明模型已经具备可用的软件工程成功率；相反，固定任务成功率与测试通过率均为0%。它也不能外推到其他模型、地域、
价格、动态第三方仓库、大型仓库、供应商账单精确对账、账户级硬预算或1.0商用就绪结论。后续改进必须创建新Suite与新证据目录，
不得覆盖本目录或选择性重跑本次失败Trial。
