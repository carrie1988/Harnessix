---
doc_type: validation-evidence
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [evals]
related_adrs:
  - docs/adr/0086-formal-eval-case-adapter-and-recorded-provider-boundary.md
  - docs/adr/0088-controlled-real-provider-suite-baseline.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/evals/test_task_pack_profile_outcomes.py
  - tests/evals/test_task_pack_evidence_stop.py
  - tests/evals/test_task_pack_execution.py
  - tests/integration/test_task_pack_execution.py
supersedes: []
---

# 固定Profile观测与证据缺失停止验证交付

## 1. 范围与完成边界

本交付验证安全前置参数拒绝不充当检查、所有实际Profile结果逐项验证、
指定证据缺失持久停止Campaign/Suite，以及停止后重开不重放。
完整架构、源码链接、数据与字段、伪代码、流程/时序/数据图、异常和兼容见
[总体及详细设计](../../changes/m09-r3-profile-observation-and-evidence-stop.md)。

本切片没有新增付费请求，没有新的完整20 Trial成绩，不关闭R3或商用R1～R6。
历史完整0/20以及后续中断保持原件；不能将离线断言或真实容器的Recorded Provider链算作真实模型质量。

## 2. 原件求证与后继控制

原中断Session以SQLite只读备份复制到独立私有诊断目录，未修改原数据库和报告。
原Turn包含七个无Action身份/效果的`tool_invalid_arguments`和最后一个`unknown/uncertain_effect`。
原观测函数在取首项基线时抛出`eval_baseline_invalid`；`INTERRUPTED`本身已是可返回终态。
新函数在排除七项安全拒绝后仍因未知结果拒绝，不将原中断变为完成。
仅移除最后未知结果的内存负载控制返回空检查证据；该控制不是实际Trial报告或新的质量成绩。

## 3. 源码与测试责任

| 源码/测试 | 验证责任 |
|---|---|
| [`观测分类`](../../../src/harnessix/evals/task_pack_observations.py) | 原正式Decoder、无审批/输出/效果约束、逐项已知退出及真实首尾检查 |
| [`Case Adapter`](../../../src/harnessix/evals/task_pack_execution.py) | 指定错误停止、前缀保持、重开原因保持及未知费用独立 |
| [`合同`](../../../src/harnessix/evals/campaign_execution_contracts.py) | 新有限原因和生成Schema一致，字段和评分模型不改 |
| [`观测专项`](../../../tests/evals/test_task_pack_profile_outcomes.py) | 无效果拒绝、真实非零退出、中间未知、取消和伪装结果负例 |
| [`停止专项`](../../../tests/evals/test_task_pack_evidence_stop.py) | 原Case/Suite文件发布、重开无重放、前缀和其他异常传播 |

各测试集可能重叠，不累计其计数。容器专项使用原RepoDigest、显式同Engine宿主和Recorded Provider，
不作为默认Docker Desktop路径、Windows/Linux消费者或真实模型验收。

## 4. 预算与原失败保护

实际私有原账本可读取，当前SHA与原冻结记录一致；71条请求中70条已知、一条旧unknown。
已知估算1.74186元，旧unknown预留20.77824元；原70元周期不变。
同一40元复验额度已用估算0.219136元，剩余39.780864元，未重新分配40元。
账本仍绑定原中断Suite，新完整Suite必须明确处置原授权范围；本切片不改授权或请求记录。
估算不是实际账单，预留不是已扣费，工具效果未知不是新增模型费用未知。

## 5. 证据与复现

最终候选的[事实](facts.json)、[Verification](verification.json)、[Review Packet](review-packet.json)
及[Manifest](manifest.json)绑定源码、原件摘要、实际执行结果和发行物；公开目录不复制私有日志或数据库。

```bash
python -m scripts.generate_specs --check
python -m pytest tests/evals/test_task_pack_profile_outcomes.py tests/evals/test_task_pack_evidence_stop.py
python -m pytest tests/evals
python -m ruff check .
python -m mypy src/harnessix
python -m scripts.readability_report --check --check-final-report
python -m scripts.documentation_check
```

正式夹具使用Python3.12和Git2.53，完整默认回归不启用真实Provider环境开关。
源码外安装从实际Wheel导入并验证包成员字节，不用editable源码冒充安装产物。
三个图分别已实际渲染并观察：[总体架构](diagrams/architecture.png)、
[时序](diagrams/sequence.png)、[数据流](diagrams/data.png)。

## 6. 发布缺口

新完整真实20 Trial、每仓严格成功、零越界、三平台消费者核心产品链和独立Beta仍开放。
本切片只收敛R3评测故障，版本仍是内部`1.0.0rc1`，不宣称正式商用发布。
