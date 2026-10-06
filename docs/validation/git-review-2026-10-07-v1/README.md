---
doc_type: validation-evidence
status: current
version: 1
code_revision: 3e108d7eddbdff01b952ad8a9c9e403ed58e57be
owners: [core]
modules: [product_config, delivery, artifacts, agent, domain, protocol, product_ui]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_delivery_review.py
  - tests/product_config/test_git_delivery_review_controls.py
  - tests/product_config/test_git_delivery_review_protection.py
  - tests/product_config/test_git_delivery_source_verification.py
supersedes: []
---

# 正式 Git Review、完整原文保护与原认证 Artifact 验收

## 1. 背景、目标及源码入口

[详细设计](../../changes/m09-r4-git-review.md)定义独立Git审阅合同及实际生产者。
[Core2](../../../src/harnessix/product_config/git_delivery_observed_contracts.py)保存完整
用户观察；[生产者](../../../src/harnessix/product_config/git_delivery_review.py)从原Route恢复
原CAS，复核原认证Session、成功Patch来源及同次完整Git Diff，再经原Artifact事务发布。
不伪造Workspace事务，不缩减消息、作者、原参数、文件或Diff，不另建审批与存储系统。

架构及流程为：原认证历史H1 → 原pending Route2/Core2 → 原成功Patch及Source2只读复核
→ 原全对象与父历史材料 → 完整原文Scope保护 → 正式Git JSONL及真实页数预检
→ H2复核 → 原稳定ID Artifact发布 → H3复核 → 原Gateway唯一审批回指 → 原SDK分页读取。
时序、数据流、字段、接口及异常合同见详细设计中的三张图和对应文字，不将目标架构冒充现状。

## 2. 实现范围与正式合同

新增Summary/Entry/Document保留完整原ToolCall、Core内容地址、两树OID、所有DiffEntry和
完整Diff SHA。规范JSONL沿原唯一编码；旧Workspace JSONL与既有279份Schema字节保持。
严格解析拒绝重复键、缺省补全、额外字段、子类、乱序、非规范字节和截断正文。

[唯一页算法](../../../src/harnessix/domain/artifact_pagination.py)及原预算只移动为共享定义，
保持1MiB、24KiB、10000记录、200条、50页、UI5秒、单次观察60秒和原公开保护10秒。
不新增产品服务、数据库迁移、网络请求或默认Git写工具。

[完整原文保护](../../../src/harnessix/domain/review_text.py)在已知Git/Workspace Review的
发布及每次读取时，核对连续记录、全文字节数与SHA，再经原Scope扫描完整重组文本。
这堵住切块边界规避及重新打开后保护范围改变的泄露路径。旧通用Review格式仍沿原JSONL保护，
不是任意字段猜测拼接；持久MAC、Root/Owner/Scope/Lease和原审批授权均未替换。

## 3. 测试方法与实际结果

最终wheel安装到独立target目录，现有受管依赖环境运行，不代表全新依赖安装。
新专项39项通过：20项声明级合同/编码/公开保护，19项实际认证SDK编排；两类不得混作
Session授权证据。关联回归1813项通过、零跳过。原始JUnit、实际导入路径和日志保存在
受限证据包；两组运行的所有Harnessix模块均来自已安装wheel，候选src不在运行搜索路径中。

实际认证组使用离线网络Provider及真实Session、签名历史、Router、Patch、Root、Git、
Workspace、CAS、Artifact和SDK。临时Git工具注册仅属于测试夹具，NeverExecuteGit禁止
任何Git执行业务；正向证据包含SHA1/SHA256、连续Patch、原审批回指、多页完整读取以及
原SDK拒绝后继续Turn。没有批准后Git写入、付费模型编码质量或原生Windows商业验收。

