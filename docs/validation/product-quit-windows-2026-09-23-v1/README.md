---
doc_type: validation-evidence
status: current
version: 2
code_revision: 823ceac0c7ec250bb36cd0009946949ad8f094d2
owners:
  - product
modules:
  - product_ui
  - app_server
  - sdk
related_adrs:
  - docs/adr/0078-product-shell-and-recoverable-client-state.md
  - docs/adr/0089-bounded-local-transport-lifecycle.md
related_tests:
  - tests/product_ui/test_app_interactions.py
  - tests/product_ui/test_controller.py
supersedes: []
---

# Windows Product UI退出期限首次失败诊断

## 1. 证据身份与结论

Revision `823ceac0c7ec250bb36cd0009946949ad8f094d2`的[CI Run 35845010214](https://github.com/carrie1988/Harnessix/actions/runs/35845010214)首次尝试中，Windows `windows-trusted-execution` Job在Product UI退出用例失败；其他五个Job成功。[第二次仅重跑失败Job](https://github.com/carrie1988/Harnessix/actions/runs/35845010214/attempts/2)的Windows 512项通过、45项跳过。首次失败的同一测试集合为511项通过、1项失败、45项跳过。**重跑成功证明失败非稳定复现，不证明首次失败原因已经关闭。**

失败用例为[`test_product_quit_does_not_send_turn_cancel`](../../../tests/product_ui/test_app_interactions.py)，结果`CloseReport(clean=False, error_code=controller_connection_close_failed)`。日志未记录外层Deadline到期还是底层Close异常，因此根因未确认。测试使用本地确定性`ScriptedProvider(delay_seconds=10)`，不使用真实模型、API Key或网络。原始CI日志可能含Runner临时绝对路径；本仓库只保存低敏摘要和[证据Manifest](bundle-manifest.json)，不复制日志正文。[评审包](review-packet.json)记录门禁状态。

## 2. 可由源码复核的预算倒挂

[`ProductApp`](../../../src/harnessix/product_ui/app.py)当时传入10秒退出总期限；[`SubprocessAgentTransport`](../../../src/harnessix/sdk/subprocess.py)默认允许10秒优雅关闭加5秒终止收尾；[`AgentApplicationService`](../../../src/harnessix/app_server/service.py)给活动Turn 5秒自然收敛。外层总预算低于底层允许的进程收尾预算，是独立成立的合同不一致。该不一致**可能**解释慢速Windows环境的误报，但首次日志不足以建立排他性因果。

[退出期限详细设计](../../changes/m09-3d-product-quit-close-budget.md)选择两个Product App退出路径共用20秒；不改Server 5秒、Transport 10+5秒或领域`turn/cancel`语义。新Revision的Windows完整Job已复验；旧Revision的首次失败保留为诊断，不因后续绿灯改写。

修复Revision `c3cb22fc8465a7da1022c0bb9cc16ce9a5b7595b`的[CI 35847851813](https://github.com/carrie1988/Harnessix/actions/runs/35847851813)原始尝试与两次重跑六Job均成功，Windows用例连续三次通过；本地全量回归3725 passed、32 skipped，macOS单例重复10次通过。它验证预算调整在现有跨平台回归下有效，**不证明首次失败唯一原因**，也不替代0.9.3d其他场景验收。

## 3. 复核与限制

从GitHub打开Run的首次和第二次尝试，可核对Job ID、终态和测试计数。若重新下载两个原始Job日志，可用[Manifest](bundle-manifest.json)中的字节数及SHA-256核对本次所读版本；GitHub可能在保留期后删除日志，届时仅能核对链接元数据和本仓库低敏记录。

本证据不包含Windows本机时序埋点、进程级Close阶段测量或SQLite收尾栈，因此仅支持“预算倒挂存在”和“首次失败、重跑成功”的结论，不支持“20秒保证彻底修复所有Product UI超时”。后续新Revision若仍失败，须区分Controller超时与底层异常，并继续定位。0.9.3d整体仍未完成。
