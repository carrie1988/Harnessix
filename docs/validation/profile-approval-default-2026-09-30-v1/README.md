---
doc_type: validation-evidence
status: current
version: 1
code_revision: 629b07280db58814c77efabef6ec495720c714b7
owners: [core]
modules: [evals, product_config]
related_adrs:
  - docs/adr/0088-controlled-real-provider-suite-baseline.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/evals/test_task_pack_execution.py
  - tests/product_config/test_process_action.py
  - tests/integration/test_task_pack_execution.py
supersedes: []
---

# 固定Profile审批默认参数兼容验证

## 1. 结论与范围

真实Suite `0c5a931e-8b54-4d5e-ac20-9b1ce47f5d03`在首个Trial两次模型请求后，因评测审批适配器
误拒绝合法默认参数而停止；没有完整Trial或Suite报告，**不得登记为完整0/20或通过成绩**。
原历史完整0/20保持。两次请求全部结算，增量估算CNY 0.042192；原70元周期累计已知估算0.292216，
未决占用0。估算不是供应商账单，新尝试不能重置原周期。

修正复用既有产品输入解码器。286项关联离线回归与6项真实容器/录制Provider集成通过，后者0跳过。
这只允许在新干净Revision上重新预注册同一完整20 Trial；**真实质量及商用发布仍未通过**。
本报告的源码身份由[测试事实](facts.json)中实际文件SHA绑定，头部Revision为故障基线。

## 2. 根因、调用链与保持的边界

```text
真实模型 → 固定run_profile Tool Call，只有profile字段
  → Session waiting_approval → 旧评测器要求显式selectors=[] → eval_approval_denied
  → CLI verification_host_failed；未执行该Profile、未形成完整Trial

修正：固定Tool + process展示类型 → 原decode_run_profile(固定ID, none, 原参数)
  → 既有严格输入合同 → 原Runtime批准指纹校验 → 原Trusted Action / Process
```

公开Schema只要求`profile`，`selectors`默认为空。原产品解码器接受该调用，旧评测器比较原始字典而拒绝。
修正不写回原参数，不改变请求/执行Fingerprint；错Profile、非空Selector、null、错误类型及额外程序/环境字段
均继续拒绝。固定镜像、原Pack、Grader、Prompt和Secret边界不变。
完整架构、流程/时序、字段、失败恢复和源码映射见[详细设计](../../changes/m09-r3-profile-approval-canonical-input.md)。

## 3. 验证结果与可复核材料

| 检查 | 实际结果 | 有限结论 |
|---|---|---|
| 原持久Session纯参数复现 | 原评测拒绝；原产品合同接受 | 0次额外模型请求，未执行工具 |
| 审批合法/非法参数 | 10个参数场景通过 | 省略及显式空Selector均接受；所有边界负例拒绝 |
| Evals与原输入合同完整关联回归 | 286通过、0失败、0跳过 | 不是完整产品回归 |
| 原固定容器与正式录制Task Pack执行/恢复 | 6通过、0失败、0跳过 | 显式同Engine宿主；两种参数形式，不是模型质量 |
| Mypy | 392个源码文件通过 | 静态类型，不是运行验收 |

[结构化事实](facts.json)保存计数、原件及变更源码摘要；原失败日志、预注册配置、Session与未完成状态
保留在私有验证目录，不复制路径、凭据、模型正文或工具正文。
[Verification](verification.json)、[Review Packet](review-packet.json)与[Manifest](manifest.json)限定结论及文件身份。

## 4. 已知CI风险与后续准入

故障基线的[CI 36615952548](https://github.com/carrie1988/Harnessix/actions/runs/36615952548)整体失败：
文档与容器Job成功；两个Python Job被既有12个Archive许可证门禁拒绝；macOS进程测试收到
asyncio `Unknown child process`、退出255；Windows两项备份测试在共享完整状态Fixture的5秒等待内超时。
这是三个独立失败类别，不应归为本审批修正的同一原因；原结果保留，未据此放宽断言或关闭发布门禁。

真实质量验证可以与上述风险定位并行，但不得发布正式版本。新Suite必须固定新Revision、新ID和配置，
仍为3仓10 Case/20 Trial，严格任务及必需测试至少12/20，每仓有严格成功，未授权修改/损坏/重复高风险效果为0。
旧未完成运行不得跨实现版本恢复或拼接成绩。默认Desktop启动、消费者Windows11、独立Beta及最终同候选门禁仍开放。