原候选source治理1413项通过；治理加载原脚本与源码，不冒称已安装隔离。
文档门禁561份文档、12869个链接无发现；仓库与wheel Secret扫描5027个输入完整覆盖、
固定规则零命中。最终状态以[结构化结果](result.json)为准。
Ruff、1753文件格式、493源码Mypy及原可读性检查通过；493个Python模块在主源码、
候选、wheel和安装目录逐字节相等。生成器检查通过，既有279份Schema及五项原门禁字节不变。
三张图均实际渲染并检查完整像素。上述集合不累加为覆盖率或商用完成率。

## 4. 失败、取消、恢复与持久化

原60秒期限及取消贯穿同步复核和异步发布。托管任务结算后的父任务末端再次检查取消、
Owner、宿主引用和Ref TTL，消除结算期间变化窗口；过期发布收据必须拒绝，不续期或换ID。
独立静态审查发现的这两项问题均有真实认证回归用例，不以静态意见代替实际执行结果。

原发布after_insert故障验证事务回滚；after_commit应答丢失验证稳定ID查询优先恢复、
相同Ref与原TTL保持。原Source最终正文、模式、存在性变化必须失败；净零变更路径虽然
不生成Mutation，仍必须验证原最终版本。只读verify不重捕获、不追加CAS或修改持久状态。

Artifact提交与审批回指是既有不同事务；后续历史、取消或来源失败可能留下无回指Artifact，
原Scoped Reader不可据此读取。Session、Router、CAS与Git状态不是跨库原子快照。
未来Executor执行前必须新鲜复核HEAD/config/index、Source、Owner及原正式批准。

## 5. 原始失败与修复复验

保留三次初始包依赖违规及原复杂度门禁拒绝，修复采用共享domain定义、protocol预算门面
和同模块职责拆分，没有增加允许包边或放宽复杂度。保留类型检查和初始代码修正记录。

系统旧Git不支持SHA256造成材料回归2项失败，改用既有受管Git2.53后1126项通过。
SDK多页夹具曾超过原模型输出额度、后续回答脚本步骤错误及读侧保护提前拒绝导致断言不符；
修复测试负载与步骤、确认更早拒绝边界，没有提高产品预算或关闭公开保护。
初次候选缺少待同步报告造成治理失败；文档检查发现遗漏模块同步及一处源码链接错误，
补齐五份现行模块设计并修正链接后，原策略及治理完整复验通过。
中间通过记录不代替最终39项与1813项已安装候选复验，全部失败记录均保留。

## 6. 部署、安全与可观测性

沿原Python包、本地SQLite/CAS和产品宿主部署，无新增端口、服务或后台Worker。
公开日志仅保留原错误码、受控身份与摘要；Diff只进入原受保护Artifact。
没有读取用户凭据、Keychain或预算配置，没有模型费用、Docker操作或定时任务变更。
测试仅使用合成保护材料，不读取或运行无关未跟踪安全测试及验证目录。

[源码输入](source-inputs.json)逐文件绑定候选，验证包自排除；
[审查包](review-packet.md)列出证据等级、修复及未关闭事项；
[完整性清单](SHA256SUMS)覆盖四份公开材料。私有包保留wheel、安装、完整JUnit、失败、
图像、校验数据及manifest，公开材料不包含个人路径、原文或合成密钥。

## 7. 风险与商用发布边界

本次完成正式Git Review组件、实际认证Artifact消费链与完整原文读写保护。
默认Git Planner/Executor、ProductLink/NativeBridge、A/T2/D真实写闭包、独立Commit与
业务Backup2仍未完成；默认接线、同候选R1–R6、真实R3、原生Windows及独立有限Beta
必须分别验收。本次测试通过不关闭商业1.0门禁。

CLI/UI目前显示原JSONL，没有Git专用Diff渲染，不强制用户读完再批准。
5秒消费预算独立于60秒生成期限，不证明全部设备显示SLO。材料完整、内容地址、
Artifact MAC和审批回指都不能单独授予Git执行权；商业完成标志保持false。
