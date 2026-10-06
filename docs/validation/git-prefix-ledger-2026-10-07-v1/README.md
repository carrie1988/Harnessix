---
doc_type: validation-evidence
status: current
version: 1
code_revision: f7d06e2661b4ce91225790edd06a52298446c225
owners: [core]
modules: [product_config, delivery, session]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/delivery/test_git_prefix_ledger.py
  - tests/delivery/test_git_prefix_ledger_review.py
  - tests/delivery/test_git_store_genesis.py
supersedes: []
---

# GitDB v2 全前缀认证账本验收记录

## 验收范围

本记录覆盖空结构初始化、有限五kind物理账本同事务认证、独立规范全集尾锚、
只验真Reader及原SQL合作取消窗口。基线为`f7d06e2661b4ce91225790edd06a52298446c225`。
原默认v1及产品装配不变；不存在默认GitBridge、业务Backup2或商用完成声明。

## 源码与设计

[总体与详细设计](../../changes/m09-r4-git-prefix-ledger.md)包含需求、模块边界、架构/时序图、
数据与接口字段、核心伪代码、失败/恢复、安全、部署兼容及完整测试矩阵。
最终结果、候选源码摘要、发行产物及评审证据以本目录结构化文件为准。

## 最终候选结果

- 单一Wheel源码外关联回归：**1094通过、0失败、0跳过**，137.40秒。
- 原完整治理：**1413通过、0失败、0跳过**，39.92秒；首轮1406通过/7失败保留。
- Wheel内468件Python源码全部逐字节匹配；实际测试加载306个Harnessix模块，全部来自安装目录，sys.path没有候选src。
- Ruff及格式检查通过，Mypy468模块通过；原271件Schema字节不变，新增目录Schema一件。
- 文档544件、12645链接、1049图、26包核验零发现；本增量两图实际渲染并视觉复核。
- 原独立评审96项曾93通过/3失败；修复后独立复验96项全部通过，源码外关联回归再次覆盖同断言。

源码回归1091项与源码外1094项、评审96项及其他关联范围存在交集，**不累加为独立测试总量**。
这是最终限定关联范围通过，不是整库全量、三平台原生或默认产品GitBridge验收。

## 原失败与根因

URI只读误判、手工自引用窗口来源缺证、跨事务复用及旧当前投影漂移均以原红例关闭。
补丁首轮可写探针误置在genesis提前返回之后的失败仍保存，最终共用入口修复覆盖原断言。
另保留新真实领域夹具父目录缺失、系统Git2.24不支持所需对象格式及原治理对照不符；
修正自有夹具路径并使用原规定Git2.53后复验，不修改旧实现、治理断言、容量或期限。

## 结构化证据

- [最终结果](result.json)
- [候选源码完整输入](source-inputs.json)
- [Review Packet](review-packet.json)
- [公开证据SHA256清单](SHA256SUMS)

私有完整证据位于`~/Library/Application Support/Harnessix/verification/git-prefix-ledger-20261007-v1/`。
目录保存各轮JUnit/日志、输入快照、Wheel、安装来源证明、图渲染及原失败；不包含新增模型调用或用户凭据。
本增量模型调用0次，不改变预算与未决费用预留，不更改自动化或安装独立中间件。

## 风险与未完成项

物理MAC认证不能代替正式业务模型、阶段语义、Root/Owner/Scope/Lease、独立批准、
真实A/T/D桥接、CAS对象闭包或跨Store共同捕获窗口。新增桥接索引当前失败关闭。
三平台消费者、R3真实编码质量、既定有限Beta及R1—R6商用门禁继续开放。
