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
  - tests/product_config/test_git_link_user_observation_consumption.py
supersedes: []
---

# 原生及末端研究评审包

## 1. 核验步骤

1. 验证[事实与原件摘要](facts.json)：现行候选d5c、原Wheel、原stdlib连接、官方固定源文件及各研究run分开。
2. 复算20／0／1主矩阵；检查原14／3、16／1、18／1及连接重新初始化红测没有删除或改记为通过。
3. 对原API／lease／xDestroy逐层核对关闭、GC、覆盖、重复释放、跨线程、双连接与progress无SQL重入。
4. 读取两个B4实际SDK事实：原U完成、callback注入、Ledger仍接受、Store及行未变。JUnit绿表示探针有效，不是安全绿。
5. 检查主库移动能力与完整FD／WAL／SHM／三平台的边界；不把研究dylib装配到当前Wheel。

## 2. 准入决定

可以接纳有限研究结论及已确认缺口；不能接纳默认Writer、NativeBridge、完整B7／B4、P1或R3通过。
后续生产窄桥和Ref/config终端见证必须有正式契约、失败／资源语义、同候选原消费者与平台安装证据。
当前无模型、费用、客户项目、公共协议或数据库迁移变更。
