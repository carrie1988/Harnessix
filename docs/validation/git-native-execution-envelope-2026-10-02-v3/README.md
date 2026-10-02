---
doc_type: validation-evidence
status: current
version: 1
code_revision: ed8005f00755b9d37a658f9d0c7163f6250586f1
owners: [core]
modules: [delivery, product_config, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/governance/test_windows_git_native_branch_observation.py
  - tests/governance/test_windows_git_native_branch_preflight_v2.py
  - tests/governance/test_windows_git_native_selected_layout.py
  - tests/governance/test_windows_git_native_execution_v3.py
supersedes: []
---

# Windows Git 有限执行观察封装 v3 验证包

## 文档摘要

本包绑定ed8005f发布基线上的有限观察器候选，不是Windows实际运行或SDK验收。schema显式v3，原cases/branch_gate/status/退出、SDKfalse及Root UNKNOWN保持；新增九字段未认证案例观察和三个0..64语法饱和下界计数，不授予权限。

## 精确内容

- [完整详设](../../changes/m09-r4-git-native-execution-envelope-v3.md)：背景、架构、流程、接口、字段、异常、预算与部署回退。
- [SOURCE.json](SOURCE.json)：三源及必要schema测试变化、固定16输入、原函数与f2b候选AST关系。
- [facts.json](facts.json)、[verification.json](verification.json)：真实运行原件投影、当前正式227/真实pytest及历史peer分列。
- [review-packet.json](review-packet.json)、[COMMANDS.json](COMMANDS.json)：有限复验及合入/原生观察技术边界。
- [frame-observation.mmd](frame-observation.mmd)、[frame-observation.png](frame-observation.png)：当前新增图实际render/view，原四图只摘要复用。
- [原observe.py](originals/observe.py)、[原projection.py](originals/projection.py)、[原run_cases.py](originals/run_cases.py)：原v2精确字节，非当前入口。
- [manifest.json](manifest.json)：精确写集、源与继承证据成员，排除自引用。

## 实际验证

正式227 PASS＝原153＋新增74，0失败/错误/跳过。新增覆盖最终帧边界、混合完整标记/partial-prefix sticky invalid、旧gate差分、exact类型、A/B、不可用/null、一次读取、语法计数与饱和下界。必要旧测试仅将一处schema v2预期改v3，其他原断言不改。

当前最终真实pytest8.4.2/Python3.12.7再验：原teardown/render/_publish复用，2完整fixture帧前均有pending F，sink恢复2行且invalid=false，pytest仍失败退出1。fixture协议声明win32/13并非真实安装hook或Windows见证。两行raw_validation=UNAVAILABLE仍保持原NO-GO，但v3在独立未认证观察中可见。

项目外peer43 PASS、原真实pytest RED/GREEN及混合未完协议RED作为历史引用保留，不计入当前227。当前envelope缺字段1 FAIL RED另保留。Ruff/format12文件、两份文档与精确写集Secret扫描结果见verification。

独立固定23向量复验的旧输出及异常类型零差异，九源与当前候选逐字节相等；该结果单列为已有外部复验，不计入227，也不作为Windows原生证据。

## 安全与未验证

只有完整日志成功受限读取才可能测得0；缺失/过大/投影失败不冒充0。计数64表示至少64，匹配只指整行语法，不证明进程、硬件命中或正确context。新观察不影响原门，extra/类型错误不透传原正文，PID/path/error文本与原日志不公开。

原[Run36983172137失败](../git-native-execution-incomplete-2026-10-02-v1/README.md)不回写，不从历史空数组猜真实marker或帧数。旧v2包原件保持。没有stage/push/dispatch、Windows/SDK/CI、网络/模型、凭据、账本或Docker；不关闭Git128、W1、R3或商用验收。
