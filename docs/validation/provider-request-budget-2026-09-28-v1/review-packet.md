---
doc_type: validation-evidence
status: current
version: 1
code_revision: 90de93f565ea88679e54242ee6f1771e9be721b7
owners: [core]
modules: [evals, models]
related_adrs:
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/evals/test_provider_verification_budget.py
  - tests/evals/test_provider_verification_host.py
supersedes: []
---

# R3 验证请求预算审查包

## 固定版本与审查范围

- 生产实现：`7b4219a15fe474c1721579fc30839a4206494036`。
- 最终测试候选：`90de93f565ea88679e54242ee6f1771e9be721b7`；仅修改明确非凭据Canary，src/scripts原字节相同。
- 目标：原预算持久预留、官方Adapter单尝试、费用未知停止、双层恢复绑定和正式宿主接线。
- 非目标：通用产品计价、账单/账户硬限额、Windows宿主预算、自动镜像下载、评分整改或商用封板。

## 必读源码与调用链

1. [宿主](../../../scripts/run_engineering_provider_suite_budgeted.py)：默认禁网 → 有限配置/价格 → 原Scope → 固定镜像 → 账本 → 凭据。
2. [账本](../../../scripts/provider_verification_budget.py)：原唯一active周期、私有权限/ACL及锁 → 原字节比较 → 预留/结算可靠发布。
3. [Guard](../../../scripts/provider_verification_guard.py)：Suite取消托管迭代、唯一Attempt/Usage/模型校验、源关闭、结算后成功。
4. [正式Suite端口](../../../src/harnessix/evals/provider_suite_execution.py)：默认路径兼容，受托Factory和摘要成对，Case/Suite同一绑定。
5. [官方Adapter](../../../src/harnessix/models/openai_chat.py)：Started先于网络、SDK重试零、源关闭；不是任意Provider形状认证。

## 重点问题与证据

| 审查问题 | 判定/证据 |
|---|---|
| 是否在持久预留前发送？ | NativeAdapter MockTransport断言；预留同步两故障点发送零次 |
| Suite取消能否中断本次阻塞IO？ | 原红测试超时，修复后独立Suite取消和父Task取消均关闭源 |
| 未知是否当零/退款？ | 不完整用量、别名、超限、截断、重试等保留全部占用，重开拒绝 |
| 能否结算失败但发布成功？ | 两结算同步故障点不发布ResponseCompleted；replace后仍按实际字节保守处理 |
| 是否重置原额度或改历史？ | Fixture原请求及allocation保持；实际原预算未读取/写入 |
| 恢复能否偷换Guard？ | Guard身份与原配置合成摘要同时进入Case和Suite；原漂移回归保留 |
| 是否把Fake模型当作真实质量？ | 所有Adapter测试为MockTransport；实际宿主镜像拒绝，新增请求零，原0/20不变 |
| 扫描失败是否被豁免？ | 保留一次合成假值命中，改用明确Canary；固定规则与白名单未变 |
| 能否据此宣布Windows/1.0？ | 不能；原生三平台、恢复、权利、质量、Beta及封板仍开放 |

## 接受与拒绝条件

接受本切片要求：固定源码、Guard/Owner故障与取消专项、正式Adapter及宿主接线、受影响回归、
兼容与冻结检查、实际图示和资料完整性都可核验。计数重叠不相加，版本变化不混写执行证据。

拒绝商用关闭：R1～R6任何退出条件仍缺失；尤其完整真实Suite未达预注册12/20双门槛、
Windows原生闭环或用户Beta缺少证据时不能封板。这里只接受有限请求保护，不签发总体产品质量证明。
