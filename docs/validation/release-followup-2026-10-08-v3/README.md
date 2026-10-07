---
doc_type: validation-evidence
status: reviewing
version: 1
code_revision: 9c5e5d227b718b22d1c9f6f854722f11e453e7f8
owners: [core]
modules: [product_config, session, execution, delivery, documentation]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_decision_link_contracts.py
  - tests/product_config/test_git_prepared_link_contracts.py
  - tests/product_config/test_git_authority_pure_paths.py
  - tests/governance/test_documentation_policy.py
supersedes: []
---

# 决定数据合同、纯路径增量与 Beta 前测复验

## 1. 交付范围与判定

本交付包含四个生产源码文件、两个测试文件及设计同步：三种 Git 决定纯声明与严格规范字节；
原宿主四个固定期望 Path 的局部纯值复用。没有默认写工具、认证 Proof、数据库 Writer、DDL、配置或模型行为变更。
总体及详细设计分别见[正式决定方案](../../changes/m09-r4-git-approved-link.md)、
[数据详设](../../changes/m09-r4-git-decision-data-contract.md)和[纯值详设](../../changes/m09-r4-git-authority-pure-paths.md)。

| 证据集合 | 实际结果 | 不覆盖的范围 |
|---|---|---|
| 主仓新决定与旧 prepared 合同 | 142+212=354 PASS | 真实 Session/MAC/Owner 来源 Proof、Writer、恢复 |
| 主仓相邻观察Core/产品codec/目录 | 487 PASS | 实际商用场景、安装与三平台 |
| 主仓纯路径39+文档策略23 | 62 PASS | 全部实际SDK控制回归、响应性SLA |
| 原/候选纯Path对照 | 40短测及各自1 SDK PASS | 单组计数观测，不是全矩阵或主仓新重跑 |
| 独立数据审阅 | 最新142及独立合法大小Oracle3通过；初始独立完整探针107通过 | 不叠加跨版本/重跑为一套SDK或354独立复验 |
| Beta 上传Mock反事实 | 一次完整类10/10 PASS | 非Agent修复，原全业务FAIL不变 |
| Beta 权限运行时及反事实 | 两轮各3 FAIL，原403越过后仍失败 | 未到达断言、资源闭包、业务/Beta通过 |

主仓三个已完成且节点不重叠的集合合计903 PASS/0FAIL/0ERROR/0SKIP。主仓实际控制/Owner矩阵仍运行，
不计其部分输出为通过，不以单组候选替代此未完成矩阵。没有CI绿色或1.0发布结论。

## 2. 架构、源码与数据边界

新[合同](../../../src/harnessix/product_config/git_decision_link_contracts.py)以三个确切变体保存完整 Plan2、
原未决定请求、两域原决定和事件索引。approved→approved，denied/cancelled→failed；sequence严格整数1。
前驱复用原 prepared 校验，旧准备字节与未决定语义不放宽。Session请求指纹与Execution计划指纹独立，
人工决定actor/reason/outcome/同一时间必须一致；取消不伪造Session人工decision。

[新wire](../../../src/harnessix/product_config/git_decision_link_wire.py)按重复键拒绝、严格JSON解析、
原深快照与[原完整编码器](../../../src/harnessix/product_config/git_delivery_plan_wire.py)字节相等顺序执行；
保留所有字段，原512KiB上限不提高。合法三变体精确524288字节往返，524289拒绝。
首/中/末控制异常保持原对象。aware决定时间按同一瞬间比较，并不保证两域时区字符串相同；
真实认证事件正文仍是后继Proof责任。有效construct可重建但不是来源证明。

[`git_user_authority`](../../../src/harnessix/product_config/git_user_authority.py)只在exact-native Path同closure复用四右值，
首次在原条件位置构造；子类每次执行原操作。全部live左字段和首末顺序不变，不缓存URI/resolve/Owner/连接/授权。
新[纯路径回归](../../../tests/product_config/test_git_authority_pure_paths.py)不依赖私有基线文件；
[142数据回归](../../../tests/product_config/test_git_decision_link_contracts.py)核对完整字段与旧兼容。
两份详设的7张流程/架构/时序图单独渲染；不将仓库全部Mermaid语法检查声称为全部新渲染。

