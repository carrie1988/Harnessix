---
doc_type: validation-evidence
status: current
version: 1
code_revision: ef582dacb5609fee90e6b3905998dea8db269c53
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

固定源码候选`ef582dacb5609fee90e6b3905998dea8db269c53`。本交付验证安全前置参数拒绝不充当检查、所有实际Profile结果逐项验证、
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

## 4. 实际验证与发行物

| 验证 | 结果 | 范围 |
|---|---|---|
| 原邻近回归 | 58通过 | 原Case、Suite、Campaign及Provider宿主合同 |
| 新增最终专项 | 107通过 | 分类负例、停止、恢复、前缀和发布窗口；初次104项结果单独保留 |
| 原源码负对照 | 50失败、57通过、0错误 | 实际导入独立导出的7d200b3源码，非人工删除分支的变异体 |
| 初次实际容器/录制链 | 4通过、2未选 | 同Engine、固定原镜像、Workspace挂载和正式Agent/Case持久链；不是真实模型质量 |
| 固定候选容器复验原失败 | 4失败、2未选 | Engine未运行，保留原失败；不是新的编码质量成绩 |
| 启动原Engine后同候选复验 | 4通过、2未选 | 只启动既有Docker应用，无配置或数据修改；仍是显式Engine及录制Provider范围 |
| 源码外实际安装 | 124通过 | 锁定全部产品Extras、实际Wheel和独立site-packages，非editable源码 |
| 完整本机源码回归 | 6283通过、111跳过、0失败/错误 | 6394项，568.540秒，固定ef582源码，无真实Provider开关 |
| 最终治理专项 | 302通过 | 原文档、结构、合同及安全治理回归；与其他测试集合重叠 |

实际Wheel SHA-256：`41a30308b3d5f1baf45bbefe654c713ebdab0b386ba698ba7ae574a57901a939`。
447个包成员逐字节与源码快照及独立安装一致；完整验证后源码字节仍与快照一致。
Ruff及1390文件格式、406源文件Mypy、原结构治理和Schema一致性通过；
仓库与Wheel在内3740输入Secret扫描零发现。测试集合重叠，不相加。
首次测试工具安装使用未缓存版本而失败，后继查询实际版本并使用pytest-asyncio1.4.0；
初次文档检查缺规范语义标题及模块状态，后继补齐而未放宽策略。原日志摘要保留。

后继容器复验时默认及显式Engine端点均无法连接，四项测试失败。启动既有Docker应用后，
核验两端Engine身份相同、显式入口脚本字节不变，同一固定源码和镜像的四项复验通过。
未修改代理、共享或Daemon配置，未删除数据；默认Desktop的Workspace启动路径仍未认证。
原失败与恢复后结果分别登记，不以历史健康或后继通过覆盖前一次失败。

## 5. 预算与原失败保护

实际私有原账本可读取，当前SHA与原冻结记录一致；71条请求中70条已知、一条旧unknown。
已知估算1.74186元，旧unknown预留20.77824元；原70元周期不变。
同一40元复验额度已用估算0.219136元，剩余39.780864元，未重新分配40元。
账本仍绑定原中断Suite，新完整Suite必须明确处置原授权范围；本切片不改授权或请求记录。
估算不是实际账单，预留不是已扣费，工具效果未知不是新增模型费用未知。

## 6. 新完整Suite前置范围

在干净ef582候选上构造新完整3仓10 Case/20 Trial私有配置，费用停止线为同40元额度剩余39.780864元。
源码绑定、固定Task Pack、两原镜像及固定北京计价范围前置已通过；
[官方快照资料](https://help.aliyun.com/zh/model-studio/qwen3-coder-plus)的北京32k内输入/输出价格仍与原Guard一致。
原预算Owner对新Suite身份明确拒绝，且在凭据读取和请求之前返回`verification_budget_unresolved`，
账本字节不变。该记录是准备验证，不是登记授权或开始真实运行。
候选、配置、Suite身份都必须在实际执行时再次核验；不得因后继文档提交或源码变化而静默改配置身份。

## 7. 证据与复现

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

## 8. 发布缺口

新完整真实20 Trial、每仓严格成功、零越界、三平台消费者核心产品链和独立Beta仍开放。
本切片只收敛R3评测故障，版本仍是内部`1.0.0rc1`，不宣称正式商用发布。
