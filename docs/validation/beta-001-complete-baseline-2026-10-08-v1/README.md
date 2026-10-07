---
doc_type: validation-evidence
status: current
version: 1
code_revision: ba6171f32e5575be727364d166001ca0619db6a0
owners: [core]
modules: [sdk, product_config, evals]
related_adrs:
  - docs/adr/0071-headless-app-server-and-sdk-lifecycle.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/tools/test_scoped_runtime.py
  - tests/product_config/test_agent_context.py
supersedes: []
---

# BETA-001 未整改副本完整后端测试基线

## 1. 结论与背景

`FAIL_FULL_BACKEND_BASELINE_WITH_NETWORK_APPLICABILITY_GAPS`。
[前一隔离前测](../beta-001-isolated-baseline-2026-10-08-v1/README.md)的18项认证/API测试通过，
不能证明全部业务回归通过。本次保留原Maven测试发现，不按结果筛选用例，在新的物理副本运行完整现有后端测试。

76个测试类、350项测试：302通过、9失败、39错误、0跳过；206.299秒，进程exit1，未超时。
没有登录整改、Harnessix受控Profile、浏览器验收或Beta通过；任务完成数仍0。

## 2. 输入与执行合同

输入为冻结初始Git版本`cd0cf278fce916db8ea19378588490c795e5307f`来源的423件私有未整改合成副本。
从已审查前测输入复制，不读取原工作树或整改参考件，不复用旧target、日志、测试结果或整改答案。
逐件验证来源摘要和独立物理身份；全部423件源文件执行前后摘要一致。

Java17、离线Maven、缓存依赖与原认证前测一致，继续使用锁定Byte Buddy 1.14.19启动Agent。
只删除原前测命令的`-Dtest`筛选器，不改变业务/测试源码、断言、Mockito策略或依赖版本。
运行前冻结600秒期限、无重试、结果全部保留。测试配置和合成凭据仅私有保存，不放行模型外发。

## 3. 隔离与安全边界

网络全部拒绝，文件写入只允许新验证根和设备节点；原业务、参考件和Keychain读取拒绝。
HOME、TMPDIR、日志和业务输出置于新根；空环境和既有合成配置不继承真实账号或供应商凭据。
实际探针确认本根写入允许，根外写入、原业务及参考件读取、loopback连接均拒绝。

该策略不等同于产品Container Profile，也不证明原整个目录所有正文长期未变。
没有安装或运行原应用，没有连接旧业务数据库，没有模型、Keychain或预算Owner调用。

## 4. 结果、失败分类与适用性

| 结果 | 数量 | 可证明的范围 |
|---|---:|---|
| PASS | 302 | 本次未整改隔离输入的实际通过项 |
| failure | 9 | 断言失败，根因尚未认定 |
| error | 39 | 其中29项SocketException包含Operation not permitted，另10项尚未归因 |
| skipped | 0 | 没有依据结果排除用例 |
| 认证相关类 | 25，全部通过 | 是350项的子集，包含前18项；不叠加为额外测试数量 |

29项网络权限错误与禁网合同不适用有观察关联，不能据此将全部错误归为环境问题。
其余19项失败/错误保持未归因；不能作为历史既有失败已确认、登录缺陷已定位或可豁免回归。
全量日志、每个原Surefire报告和完整失败nodeid集合私有封存；公开资料不包含错误正文或业务配置。

后继网络夹具必须先设计隔离适用性方案，避免简单放开宿主loopback而触及现有业务服务。
不能为取得PASS删除测试、放宽断言、追加无界权限，或按新结果选择样本。

## 5. 证据、持久化与费用

[事实](facts.json)、[评审包](REVIEW_PACKET.md)及公开摘要清单与私有执行收据绑定。
私有Manifest封存170件命令、配置、策略、探针、原日志、JUnit及结构化结果；生成目录不混入源码初始清单。
本次无模型请求，不改变共享60元账本。费用、R3与Beta结果各自保持原有范围。

## 6. 后继整改验收

后继修复前须解决测试适用性并冻结同条件前测；修复后比较完整失败集合，不能只比数量。
登录安全及必需业务路径的失败不能自动豁免。实际Agent提案、人工审批、受控执行、
Console/Request Payload、取消/恢复和使用者验收仍开放。
参见[任务契约](../../operations/pilot-tasks/001-login-password-protection.md)和
[发布门禁](../../changes/m09-to-v1-release-scope-convergence.md)。
