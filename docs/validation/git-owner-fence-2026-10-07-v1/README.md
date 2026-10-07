---
doc_type: validation-evidence
status: reviewing
version: 1
code_revision: 12e30d333334234c1ad73789f392aee4c7bedf36
owners: [core]
modules: [trusted_actions, product_config]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/trusted_actions/test_readonly_runtime_fence.py
  - tests/product_config/test_git_review_runtime_fence.py
  - tests/product_config/test_git_prepared_link_ledger.py
supersedes: []
---

# Git 原 Owner 前置的限定验证报告

## 1. 范围与判定

验证[详细设计](../../changes/m09-r4-git-readonly-owner-fence.md)中原算法提取、原身份/字段冻结、显式事务门禁与终端写保护。
状态为 **PARTIAL_COMPONENT_NOT_RELEASE**，不是完整 Owner 新鲜性、默认 Git、approved Writer 或商业发布验收。
只读核验不取得新 Owner，不创建 Store、Token、Schema、账本或业务行，不执行共享 checkpoint。原写事务先执行原写保护，再复用相同原算法。

## 2. 固定候选与安装范围

研究基线由元数据记录，不假定该提交已包含新增实现。完整[源输入](source-inputs.json)、[结构化结果](result.json)和[本包摘要](SHA256SUMS)固定实际候选。
本机 macOS 受管 Python 3.12.7 的源码外 Wheel 为 `harnessix-1.0.0rc1-py3-none-any.whl`，SHA256：
`bb62c8f817ff80a2e0f3788d397b1fbfd18d77e28b768c2fe461d8acfda7aa8b`。
545 个包成员、504 份 Python 源码在主工作区、独立克隆、Wheel 和安装目录逐字节相同。全部验证生产导入来自安装目录，测试/依赖仍使用原受管环境；不是全新依赖安装或其他平台验收。

## 3. 实际分组结果

| 安装范围 | 通过 | 失败 | 错误 | 跳过 | 秒 |
|---|---:|---:|---:|---:|---:|
| new | 24 | 0 | 0 | 0 | 68.083 |
| domain | 3771 | 0 | 0 | 23 | 773.975 |
| associated | 59 | 0 | 0 | 0 | 930.196 |
| governance | 1413 | 0 | 0 | 0 | 42.305 |

集合有交集，不累加为唯一测试数。平台跳过仅代表本机未运行该平台场景，不计为 Windows/Linux 验收。源码定向集合24项另行通过。
真实原 BEGIN IMMEDIATE 中只读核验与原写核验均返回原 Fence，仅执行两次 SELECT、零变更且保留调用方事务；由调用方回滚，不自动结束事务。

## 4. 保留失败与 P1 未闭合项

原方法缺失9失败、原 Host 接受身份/可见代次漂移2失败、显式 WAL 旧快照1失败均保留。首轮定向21通过/3失败来自只读 Owner 场景与原 Ledger 夹具强制创建 GitDB 的冲突；修复仅增加默认 True 参数，新场景显式 False，原默认缺目录断言保留。
治理初次1412通过/1失败为详设缺少规定的“接口设计”语义章节；补齐章节后最终整组通过。四次 runner hook 参数错误与首次异步探针配置错误为工具失败，未启动目标业务，不计作产品回归成绩。

**隐式游标快照：真实 SDK 单独 Host 反例1失败，仍未修复。** 原连接保留未耗尽 SELECT 游标；外部 WAL 提交 Owner 改变后 `in_transaction=False`，Owner SELECT 与同连接 data_version 都读旧值，Host 仍通过。不能将显式事务门禁表述为完整新鲜性。
原 Ledger 独立监视连接的其他防线未由此反例证实绕过；入口前旧快照仍须独立处理。禁止以 xfail、跳过、重开业务 Store 或 unsafe 句柄探测掩盖问题。

## 5. 原边界与后续工作

原282份 Schema、列出的政策与 CI 字节不变，原 Owner/摘要算法提取逐字节等价；四幅图已实际渲染并查看。
approved Writer、完整 B7、Git/SQLite终端强一致语义、NativeBridge/A/T2/D/Commit/Backup2、R3费用与质量、消费者三平台及正式多用户Beta仍开放。
单人先导可以采集真实任务反馈，但实际任务仍为0，不替代独立用户门槛。

## 6. 评审与证据

[Review Packet](review-packet.md)列出可确认不变量与未确认边界。原 XML、导入路径、完整失败、SQLite/SDK探针、固定Wheel、图像和私有 MANIFEST 保存于专用交付目录，公开包仅列摘要，不含数据库正文、凭据或 Token。
