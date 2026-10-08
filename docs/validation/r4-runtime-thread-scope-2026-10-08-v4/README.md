---
doc_type: validation-evidence
status: current
version: 4
code_revision: d5c572aff2fedae11d25fd1b0e8a4ca41062a8d2
owners: [core]
modules: [agent, product_config, delivery]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/delivery/test_git_prefix_sql_lifecycle.py
  - tests/delivery/test_git_prefix_task_owner.py
  - tests/product_config/test_git_fresh_owner_reader.py
  - tests/product_config/test_git_prepared_link_connection.py
  - tests/product_config/test_git_prepared_link_terminal.py
  - tests/product_config/test_git_review_fresh_owner.py
  - tests/product_config/test_git_review_runtime_fence.py
  - tests/product_config/test_git_decided_source_reader.py
  - tests/product_config/test_git_decided_source_terminal.py
  - tests/product_config/test_git_decision_source_sdk.py
  - tests/product_config/test_git_prepared_approval_history.py
supersedes:
  - docs/validation/r4-runtime-thread-scope-2026-10-08-v3/README.md
---

# Git 原锁窗口：剩余十一完整文件回归报告

## 1. 结论与发布边界

同一冻结安装候选的两个剩余完整文件集合分别完成：

- ports 七文件：**125 PASS、0 FAIL、0 SKIP**，JUnit 642.543 秒；
- history 四文件：**41 PASS、0 FAIL、0 SKIP**，JUnit 932.180 秒。

它们闭合当前已安排的两组完整文件回归，不是整个产品 SDK、完整 B7 或默认 Git 写入链通过。
既有[消费者三文件 49 项](../r4-runtime-thread-scope-2026-10-08-v3/README.md)及
[原组件安装结果](../r4-runtime-thread-scope-2026-10-08-v1/README.md)按各自范围读取，
不拼接跨版本、重复或诊断集合形成新的完整产品成绩。
**B4、完整 B7、P1、默认决定 Writer、R3、R4 与商用发布仍 OPEN／NO-GO。**

## 2. 候选、装配与数据流

生产源码提交 d5c572aff2fedae11d25fd1b0e8a4ca41062a8d2，原 Python 3.12.7、uv.lock 与 Git 2.53。
Wheel、安装、工作区源码及 Git 对象的 556 个生产成员再次逐字节复核一致。
Wheel SHA、原测试文件摘要、JUnit 与原件清单见 [facts.json](facts.json)。
两个 SDK 集合串行执行，不扩大原 Git 60 秒或 Turn 120 秒期限，不替换原取消、MAC 或全集认证。

[十四节详设](../../changes/m09-r4-git-runtime-thread-scope.md)定义现行类、字段、接口、三幅图、
锁／Task 代际、失败及提交窗口。实际数据链是原 Runtime 持锁 → 原 factory → bind →
BEGIN → 原认证／读写 → COMMIT／ROLLBACK → scope 撤销。
这些安装测试使用实际原 SDK、Runtime、Route、Session、Approval 和临时 Git 仓库，
模型事件来自离线受控 fixture，不产生模型费用，不是实际编码质量评测。

## 3. 完整文件与失败语义覆盖

| 集合 | 完整文件／用例数 | 覆盖职责 |
|---|---|---|
| ports | Prefix SQL 生命周期3、原Task11 | 控制清理、异常、Task与事务窗口 |
| ports | 鲜读Owner4、原物理连接95 | 原连接、路径、关闭、源实例、符号与Task准入 |
| ports | prepared terminal7、review fresh2、runtime fence3 | 同步末端、原失败与Owner／Runtime变化 |
| history | decided reader19、decided terminal12 | 原决定解释、当前依赖与同步末端 |
| history | actual SDK决定来源1、approval history9 | 原审批、重开／恢复与历史认证 |

准确逐文件计数由实际 JUnit 的 `testcase.classname`复算，不从终端点号或 collect 数推定通过。
既有旧 helper 接入失败、并行 Turn 到期失败、部分中断及 cProfile 诊断超时均保留于前序原件。
终态复验不删除旧 FAIL，也不将独立子集替换成完整文件结果。
测试通过证明对应失败合同的有限覆盖；长耗时仍是性能整改问题，不当作 P1 响应性达标。

## 4. 独立研究与下一关键路径

[原 SQLite 原生来源研究](../../research/git-sqlite-native-source.md)与正式候选隔离。
已复现普通置换、指定 ABA 与 SQL UDF 覆盖风险；研究原型不进入当前 Wheel，
不整体迁移 Store，不以主库移动检查冒充完整 FD、WAL／SHM 或三平台认证。
B4仍需要实际 Ref/config 从最后 U／callback 到终端及 COMMIT 的明确保护合同。
P1分层若改变 callback 或检测点，需要版本化合同及完整负控，不能把默认关闭旧研究直接启用。

R3仍需有效授权登记和同安装候选完整真实 Trial；历史严格成功0/20、必需测试1/20、
真实 Beta 接受0未因此改变。当前报告没有新公共接口、持久化格式、模型请求、费用变更或商业验收。
客户工程及副本、非任务主仓目录和历史固定封存未作为本项输入，也未修改。

## 5. 复核入口

[Review Packet](REVIEW_PACKET.md)、[结构化事实](facts.json)、[核验](verification.json)
及 [成员清单](manifest.json)绑定终态；私有原件保存原命令、两组 XML／日志和输入摘要。
前序 v1／v2／v3 不追写，当前追加使用新目录与新 manifest。
原图、Mypy、Ruff／格式使用同一生产候选证据；新文档与完整已跟踪仓库加原 Wheel 的门禁独立执行。
