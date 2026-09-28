---
doc_type: validation-evidence
status: current
version: 1
code_revision: 7519a8e69887ad32532bd45845597fd861445193
owners: [core]
modules: [product_config, session, execution, trusted_actions, delivery, processes]
related_adrs:
  - docs/adr/0090-plan-first-store-maintenance-and-backup.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_product_state_backup.py
  - tests/product_config/test_product_backup_files_windows.py
supersedes: []
---

# 完整产品备份与验真评审资料

## 1. 对象与边界

评审对象是固定`7519a8e`的完整停机备份和原来源只读验真，不是整体恢复或商用发布。
报告、详细设计和结构化资料统一集中于本目录；当前没有新增模型费用或预算重置。

## 2. 源码阅读顺序

1. [完整详设](../../changes/m09-r1-product-state-backup.md)：先理解源状态、候选、独立回执与最终制品的不同信任身份。
2. [`state_backup_contracts.py`](../../../src/harnessix/product_config/state_backup_contracts.py)：六库、原Key、可选Process与闭合成员/容量。
3. [`state_backup.py`](../../../src/harnessix/product_config/state_backup.py)：Owner、全部库静默、复制、核验、回执、发布与取消结算。
4. [`state_backup_files.py`](../../../src/harnessix/product_config/state_backup_files.py)：原Handle/FD私有性、版本、有限复制与平台排他Rename。
5. [`state_backup_validation.py`](../../../src/harnessix/product_config/state_backup_validation.py)：原Schema和Session/Event/Artifact认证；不初始化或补签。
6. [`state_backup_records.py`](../../../src/harnessix/product_config/state_backup_records.py)：Plan/Audit、事务/CAS与Process跨Store引用。
7. [`sqlite_readonly.py`](../../../src/harnessix/sqlite_readonly.py)：复用正式Reader的只读端口，不改写已有Store。
8. [完整产品专项](../../../tests/product_config/test_product_state_backup.py)：真实默认产品和真实文件/进程事实，不把Scripted Provider当模型质量。

## 3. 必须核查的问题

- 取消及重复取消是否在唯一原工作线程和SQLite连接结算之后释放根外Owner？
- 六库、原Key、CAS和可选Process是否全部捕获，是否只依赖单库或普通SHA？
- `_quiet_databases`是否持有全部库保留写锁，且Backup用不同只读连接？
- 各只读构造器是否在目录/Schema/Owner初始化之前返回，缺库是否保持拒绝？
- 验真是否沿原独立回执授权，是否可能接受备份自签新Key或重签旧历史？
- 源文件变化、错MAC/Schema、坏Blob与跨Store孤儿引用是否在发布前拒绝？
- 目录已经Rename但返回失败时，是否保留原回执并避免把错误响应推断为未提交？
- 实例UUID碰撞、候选地址替换与合法SHM消失是否使用不同失败/清理边界？
- Windows skip和本机端口实现是否被错误解释为完整Windows产品支持？
- 五条新增基础依赖边是否有明确方向，是否保持原阈值及无新增循环？

## 4. 证据复核

原业务文件漂移、合法SQLite侧文件消失和发布确认丢失RED全部保留，最后一个场景使用真实原生目录发布。
这些RED来自未提交的前置候选，不关联为旧提交的源码事实。
最终成绩绑定同一固定Revision；Python 3.12独立检出也在macOS，不能外推为Linux或Windows运行。
实际Wheel逐字节匹配14个变更生产文件，未包含测试，也未构建sdist。

参见[报告](README.md)、[结构化验证](verification.json)、[合同事实](contract-facts.json)、
[制品证明](wheel-observation.json)及[资料Manifest](bundle-manifest.json)。

## 5. 后续必要工作

R1继续完成整体停机恢复、原地址耐久Restore Journal、提交窗口恢复和启动保护；
R4继续完整Windows装配及实际三平台安装/升级/恢复，不以本报告关闭。
R2许可、R3真实质量、R5独立Beta和R6候选封板保持独立退出条件。
