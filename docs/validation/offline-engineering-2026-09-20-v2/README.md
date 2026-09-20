---
doc_type: validation-evidence
status: historical
version: 1
code_revision: 505bc537f74bd59e891c605ff4114856991f1783
owners:
  - core
modules:
  - evals
related_adrs:
  - docs/adr/0082-multi-repository-eval-suite-and-transcript-evidence.md
  - docs/adr/0087-deterministic-offline-eval-suite-composition.md
related_tests:
  - tests/evals/test_task_pack_suite.py
  - tests/evals/test_offline_suite_runner.py
  - tests/integration/test_task_pack_execution.py
supersedes: []
---

# 工程Task Pack v2完整离线Suite验证证据

## 1. 证据定位

本目录冻结0.9.2d3在代码Revision `505bc537f74bd59e891c605ff4114856991f1783`上的确定性离线
Suite证据。它证明固定工程Task Pack v2可以沿正式Suite、Case、Agent、Session、产品Trusted Action、Artifact、
Grader和Campaign主链完成10个Case、20个Trial，并从两个Suite提交崩溃窗口恢复。

Recorded Provider按Wheel外Golden构造确定性模型事件，因此本证据只证明执行、恢复、聚合和脱敏链正确，不证明任何
真实模型的软件工程能力。真实Provider基线属于0.9.2e。

## 2. 固定身份

| 项目 | 固定值 |
|---|---|
| CI | [35483905418](https://github.com/carrie1988/Harnessix/actions/runs/35483905418)；最终Attempt 2成功 |
| Harnessix Revision | `505bc537f74bd59e891c605ff4114856991f1783` |
| Suite ID | `313e8aa5-8299-563d-a644-d955989e5f0a` |
| Task Pack | `harnessix-engineering/v2` |
| Pack SHA-256 | `6ab16bf0a305c5d5a590be02f028184a9510e941146f3b8d27a3ef99ee7b7c46` |
| Plan Fingerprint | `e5ca098c6a7f0354f5b1ce399d2b58da51e29507239ffc66b78fcb5b889958c1` |
| Report SHA-256 | `374c3c3c89b1dd5eb87acd674e3930cf07f7f532b7841e2cb56c87d8466df3ad` |
| 执行环境 | Linux固定Digest Python/Node Container；`network=none` |

## 3. 验证结果

| 指标 | 结果 |
|---|---:|
| Repository | 3 |
| Case | 10 |
| Trial | 20 |
| 通过Trial | 20 |
| 测试通过Trial | 20 |
| Provider打开次数 | 20 |
| Provider请求次数 | 120 |
| 模型步骤 | 120 |
| 自动审批 | 60 |
| 人工干预 | 0 |
| 首Case证据提交崩溃恢复 | 通过 |
| Suite报告发布崩溃恢复 | 通过 |

首轮v1运行暴露两个Test Case依赖未跟踪新文件。修正没有放宽Workspace Patch或Grader，而是保留v1原字节并发布
带受版本控制失败测试基线的v2。本次20个Trial均只产生允许路径内的Tracked修改。

## 4. 证据文件与摘要

| 文件 | 原始文件SHA-256 | 内容 |
|---|---|---|
| [`suite-plan.json`](suite-plan.json) | `b65ce5f23e56694b6252f1ac1faef7258eef78f77e92f0b6a8da79c10a3a46ef` | Suite、Case、Campaign和环境计划 |
| [`suite-report.json`](suite-report.json) | `374c3c3c89b1dd5eb87acd674e3930cf07f7f532b7841e2cb56c87d8466df3ad` | 20 Trial可重算低敏报告 |
| [`evidence-manifest.json`](evidence-manifest.json) | `954b2ab10f561960f1ef538c7fc39c9450a650be55bbd8ca70322cfa25705809` | Pack、Revision、摘要、计数和恢复标志 |

下载GitHub Artifact后，文件权限需恢复为`0600`再调用生产Reader；GitHub Artifact和Git不保存私有文件权限语义。
冻结前已用严格合同重新解析Plan、Report和Manifest，重新计算Pack、Plan及Report摘要，并递归执行公开字段、绝对路径、
Prompt、参数、工具输出、Diff、Secret、Workspace和私有运行身份拒绝检查。

## 5. CI说明与适用边界

固定Container Job在首次Attempt即完成集成测试、完整Suite和Artifact上传。首次Windows Job的既有ConPTY测试已产生
正确Unicode输出与退出内容，但租约被观察为`UNKNOWN`；失败Job复跑后通过，最终CI结论为成功。该跨平台间歇现象不参与
离线Suite结果计算，仍应在后续可靠性工作中持续跟踪。

本证据不得外推为真实Provider质量、大型仓库能力、三平台Container执行、发布SLO或1.0商用就绪结论。
