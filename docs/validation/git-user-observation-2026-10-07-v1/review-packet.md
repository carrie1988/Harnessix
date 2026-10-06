---
doc_type: validation-evidence
status: current
version: 1
code_revision: db160c8caeafb0385a2362ccd0af1502a1f6e79e
owners: [core]
modules: [product_config, session, workspace, delivery]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_user_observation.py
  - tests/product_config/test_git_user_observation_controls.py
  - tests/product_config/test_git_parent_consumers.py
  - tests/session/test_authenticated_history.py
supersedes: []
---

# 用户 Git 完整观察审查包

## 1. 需求与核心流程

原成功Patch正常使U处于dirty状态；干净RepositoryBinding属于A而不是U，旧准入不得放宽。
原Session认证完整事件并校验Thread重放，实际history.thread驱动原成功Route与published事务
读取及一次Source2捕获。固定Git查询与原no-follow能力核验common/admin、完整物理Index、
全部逻辑基准及完整配置值；初始、中段、末段三次完整历史交叉复核，末轮Git查询
位于中段与最后history之间，最后同步复核Source2/Index。

新合同包含完整Baseline2、原Store/Key非秘密UUID、实际目录身份、Index存在性/身份/SHA/size、
完整配置输出SHA、七个产品层配方摘要和全字段指纹。没有私有正文、批准或执行权字段。

## 2. 缺陷根因与闭环

| 问题 | 根因与修复 | 验证边界 |
|---|---|---|
| Index检查点身份被转换 | Windows原转换器捕获OSError；包装原控制异常并边界解包 | 转换端口单元测试；不是Windows原生验收 |
| 原宿主身份冻结不完整 | 原Events可被替换、身份字段可漂移或Scope关闭 | 固定对象、Store/Key身份与全部存活状态 |
| POSIX保护声明超过原端口能力 | full_stdout只证明完整性，不证明redaction已运行 | 私有完整SHA、不输出正文；限定源码与文档声明 |
| history/Git交叉窗口 | 仅把history放前面漏事件追加，仅放后面漏history等待期间HEAD/config变化 | 初始、中段和末段三次完整history；中段后末轮Git，之后最后history |
| 根目录观察不能消费共享控制 | 旧根观察未提供checkpoint | 借原capture_snapshot_facts，逐项检查共同期限与取消 |
| 实现摘要被误认为全依赖身份证明 | 摘要仅覆盖七个产品层文件 | 明确范围；全包由四方字节和实际导入验证 |

首次失败及修复证据完整保留。独立复核为限定源码静态审查，不代替安装测试。
各测试集合可能重叠，不把累计通过数转换为覆盖率或商业验收结果。

## 3. 数据、安全、异常与恢复

原Source2/Baseline2及所有父引用保持完整；不把Source1改版本、不补造旧历史。
目录与Index使用原native能力，借用Scope仅验证原身份，不读Key/Secret材料。
原认证Reader控制对象不包装；只在原生边界使用原UpstreamCheckpointError并解包。
回收失败优先级沿原实现；共享60秒或更短原期限不续期，不进行自动重试。

这是多次顺序观察，不提供全局Git锁或跨库原子事务。新数据不能直接进入旧Core1并丢失字段；
后续正式Core代际、默认Planner/Executor、Review/ProductLink/Bridge以及Git成功闭包仍需实现。
失败可能保留原CAS中的无授权材料，不登记成功或擅自清除共享状态。

## 4. 安装、测试与验收限制

同候选wheel独立安装，测试期间所有Harnessix模块必须来自安装目录；完整源码哈希、
原门禁字节、原Schema兼容、治理及秘密扫描共同复核。模拟转换器与逻辑Windows分支不等于
原生Windows验收，ScriptedProvider不等于真实编码质量。旧公开Source1/Baseline1保持兼容。

实际Planner/Executor、Review Artifact、完整ProductLink、NativeBridge、A/T2/D、独立Commit、
Backup2、R1～R6、真实R3及有限Beta仍是后续商业必要项。本组件通过不表示商业1.0已完成。

[结构化结果](result.json)、[源码来源](source-inputs.json)、[完整性清单](SHA256SUMS)。

三次交叉读取仍存在最后H3读取期间或其后的HEAD/config独立变化窗口；它不是
跨库原子快照，不能仅凭观察值发布Git效果。后续实际执行必须重新核验当前
HEAD、配置、来源和物理Index，并按原失效语义拒绝陈旧计划。
