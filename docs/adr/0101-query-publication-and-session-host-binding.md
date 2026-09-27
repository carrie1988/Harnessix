---
doc_type: adr
status: draft
version: 1
code_revision: d105d6212b374c20ffef5b20d95cc2de6a88d48f
owners: [core]
modules: [app_server, agent, protocol, secrets, session, product_config]
related_adrs:
  - docs/adr/0100-protocol-frame-and-handshake-publication.md
related_tests:
  - tests/app_server/test_query_publication.py
  - tests/product_config/test_query_publication_root.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# ADR-0101：查询原DTO公开边界和同一Session宿主绑定

## 背景

固定d105d62独立真实Runtime/SQLite/Service探针确认，导出Python查询绕过Transport仍可返回当前材料。
恢复快照公开之前已调度Turn，错误Store装配也未在订阅之前拒绝。

## 决策

六查询共用execute_query：完整原Params先于IO/信号注册，完整原DTO和稳定错误先于返回，恢复回调后置。
序列化warnings=error防止非法构造模型的含值诊断进入stderr；意外错误固定化，Server保持既有-32603分类。
构造时Runtime.store、查询Session和Reader.session必须为同一对象；不从路径或Duck属性推导自定义Request Store身份。
提取Replay候选与50ms长轮询，不增加扫描器、数据库或网络服务。

## 取舍与后果

原DTO/Hash/Schema/导出/方法签名不变；Python底层稳定错误改为公开AgentServiceError。
输出拒绝不清洗历史，不恢复Turn；已closed宿主仍可重放原回执但不再创建后台Task，accepted只表示原事实。
Delta仍可丢失，候选扫描不等于历史Seal；内部Store聚合授权、全部Provider和物理DB身份仍开放。

## 验证与资料

61查询测试含1开放旧历史观察，1默认Root直调验证；取消/超时/关闭、非法模型、错误绑定与原事实均覆盖。
[详细设计](../changes/m09-4a-query-publication-boundary.md)含架构、五图、接口字段、伪代码、部署和源码定位。
[验证证据](../validation/query-publication-2026-09-28-v1/README.md)保持整体release_blocked，不豁免12项Archive权利。
