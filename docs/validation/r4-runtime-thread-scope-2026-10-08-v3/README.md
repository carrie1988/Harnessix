---
doc_type: validation-evidence
status: current
version: 3
code_revision: d5c572aff2fedae11d25fd1b0e8a4ca41062a8d2
owners: [core]
modules: [agent, product_config]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/product_config/test_git_prepared_link_controls.py
  - tests/product_config/test_git_prepared_link_terminal_callbacks.py
  - tests/product_config/test_git_link_user_observation_consumption.py
supersedes:
  - docs/validation/r4-runtime-thread-scope-2026-10-08-v2/README.md
---

# Git Thread 消费者完整三文件及响应性诊断报告

## 1. 结论与发布边界

同一冻结安装候选的消费者三完整文件 **49 PASS、0 FAIL、0 SKIP**，JUnit 总耗时 5200.738 秒。
这补充内部取消、控制回调、原 U 消费及故障拒绝的功能证据；长耗时不当作可接受生产性能。
**完整产品回归、B4、完整 B7、P1、R3、R4 与商用发布保持 OPEN／NO-GO。**

[v1](../r4-runtime-thread-scope-2026-10-08-v1/README.md)与
[v2](../r4-runtime-thread-scope-2026-10-08-v2/README.md)保持原快照，
原并行审批期限失败、旧测试接入失败和部分中断记录仍保留。
不把原 41 项 history 或 125 项 ports 的不同子集结果拼成最终全绿。

## 2. 候选与完整用例范围

生产代码提交 d5c572aff2fedae11d25fd1b0e8a4ca41062a8d2；
Wheel、源码、Git 对象及非 editable 安装的 556 个生产成员一致。
Python 3.12.7、原 uv.lock、Git 2.53，具体 Wheel、JUnit 与原件 SHA 由 [facts.json](facts.json)绑定。
测试投影只含已跟踪输入，不纳入主仓非任务文件；运行期间不修改原生产或测试候选。

| 完整文件 | 通过项数 | 验证范围 |
|---|---:|---|
| [prepared controls](../../../tests/product_config/test_git_prepared_link_controls.py) | 10 | 宿主／真实材料／MAC／策略／连接变化与原取消、期限 |
| [terminal callbacks](../../../tests/product_config/test_git_prepared_link_terminal_callbacks.py) | 3 | 同步末端不调用外部回调、原异常身份及材料消失 |
| [U consumption](../../../tests/product_config/test_git_link_user_observation_consumption.py) | 36 | 原 U、漂移、非目标坏 U、首失败、回滚、重试及到期 |

不累加 v1 原锁／核心或 v2 子集为新的独立业务任务。
测试中临时注册工具仅用于原 Runtime 链路，不表示默认产品 Git 写工具已装配。

## 3. 架构、流程与源码定位

[总体及十四节详细设计](../../changes/m09-r4-git-runtime-thread-scope.md)包含三幅实际编译图、
类／字段、SQL 窗口、Task／锁代际、失败与恢复、并发及部署。
实际链为原 Runtime 持锁 → 原连接工厂 → bind → BEGIN → 原业务认证 → COMMIT／ROLLBACK → 撤销 scope。
受管 U 子 Task 只能观察，不能签发新窗口、使用原 SQL 权限或释放锁。
源码入口为 [scope](../../../src/harnessix/product_config/git_prepared_runtime_thread.py)、
[Ledger](../../../src/harnessix/product_config/git_prepared_link_ledger.py)和
[Runtime 锁](../../../src/harnessix/agent/runtime_thread_lock.py)。
本追加记录没有新接口、配置、业务授权、Schema 或模型参数。

## 4. P1 调用倍增诊断

静态调用链显示：每个 Ledger 普通检查点包含 internal → 外部 callback → internal；
两轮内部验证包含 Audit 鲜读连接与四库变化检查。JSON iterencode 每个片段及分块、
历史每个事件前后和深层模型字段都会向上放大该成本。
新增 prepare 对 N 条既有关联执行 2N＋2 次完整认证，连续新增的次数为 Θ(K²)；
没有发现 U 回到完整认证形成无限递归的调用边。
上述是当前源码推导，不是实际运行热点百分比。

对同一安装候选的一个实际 SDK scope 场景进行标准库 cProfile 独立诊断。
它在原 49 项进程结束后启动，诊断墙钟上限 240 秒；产品原期限及回调不替换、不扩大。
运行终态、精确函数计数与诊断是否被中断由 facts 绑定。
仪器开销、诊断截断或场景异常必须保留，诊断结果不计入功能通过或性能 SLA。
纯常数优化尚无性能承诺；不得删除全集认证、改变首失败／回调轨迹、缓存可变原材料或增加 TTL。

本次诊断约 98.829 秒结束，未达到 240 秒诊断上限；原 60 秒 Git 操作预算到期，
JUnit **1 FAIL**，固定异常 `git_process_timeout`。外层 cProfile CLI 返回 0，
测试状态以 JUnit 为准；不能将 CLI 成功当作场景通过。
超时前场景记录 230520976 次函数调用、205176 次 Audit `_read_fresh_owner`，
SQLite `execute` 1744865 次、`stat` 4243002 次，Ledger control／internal 分别 92299／184597 次。
计数含场景装配及已执行阶段，不是完整成功工作流的计数；函数累计时间嵌套不能相加。
纯 annotation 匹配累计约 0.022 秒，不将已有此类微优化建议列为响应性整改主路径。
后继分层方案必须先版本化内部控制合同并补完整适用负控，不能把
[早期默认关闭研究](../git-p1-layered-research-2026-10-08-v1/README.md)直接合入或称为安全等价。

## 5. 剩余工作与真实业务边界

当前消费者覆盖仅限上述内部组件；原实际 SQLite FD、全部 dispatch、B4 Ref／配置末端一致性、
P1 响应性、决定 Writer、默认 Checkpoint／本地 Commit、Backup2、完整恢复与三平台编码仍缺验收。
R3 历史严格成功 0/20、必需测试 1/20、真实 Beta 接受 0 不变。
需要有效费用授权登记后的完整真实 Trial；本项无新增模型请求、费用变更或客户工程处理。
客户项目原件与副本、非任务主仓文件和历史封存不参与诊断，也不修改。

## 6. 复核与质量记录

[Review Packet](REVIEW_PACKET.md)、[结构化事实](facts.json)、[核验记录](verification.json)
与 [成员清单](manifest.json)绑定追加终态。
核验相同候选及三个完整文件后再复算 JUnit，诊断与验收分开读取。
文档与完整已跟踪仓库加候选 Wheel 的 Secret 门禁在文件齐备后执行；原规则与上限不变。
原三幅图、Mypy、Ruff／格式与生产成员证据沿用固定同候选原件，不另造三平台或真实编码成绩。
