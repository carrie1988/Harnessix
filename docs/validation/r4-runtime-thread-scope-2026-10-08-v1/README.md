---
doc_type: validation-evidence
status: current
version: 1
code_revision: d5c572aff2fedae11d25fd1b0e8a4ca41062a8d2
owners: [core]
modules: [agent, product_config]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/product_config/test_git_prepared_runtime_thread.py
  - tests/agent/test_runtime_thread_lock_observer.py
supersedes: []
---

# Git 原 Runtime Thread 消费者接线交付报告

## 1. 结论与发布边界

正式内部组件已交付：实际 Runtime、原连接、原 Thread 持锁 Task 与 acquire 代际绑定，
并接入 prepared Ledger 和审批历史 Reader。组件正控已有终态，**整体回归尚未封板，R3／R4／商用发布 NO-GO**。
本目录是固定阶段快照，不把仍在执行的扩展回归列为通过；后续结果使用新的交付版本，不覆写本快照。

## 2. 需求、架构与源码阅读

[十四节总体与详细设计](../../changes/m09-r4-git-runtime-thread-scope.md)包含需求、选型、三幅图、
字段、调用链、异常、并发、伪代码、安全、部署与源码映射。
[绑定源码](../../../src/harnessix/product_config/git_prepared_runtime_thread.py)不获取锁或签发执行权限。
原 Task 已持原锁后打开工厂连接并进入 bind；窗口覆盖 BEGIN、认证与 COMMIT／ROLLBACK。
U 子 Task 仅消费原父 Task 签发的观察；原 SQL 准入仍拒绝跨 Task。
prepare 核对目标 Thread；read_all 保持完整全集，不隐藏其他 Thread。

## 3. 候选与安装

源码提交与生产 Wheel 的全部成员逐字节一致，安装为独立非 editable 环境。
采用 Python 3.12.7、原 uv.lock 固定依赖与 Git 2.53。版本号 1.0.0rc1 不足以识别候选，
具体 SHA 和成员数见 [facts.json](facts.json)。本交付不验证新 Sdist 或三平台业务上线。

## 4. 已完成的同候选结果

| 集合 | 终态 |
|---|---|
| 源码新 scope 实际 SDK | 8 PASS |
| 原锁及 observer | 52 PASS；其中新增 24 项已包含于下列安装核心集合 |
| 安装七完整核心目录 | 2815 PASS、1 原生 Windows SKIP、0 FAIL |
| 安装新 scope 与完整 Ledger 文件 | 20 PASS、0 FAIL |
| 安装决定返回绑定与 Reader 控制 | 31 PASS、0 FAIL |
| 安装修订后的完整 terminal 文件 | 7 PASS、0 FAIL |

不同集合及源／安装重复用例不累加为独立业务任务。原审批、取消、只读与提交边界均以真实断言为证据，
但临时工具注册不表示默认产品 Git 工具已经装配。

## 5. 保留失败与整改

- 首次 8 项 SDK 在系统 Git 2.24 上因缺少 object-format 支持失败；固定环境复跑，保留原日志/XML。
- 漏迁移的同步 helper 消费导致安装 ports 四项 TypeError；完整迁移，不删断言。
- 一个旧历史次数锚点位于 U 验证之前。改为原完整异步认证含 U 返回后注入 CAS／Review 故障，
  保留四格矩阵，增加 total_changes 不变断言；完整 7 项 terminal 通过。
- 十二项纯终端替身引用已移除函数。改为显式隔离现行来源 observer，仍只证明返回绑定，
  不是原 MAC 或持锁认证正控；相关 31 项完整单位集合通过。
- 两项原审批正控在原 Turn 期限内未完成，发生于同步终端 live-request 核验。
  不放宽 60 秒消费期限或 120 秒 Turn 窗口；独立复核仍在执行，P1 保持开放。

初始 history 14 FAIL／27 PASS、迁移后 ports 1 FAIL／124 PASS 均保留，不由修订后子集覆盖成全绿。
一次工作目录错误的复跑在旧测试进入后中断，保留 111 项部分结果，不作为终态验收。

## 6. 持久化、安全与恢复

无新 Schema、迁移、授权 Token、费用请求、执行权限或服务端口。原 MAC、Owner、材料、
U、事务 epoch、取消与期限保持。scope 为进程内临时事实，释放再 acquire 不恢复旧 observer。
异常退出只撤 registry，原失败不被新增末端检查遮盖。
不读取凭据、不修改真实费用账本、不处理客户工程，不追加或覆写历史封存目录。

## 7. 质量检查与可观测性

514 个生产源码文件 Mypy 通过；变更范围 Ruff／格式及文档门禁均独立执行。
三幅 Mermaid 实际编译为 SVG，架构 PNG 已视觉检查。Secret 扫描限定完整已跟踪仓库加本候选 Wheel，
不扩大原扫描上限、不删除历史 dist。准确原件由 verification.json 和 manifest.json 绑定。
错误采用固定分类，不记录模型正文、凭据或客户路径。

## 8. 剩余工作与 Go／No-Go

扩展 consumer 完整集合及原审批期限复核尚未完成；未经终态不得标为通过。
B7 还缺实际 SQLite FD 与全部 dispatch；B4 缺末端 Ref／配置保护；P1 缺正式响应性闭环。
决定 Writer、A／T2／NativeBridge／D、默认 Checkpoint／本地 Commit、Backup2 与三平台编码仍开放。
R3 还需有效费用授权登记与新冻结候选下的完整真实 Trial，本次模型请求新增零、Beta 接受新增零。

## 9. 复核索引

[Review Packet](REVIEW_PACKET.md)、[结构化事实](facts.json)、[验证记录](verification.json)、
[成员清单](manifest.json)共同构成固定交付。先核验 manifest，再核验私有原件及候选成员；
测试绿灯不得直接解释为商用可用。
