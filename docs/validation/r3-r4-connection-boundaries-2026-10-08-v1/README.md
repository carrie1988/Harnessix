---
doc_type: validation-evidence
status: reviewing
version: 1
code_revision: pending
owners: [core]
modules: [product_config, delivery, models, agent, session, evals]
related_adrs:
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_prepared_link_connection.py
  - tests/product_config/test_git_prepared_link_ledger.py
  - tests/product_config/test_git_prepared_approval_history.py
  - tests/delivery/test_git_prefix_task_owner.py
  - tests/delivery/test_git_prefix_sql_lifecycle.py
  - tests/models/test_unknown_tool_boundaries.py
supersedes: []
---

# R3未知工具边界与R4连接任务准入交付报告

## 1. 结论与适用范围

本切片修复R4原连接及异步SQL窗口的同线程跨Task借用，并以只读来源观察保留合法U验证子Task。
SQL消费检查点前后均复核同一原控制窗口、原产品连接登记及Task；不增加上游回调，不覆盖原异常。
R3新增当前行为的边界验证及完整恢复方案设计，**生产Adapter恢复行为未改变**。

最终原语及边界回归**389通过、失败0、错误0、跳过0**；模型、Kernel及原预算适用回归
**747通过、失败0、错误0、跳过0**。两集合有交集，不相加为独立用例数。
新增场景合计41项：R3十五项、连接十五项、SQL任务及生命周期十一项。

同一最终Wheel的两项实际认证SDK消费者**2/2通过**：prepare、COMMIT后非空只读重开，
以及四种原上游异常实例原样传播。JUnit运行157.894秒，错误/失败/跳过均零。
这是针对本差异的真实组件正控，不是所有消费者矩阵或完整Git写工具通过。

实际SDK消费者的终态、候选绑定与未完成工作见[结构化事实](facts.json)及[评审包](REVIEW_PACKET.md)。
原语通过不代替实际消费者、完整Git业务写入、三平台原生编码或R3真实质量门槛。

## 2. 需求背景与根因

R4连接来源原登记在线程私有表，同一事件循环的兄弟Task、受管子Task和loop callback共享线程。
准确Task判断必须区分执行准入与只读来源观察：合法U验证子Task消费来源检查点，但不能获取连接或SQL发布权限。
直接全链加入严格Task判断会误拒绝原合法子Task；完全不区分Task则会借出执行准入。

R3原`tool_name_unknown`检查早于类型及严格JSON判断。因此该诊断不证明响应结构合法。
广告Alias与工具原名不同，未知Wire名称直接透传有可能命中已登记原名并实际执行。
拟议恢复需新增不可执行类型化拒绝事实及配对历史，不能删除目录校验或模糊匹配工具名。

## 3. 实现、契约与源码

| 模块 | 本切片变化 | 保持边界 |
|---|---|---|
| [连接工厂](../../../src/harnessix/product_config/git_prepared_link_connection.py) | 私有登记准确Task，原Task签发固定无参数来源观察闭包 | 原普通文件、路径pin、PRAGMA来源、活跃登记与存活；非FD证明 |
| [Ledger](../../../src/harnessix/product_config/git_prepared_link_ledger.py) | 合法U子Task只消费来源观察 | 原CancelToken.run、preserve_failure、Owner、MAC、原期限、取消与事务代际 |
| [SQL窗口](../../../src/harnessix/product_config/git_prefix_sql.py) | 建立及消费绑定原产品来源；消费回调前后复核；异步窗口原Task独占 | 未登记同步通用窗口的原线程合同；首失败优先；不授予锁或业务授权 |
| [R3边界测试](../../../tests/models/test_unknown_tool_boundaries.py) | 畸形及未知名称、同组写调用不部分释放、原Loop预算/取消与重放 | SDK仍直接失败；无新增审批、工具执行或真实模型请求 |

总体架构、时序、数据流、字段、伪代码、兼容与回退分别见
[R4完整详设](../../changes/m09-r4-git-connection-ownership.md)与
[R3待决详设](../../changes/m09-r3-unknown-tool-recovery.md)。两详设八个图均成功渲染，另对两个总体流程图完成视觉检查。

## 4. 失败证据与验证环境

修复前十二项连接场景8失败、4通过。单文件严格Task中间原型误拒绝合法U子Task；原失败保留。
后续发现产品连接可被其他Task建立通用SQL窗口，负控3失败、同步通用正控1通过；已增加原登记约束。
检查点正常返回却退出真实context的负控修复前1失败；已补充消费后复核。

运行环境分别为Python3.13.8及隔离Python3.12.8。实际Git用例使用已验证的Git2.53.0。
Apple Git2.24.3不支持object-format参数，其失败独立保存，不通过跳过或修改原用例掩盖。
未安装产品的隔离环境不能让受管Owner子进程导入Harnessix；该失败原样保留。
最终候选使用离线构建并正式安装的Wheel，`python -I -B`确认进程Owner模块可导入。

版本仍为1.0.0rc1。Wheel552个生产成员；三个修改模块逐字节等同候选源码，摘要见事实文件。
这不是正式1.0发布或发行矩阵通过声明。

## 5. 测试、持久化与恢复边界

原语组覆盖连接置换/撤销、跨await原Task、独立连接、来源观察子Task、SQL消费、事务代际、首失败及认证前缀。
R3组覆盖未知诊断先后、整组零释放、原步骤预算、取消、不重试、不创建审批、重开及事件Replay。
修改没有新增数据库字段、协议或迁移，也没有重签旧认证历史。

最终Wheel已分别验证实际prepare/COMMIT后非空只读重开与原异常实例传播；中间候选长回归不能冒充最终Wheel结果。
并行执行的测试具有CPU竞争，不据其总耗时推导稳态产品响应SLA或P1完成。

## 6. 可观测、安全与证据保存

公开仅包含固定错误类别、计数、源码及产物摘要、命令目标和证据适用边界。
原JUnit、失败正文、宿主信息、临时目录及原日志只保存于私有证据目录，不公开Task名称、对象地址、SQL、模型原名称/参数或凭据。
生产回调、认证、期限、权限及质量阈值均未削减，外来未跟踪目录及原业务项目不在修改或扫描范围。

## 7. 遗留工作与风险

- R3：拒绝事件/Item/Reducer/两个Provider历史配对及旧Reader兼容仍须冻结并实现，然后完成原固定Pack的真实20 Trial复验。
- R4：当前Task准入不证明实际FD、原协作锁持有或完整B7；B4、P1、approved Writer、默认完整Git交付及三平台原生编码继续开放。
- 原R3严格成功0/20、必需测试1/20与真实Beta接受零不因离线通过而改变。
- 本证据不关闭R1～R6，不启动稳定版本发布，不增加真实付费模型请求。

## 8. 交付与复核入口

[facts.json](facts.json)记录终态、集合范围、Wheel及源码绑定；[verification.json](verification.json)
记录验证命令与摘要；[manifest.json](manifest.json)保护公开交付成员；[REVIEW_PACKET.md](REVIEW_PACKET.md)
区分接受事实与拒绝扩大解释。私有原件及中间失败使用独立摘要，不覆盖已封存历史。
