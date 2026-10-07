---
doc_type: validation-evidence
status: reviewing
version: 1
code_revision: 03529962a63dfc7818d6b0d0b6874d4e9fc118a3
owners: [core]
modules: [trusted_actions, product_config]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/trusted_actions/test_runtime_owner_observer.py
  - tests/product_config/test_git_review_fresh_owner.py
  - tests/product_config/test_git_fresh_owner_reader.py
  - tests/product_config/test_git_prepared_link_ledger.py
supersedes: []
---

# Git 原 Owner 隐式旧快照整改验证报告

## 1. 范围与判定

[详细设计](../../changes/m09-r4-git-readonly-owner-fence.md)正式修订只读核验合同：保留原 Audit/DB/Fence 与写入口，在每次 Git Host 检查内补充短只读观察连接，复用唯一原 Owner 算法，首末原身份核验保持。
判定为 **LIMITED_OWNER_SNAPSHOT_FIX_VERIFIED_NOT_RELEASE**。关闭的是已复现隐式 WAL 快照及本次资源归属反例，不是全部 Owner 新鲜性、B7、默认 Git 或商用验收。

## 2. 固定候选与来源

macOS、受管 Python 3.12.7、源码外 Wheel `harnessix-1.0.0rc1-py3-none-any.whl`，SHA256：
`63d919699d6e8c28cb73746c043de1befdbfa80a46a9c8fc4976dba1ff4a8d21`。
545 个包成员、504 份 Python 源码在主工作区、独立克隆、Wheel 和安装目录逐字节相等。运行器记录的生产导入均来自安装目录；测试与依赖仍使用原受管环境，不是全新依赖或其他平台验收。
[源输入](source-inputs.json)固定5078份实际输入；研究基线不是新增实现已发布证明。[结构化结果](result.json)、[本包摘要](SHA256SUMS)与[Review Packet](review-packet.md)分别提供成绩、完整性和评审边界。

## 3. 实际结果

| 安装范围 | 通过 | 失败 | 错误 | 跳过 | 秒 |
|---|---:|---:|---:|---:|---:|
| targeted | 36 | 0 | 0 | 0 | 119.523 |
| domain | 1009 | 0 | 0 | 0 | 24.943 |
| associated | 59 | 0 | 0 | 0 | 1824.626 |
| governance | 1413 | 0 | 0 | 0 | 40.350 |

源码定向36项另行通过，耗时115.256秒；Mypy504份源通过，Ruff、合同及可读性检查通过。集合有交集，不累加为唯一通过数。
实际 SDK 覆盖构造/重查旧游标快照、原连接重绑与子类清理、观察失败关闭；实际 SQLite 覆盖只读写拒绝、两初始化PRAGMA失败关闭、特殊路径URI、原事务保持及可观察文件替换。原60秒准备期限和10秒同步扫描期限未放宽。

## 4. 保留的失败与验证宿主事件

035研究基线真实SDK旧快照反例1失败；r1观察候选的冻结原连接误关闭反例1失败。两项原件保留，当前r2固定安装件复验通过，不能删除失败或通过跳过掩盖。
一轮旧候选关联验证和一轮r2关联验证共用临时目录，已因隔离错误中断；原件保留，不计产品缺陷、通过或验收。当前关联成绩仅来自正确解释器、独立目录的新一轮r2复验。首次 Apple Git 环境失败及纯测试 URI 参数冲突同样不作为目标产品反例。

## 5. 资源、写保护与性能

原写入口AST不变，默认原Owner算法除读取连接选择外等价。观察连接明确排除构造冻结的原连接与当前 `_db`；独立Connection子类通过原生基类关闭，不调用可覆盖close。原业务连接和调用方事务不提交、不回滚、不关闭。
观察初始化执行 `query_only`、`foreign_keys` 两项连接级PRAGMA，再查询Owner；不新建Store、Schema、Token或业务行，不执行共享checkpoint。mode=ro仍参与SQLite WAL/SHM锁协调，不是物理零触碰。
500次实际SDK Host检查累计 **0.068520秒**，FD **40→40**；对照旧Host源码＋当前默认算法 **0.010089秒**。无缓存，但这不是旧Wheel对比、生产SLA或长同步Git响应性验收。
原282份Schema、18份列举的政策/CI文件字节不变；7幅当前详设图已重新渲染并逐图查看，含3幅当前图与4幅明确标注的历史图。

## 6. 未闭合边界及试用

原DB实际OS FD、ABA、非协作外部写者、查询后永久Owner、完整B7/dispatch、B4、approved Writer、NativeBridge/A/T2/D/Commit/业务Backup2、Git同步段响应性P1、R3真实质量与费用、三平台消费者和R1～R6仍开放。
[单人先导](../../operations/pilot-beta.md)已有首轮参与准备，真实任务完成数0；不替代3～5名独立开发者、至少15个任务及三平台使用门槛。
原XML、导入来源、脚本、Wheel、失败、图像和MANIFEST存放于专用交付目录；公开包仅保留脱敏结果与摘要，不含凭据、Owner Token或数据库正文。
