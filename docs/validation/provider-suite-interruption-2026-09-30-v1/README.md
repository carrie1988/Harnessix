---
doc_type: validation-evidence
status: current
version: 1
code_revision: 0813c581982fddf17503d47a308419035d193ecf
owners: [core]
modules: [evals, models]
related_adrs:
  - docs/adr/0088-controlled-real-provider-suite-baseline.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/evals/test_provider_verification_host.py
  - tests/evals/test_provider_verification_budget.py
  - tests/integration/test_task_pack_execution.py
supersedes: []
---

# 工程Provider Suite中断与费用待核对证据

## 1. 预注册范围与有限结论

固定`0813c58`、Suite `d85c26ca-d21a-4ac4-9a01-f780eb01b038`在请求前冻结原Task Pack v2、
3仓10 Case/20 Trial、北京固定Coder模型、40元Suite停止线及原70元持久预算周期。
原Profile、镜像、Grader与严格任务/必需测试12/20、每仓严格成功和零越界门槛不变。
显式同Engine宿主身份沿用[前置证据](../docker-eval-host-2026-09-30-v1/README.md)，不计默认Desktop验收。

实际完成前4个Case、8个Trial报告：7份`invalid`、1份`failed`，没有严格通过。
第五Case的首个Trial在第二次模型请求失败后取消，未形成Trial报告；Suite保持`stopped/cancelled`。
**完整20 Trial质量验证未完成且未通过，不能发布新完整Suite成绩或称为0/20复验完成。**
原历史完整0/20和第一次两请求审批中断证据均保留；没有选择成功Trial、自动重试或跨版本恢复。

## 2. 执行与数据流

```text
干净固定Revision + 原Pack + 新Suite配置/预注册
  → 原持久预算Owner → 单请求最坏档预留 → 原Chat Adapter → 原Agent/审批/Process
  → Session与原Trial/Case报告（4 Case完成）
  → 第5 Case响应解析失败 → Guard保留未知预留并取消Suite
  → Suite stopped；不生成完整Suite报告，不进入公开完整成绩Publisher
```

[原适配器](../../../src/harnessix/models/openai_chat.py)与
[请求保护](../../../scripts/provider_verification_guard.py)共同形成结果：末次尝试记录
`provider_invalid_provider_output`；实际模型一致、Usage字段完整，但没有符合原Adapter合同的成功终态。
预算保护要求原完整成功终态及用量一致性，因此未把该请求结算为已知金额，而是保留预留。
本证据没有原响应正文，**不能断言具体非法字段或供应商计费结果**，也不能靠Usage完整修改旧账本。

## 3. 费用、失败与恢复边界

本轮46个请求已知结算、1个请求未知；新增已知估算CNY 1.230508。
原周期累计已知估算1.522724，未知保守预留20.77824，原分配70.00未改变。
预留不是实际收费，已知估算不是账单；不能把两者混为“已消费22.30元”。
完整周期已结算61个请求，另1个未知；保留原预算与Owner锁，不退款、不新建周期增加额度。

待核对请求：北京时间2026-09-30 03:31:36，模型`qwen3-coder-plus-2025-09-23`，
响应ID `chatcmpl-0e37b5c5-b544-980b-92fb-bb32f0ecb318`，原Session记录输入4834、输出103 Token。
这些字段仅用于供应商用量核对，不能作为已确认收费或自动恢复授权。后续模型请求保持停止，
离线故障定位与回归不需要费用预留。恢复真实验证前必须沿原正式账本规则处置未决事实；
源码或配置改变后必须新建完整预注册，不能拼接本轮成绩。

## 4. 部分任务结果与根因边界

多数Trial未形成最终Profile检查；有些只在修改后执行一次检查，被原评分器当作首次基线，不能补造最终检查。
第一Case的两Trial在实际失败Profile后，多次调用缺参数的`read_artifact`；一Trial完成修改和通过检查，
但最终预算耗尽而严格失败。还有`tool_not_found`、错误文件类型及未做任何变更的情况。
这是[逐Trial有限投影](facts.json)记录的行为，**不等于固定容器启动失败，也不等于全部模型任务无法修改代码**。
`forbidden_edit`分类还可能来自缺少要求的变更数；不能仅按分类名声称发生未授权文件写入。

产品共享指令已要求修改前基线、最终检查、Schema参数与Git验证；不能把新增测试豁免或评分阈值放宽
当作产品修复。当前需要按真实失败分别定位工具协议、结果可用性、模型行为和Token预算使用；
未知原响应不能通过猜测JSON字段修复。

## 5. 材料、验证与后续门禁

[事实](facts.json)来自正式Report/State Reader及持久Session的有限字段，原文件SHA、配置Fingerprint、
预注册SHA及运行日志SHA可核对。私有Session、工作区、Artifact及模型正文不复制。
[Verification](verification.json)、[Review Packet](review-packet.json)和[Manifest](manifest.json)
绑定已交付文件，明确中断、未知占用和开放项；该投影不是完整质量Publisher的输出。

消费者Windows11、独立Beta、既有原生CI风险及最终同候选发布门禁继续开放；许可证等非功能工作
不阻挡离线功能修复，但不能据此声明正式商用发布通过。