## 3. 真实 full-check 性能与限界

从原`faec7a017e801dd5376fdb1ca0fb6490c67ef4ba`两份5180件tracked archive建立原/候选，
仅一个源码差异；两次actual SDK、原60秒consumer/120秒Turn、无profile、无layered桥。
固定右值构造990412→8；freshOwner123313、Owner246626、verify246627、外callback52361、
auditPhysical369940、四库身份493260、GitDB pin123313、terminal1全相同。
各自原数据库/Git/CAS/MAC摘要不变，SQLite total_changes0；独立UUID/Store不要求跨实验MAC同字节。

原35.719秒、候选31.602秒，仅一组有计数包装，不能承诺稳态提升；候选最大心跳间隔14.300秒，
P1仍开放。原60秒cProfile失败、旧研究失败保留；本次不以构造次数减少取代取消/Owner/漂移安全验证。

## 4. Beta 前测：反事实支持与仍失败

完整原350项仍302PASS/9FAIL/39ERROR。上传测试只将一个旧Mock stub对齐实际锁定读取接口，
一次10项28.276秒exit0、相同nodeid；原9个NPE消失，生产代码/断言/权限/OS隔离/旧原件不改。
归因获支持，不是生产上传没有缺陷或Harnessix Agent修复。

权限原样H2证明teacher2/student2新增测试用户无动态赋权与组织归属。反事实经正式
OrganizationService.addMembers→RoleManagementService.assign补齐本班EXACT，不新增GLOBAL，
原403均越过，但Task列表仍因0/1可见性期望冲突失败、Course仍缺模板400、Workflow越权仍403但错误码不同。
两轮分别60.744/61.945秒、各3FAIL、零重试；生产代码与断言不改，未到达后续权限断言不计通过。
原既有种子管理员GLOBAL与本次新增账户EXACT分别记录，不能混称增加全局授权。

423输入、两个原测试文件四个注入Span及三个支持文件逐SHA绑定。原业务/参考目录不运行或写入，
模型Workspace与业务副本分离，无付费模型请求。资源闭包和Console/Network披露面、整改后全回归及浏览器接受仍开放。
不能把19项全部称为已修复，也不能将旧FAIL减少为0。

## 5. 独立审阅与失败保全

决定审阅限定数据声明/Wire未发现可复现阻断漏洞，三变体合法大小独立Oracle3项通过。
初始68PASS/2FAIL保留：construct未知入参在Pydantic已丢弃、填充reason先触及原16KiB限制，
两者不是已证实绕过。诊断相对路径/目录复用辅助错误也保留；后继合法Oracle未调高合同。

主仓RED missing-module、mypy adapter缺标注、选择不存在测试文件的命令、旧系统Git不支持对象格式以及
移植测试两次检查错误预期均保留。后继修正只涉及本次源码/标注/测试环境及预期；
实际Git使用2.53原生命令，未改生产限制或真实断言。最初详设缺语义标题的文档门禁失败保留并补齐。
失败不被重新统计成PASS；903仅来自三份明确终结JUnit。

## 6. 预算、部署与发布门禁

新60元周期当前26个completed、估算0.706096元、预留0、剩余估算59.293904元，实际账单未知；
本交付模型请求0，旧两笔未决原件不计新周期、不追认零费用。新未知仍停止请求。

Docker引擎socket缺失，官方status退出1；后继图形界面读取超时，未取得新界面状态，
不将该超时当作新的锁定判定、引擎恢复或原8容器恢复。没有新容器删除/启停操作。
默认Workspace挂载/Profile、R3原严格0/20及必需检查1/20、真实Beta接受0、三平台与R1～R6保持开放。
Git正式决定来源Proof/Writer/恢复、B4/B7及响应性仍需完成，纯数据或单组性能不关闭这些门禁。

## 7. 交付、部署与回退

[结构事实](facts.json)、[Review Packet](REVIEW_PACKET.md)和manifest绑定公开件；私有原件单独绑定完成集合。
新数据代码无持久效果、无依赖或端口；纯路径可精确回撤一文件，不涉及数据迁移。
不得将声明直接交给原MAC发布器。正式认证、写入及后继效果启用必须另有完整原资源/终端/故障恢复验收。
