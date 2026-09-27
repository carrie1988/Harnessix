---
doc_type: validation-evidence
status: draft
version: 1
code_revision: 2ae7caf3e2a6b53431ef352ff3520f3cf1358282
owners: [core]
modules: [app_server, agent, protocol, secrets, session, product_config]
related_adrs:
  - docs/adr/0101-query-publication-and-session-host-binding.md
related_tests:
  - tests/app_server/test_query_publication.py
  - tests/product_config/test_query_publication_root.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# 导出查询原DTO与Session宿主版本绑定验证报告

## 范围与当前核验

六查询原Params/DTO/稳定错误保护、恢复回调后置、构造同一Session对象和原长轮询。
专项181项通过，含62新增功能/观察＋119既有，其中1项是未登记旧历史开放观察，不计历史授权通过。
完整回归与证据最终冻结尚待执行，候选构建和Wheel消费已核验；草案不作整体发布验收。

## 六文件证据与限制

[contract-facts](contract-facts.json)、[verification](verification.json)、[Manifest](bundle-manifest.json)、
[Review Packet](review-packet.json)、[CI观察](ci-observation.json)区分基线、修复、候选与发布阻塞。
[详细设计](../../changes/m09-4a-query-publication-boundary.md)含五图、字段、伪代码及源码定位。
默认Root验证的模型工厂和驱动是替身，不冒充真实网络Provider、三平台安装或完整C端验收。
未知历史/Seal、全部Provider、内部聚合权限和12项Archive权利仍开放，整个0.9保持未完成。

## 独立核验与完整回归状态

专项181项、相关2302项通过；两组重叠，不相加。清洁固定Git归档构建Wheel与sdist，Secret扫描通过。
`python -I`直接消费压缩Wheel中的新模块，真实Runtime/SQLite/导出Service拒绝五查询，
安全独立Session的原DTO和SDK Replay往返通过，不导入项目源码或测试助手。
发行物为固定源码测试候选，源归档文档为全量冻结前版本，不声明正式发布、干净机器安装或三平台验收。
完整回归尚待执行；本草案不作完整回归通过或0.9发布声明。
