---
doc_type: validation-evidence
status: current
version: 1
code_revision: 1337734f2536174edcbc583e340dd37e180abef1
owners: [core]
modules: [agent, sdk, product_config, evals, documentation]
related_adrs:
  - docs/adr/0071-headless-app-server-and-sdk-lifecycle.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/agent/test_approvals.py
  - tests/evals/test_beta_task_budget_plan.py
  - tests/governance/test_documentation_policy.py
supersedes: []
---

# BETA-001 登录密码传输保护验收

## 结论

状态为 **`READY_FOR_USER_ACCEPTANCE`**：真实 Harnessix Agent 已完成四个生产补丁分片及人工审批，
隔离副本的编译、聚焦测试、全量回归对照和真实浏览器验收已完成；用户尚未签署最终接受，
因此真实 Beta 完成任务数仍为 `0`。

生产补丁采用一次性挑战与 RSA-OAEP/SHA-256。浏览器登录请求仅包含
`username`、`keyId`、`challengeId`、`ciphertext`，旧明文 `password` 契约明确返回 400，
不做静默降级。

## 关键证据

| 范围 | 结果 |
|---|---|
| Agent 审批 | 登录服务、控制器、安全配置、前端四个生产分片均获批；控制器原候选的宿主后置检查误报仅按 `RECOVERED_EVIDENCE` 记录 |
| 前端 | 4 个测试文件、19 项通过；类型检查和生产构建通过 |
| 后端聚焦测试 | 登录相关三类测试通过，新增明文请求拒绝用例通过 |
| 全量回归 | 基线 `350/332通过/8失败/10错误`；整改后 `351/333通过/8失败/10错误`；失败测试及错误类型集合相同 |
| 浏览器 | 正确密码登录成功且 Console 0 条；错误密码返回 401；两种请求均无 `password` 字段，日志中密码哨兵 0 次 |
| 原项目边界 | 未将原项目设为工作目录，也未读写其中的文件；仅执行目录级 `stat`，目录修改时间未变 |
| 生产 HTTPS | 未验证，不能由本地浏览器结果推导生产部署安全 |

本机交付副本位于
`<本机交付目录>/AIPracticalPlatform-Harnessix-Beta-001-20261011`，
包含中文验收说明、结构化结果、全文件 SHA-256 清单及浏览器成功截图。

## 费用边界

用户于 2026-10-11 重新授权独立的 50 元人民币云模型预算周期；历史费用和预留不结转，
无逐请求授权及用户侧请求次数限制。新周期 `8450814a-8e89-40a7-96de-3cba141a09e0`
创建并由正式账本 Reader 校验时为：请求 `0`、已知费用 `0`、预留 `0`。
本验收收口阶段没有新增付费模型请求。

## 尚未关闭

1. 用户审阅交付副本并明确接受或拒绝；
2. 生产环境 HTTPS/TLS 与部署配置验证；
3. 如需“原目录全树未变”的强证明，另行执行独立前后全量 Manifest，而不是用目录时间代替。

结构化事实见 [result.json](result.json)。
