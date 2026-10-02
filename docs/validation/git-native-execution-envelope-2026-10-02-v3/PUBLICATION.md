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

# 有限执行观察 v3：实现与发布复核

## 文档摘要

本记录补齐[实现验证包](README.md)的独立复核和正式发布检查，不修改其已经冻结的55成员清单。
原`verification.json`中的文档与Secret状态是初次实现封存时的待检查状态；
本次实际完成结果以[发布复核](publication-verification.json)及
[发布输入清单](publication-manifest.json)为准。两份清单职责不同，不能覆盖历史记录或混用摘要。
完整架构、接口、字段和异常语义见[详细设计](../../changes/m09-r4-git-native-execution-envelope-v3.md)。

初次正式文档检查因详细设计缺少持久化语义章节失败。设计v2补齐实际数据流程及双文件半发布边界，
原设计字节保存在[设计v1原件](originals/design-v1.txt)。初始55成员清单的该路径按此原件核验，
其余54件仍按当前原路径核验；新发布清单绑定当前设计，不能称旧清单对当前设计零漂移。

## 精确内容

实现清单包含三处观察器修改、必要测试、详细设计及有限证据；六处原观察器输入保持原字节。
发布输入另包含路线图和本复核记录，以逐文件字节数及SHA256绑定。
九处观察器输入均须匹配独立审查的固定源码，后继漂移不得沿用本结论。
本轮不修改生产Runtime、固定合同、十六件发行输入、两项SDK选择器、十三hook或执行期限。

## 实际验证

- 正式关联回归：227项通过，原153项与新增74项分别记录，不计入历史预研43项。
- 固定九源的独立实现复核：81项通过，保留验证宿主的前两轮失败；限定增量未发现P0/P1/P2。
- 原23组固定投影语料：旧输出字段与异常类型零差异。新增对象不参与原完整认证门。
- 原认证表达式、分支语义及运行控制的限定AST核对通过；额外计数与默认观察对象单独识别。
- 当前详细设计相对独立审查时的变化仅为状态、字段解释、实际结果和部署说明补充；
  源码九件仍匹配，不能将这项文档增量检查称为新的完整源码审查。
- 全库文档检查、精确发布输入Secret扫描和提交字节核对按实际结果单列；检查失败不允许发布。

原本机真实pytest发布链恢复两条fixture帧，但pytest仍退出1；协议中的win32与十三hook声明
不是原生执行证明。新流程图已实际渲染并查看，四幅继承图只复用原摘要，不声称重新渲染。

## 安全与未验证

新增案例始终为未认证诊断，计数只表示语法且64为饱和下界；不可用日志为null。
原cases、branch_gate、失败状态及SDK验收语义保持。
本记录不补写旧Windows结果，不证明Git128根因、SDK成功、完整Git产品或商用发布。
新原生运行须在精确候选发布后显式执行一次原workflow；R1～R6退出条件仍开放。
