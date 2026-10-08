---
doc_type: validation-evidence
status: current
version: 1
code_revision: d5c572aff2fedae11d25fd1b0e8a4ca41062a8d2
owners: [core]
modules: [product_config, delivery]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/product_config/test_git_prepared_link_connection.py
  - tests/product_config/test_git_link_user_observation_consumption.py
supersedes: []
---

# Git 原生来源与末端漂移隔离研究报告

## 1. 结论与范围

**RESEARCH ONLY／B4与完整B7 NO-GO。** 不更改生产代码、依赖、安装候选或默认Writer。
研究推进实际缺口求证，不代表正式功能、三平台、真实编码质量或商业验收通过。

- 原SQLite C扩展SQL UDF五例已执行并重现：普通置换、打开B后恢复A、Python函数覆盖风险、加载限制、不支持拒绝。
- 非SQL桥主矩阵20通过、0失败，另1例确认点时检查的连续性限制；追加原异常对象身份配对通过。
- 原型的连接重新初始化漏检红测已复现并完成窄guard修复，旧FAIL及源码均保留。
- 正式安装候选的两个末端漂移诊断均确认拒绝缺口：最后U返回后的callback改配置或同OID symbolic HEAD，
  原Ledger仍接受。诊断JUnit2通过不是安全通过。

## 2. 原生架构、接口与源码

[完整源码研究](../../research/git-sqlite-native-source.md)包含官方固定VFS、接口事实、架构及时序图、
类／字段、伪代码、生命周期、失败、持久化和部署限制。
研究只使用原exact标准库Connection的公开加载入口及原SQLite API表，
不使用CPython私有偏移、不装第二套SQLite引擎、不整体迁移Store。

SQL UDF覆盖及原progress中SQL重入是排除项。非SQLToken采用原句柄、原线程、生命周期撤销与未执行statement锚，
在原mutex内先拒绝zombie／MISUSE，再调用main HAS_MOVED。原借用连接不由Token关闭，
release不COMMIT；关闭、覆盖、重新初始化、未知能力或跨线程均拒绝。
主库inode移动检查不证明完整OS FD设备／inode、WAL／SHM或整个历史未曾变化。

## 3. 实际测试与原失败保留

| 记录 | 结果 | 准确解释 |
|---|---|---|
| SQL UDF可执行重现 | 5例达到预期观察 | 包含覆盖风险确认；不是生产认证 |
| 非SQL早期矩阵 | 14通过／3失败，后继16／1、18／1 | 夹具问题与实际连接重初始化漏检分别记录，原件不覆盖 |
| 非SQLguard矩阵 | 20通过／0失败／1边界反例 | 本机有限支持，未经产品factory／原Audit完整接线 |
| progress原异常对象 | 有桥／无桥均第7次，原对象`is`相同 | 无新增SQL、事务不变；外层SQLite中断依原规范 |
| 两个实际SDK末端探针 | JUnit2通过，实际两例接受漂移 | **拒绝缺口已确认**，不是B4关闭 |

首两个阶段的早期runner按可核对差异重建，该限制显式记录；不宣称重建runner是原始执行文件。
原实际case JSON、日志及红测结果保留。每条原生资源归属由State／lease／userdata计数及关闭记录核对；120次GC／FD短压力只证明该场景平衡，
不替代长期资源SLA、ASan／LSan或完整三平台矩阵。原红测及不同run保存源码／runner／二进制摘要。
所有探针使用隔离临时数据库和Git fixture；不读取客户工程、密钥或费用账本，不发起模型请求。

## 4. B4实际调用链与下一实现面

原链为异步Ref/config → 完整历史await → Source → Index → U返回 → 外部callback → terminal →
Ledger返回 → 调用方COMMIT。两例在原U真实完成后注入原callback，不替换原认证为模拟成功；
数据库行、total_changes及其他Store均不变化，仍观察到pending关联返回。

[正式决定设计](../../changes/m09-r4-git-approved-link.md#101-实际-refconfig-末端缺口与接线顺序)
列出每关联来源见证、原Reader签发、内部terminal消费及提交边界。
不能再添加一次异步查询后声称全过程冻结，也不能只检查`.git/HEAD`与`.git/config`覆盖所有实际输入。
原语可与SQLite来源研究并行；Writer启用仍依赖完整B4、B7与全部消费者。

## 5. 兼容、复核与交付

研究运行在macOS、Python3.12.7／SQLite3.45.3；另一Python的单一加载能力探针不构成完整安装矩阵。
Windows的WIN32_GET_HANDLE与Unix HAS_MOVED不是可互换分支；当前纯PythonWheel未包含本机二进制。
正式封装、加载受限环境、供给完整性和原实际消费者尚未验收。

[事实](facts.json)、[核验](verification.json)、[Review Packet](REVIEW_PACKET.md)及[manifest](manifest.json)
绑定原件、实际异常、官方源摘要及边界。当前默认工具、公共协议、持久Schema和原预算规则未变；
R3、P1、默认完整Git链、三平台与商用发布继续开放。
