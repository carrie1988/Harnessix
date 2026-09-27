---
doc_type: validation-evidence
status: current
version: 1
code_revision: 829dabf8b7051b1242de2202ca9b1df99dc015bc
owners: [core]
modules: [session, agent, secrets, artifacts, app_server, product_config]
related_adrs:
  - docs/adr/0102-authenticated-history-and-event-seal-core.md
related_tests:
  - tests/session/test_publication_seal.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# 认证历史与Event Seal核心验证报告

## 当前范围

原事件签发与认证核心已实现；默认产品历史授权、SQLite同事务、投影/Artifact、Key Backend与备份/升级尚未实施。
核心测试和当前默认Root风险观察分别记录，不能将测试通过、跨Authority重开或合同存在当作生产跨重启完成。
真实模型调用0次，整体0.9保持未完成，12件Archive权利继续阻塞发布。

## 六文件证据

[合同事实](contract-facts.json)、[验证](verification.json)、[Manifest](bundle-manifest.json)、
[Review Packet](review-packet.json)和[CI快照](ci-observation.json)固定输入与范围。

## 固定源码、实际验证与范围

核心46项和相关1128项通过，两组重叠不相加。五幅Mermaid已实际渲染并逐图检查。
固定Git归档构建Wheel和sdist；Wheel通过两个独立`python -I`消费者进程消费实际SQLite事件，
原字节与独立fixture证明重开验证、错身份/正文/密钥拒绝、当前Scope对安全原文保护通过。
消费者证明单独fixture事务写入，密钥文件为fixture，不代表生产同事务接入或正式Key Backend。
依赖复用本机环境，不声明干净机器安装、可复现构建、正式发行物或三平台验收。
默认Root五查询与普通投影SHA替换风险，在基线和新固定源码中均仍可复现，未关闭历史授权。
完整回归已通过，但不声明0.9.4a、0.9或发布完成；CI在批量推送后后台运行，不逐提交等待。

## 完整回归冻结

- 固定实现：`829dabf8b7051b1242de2202ca9b1df99dc015bc`。
- 回归运行提交：`15f6aea65ce240a81a7e79df09e1050f4eff487b`；Tree：`915c750499c6675ac1d056f83db5c53f298cac10`。
- 结果：**5145 passed / 32 skipped / 406.09秒**。
- 2412个跟踪文件在运行前后原字节不变，Git状态不变；未跟踪安全草稿未读、未运行、未打包。
- 1328个核心输入与固定实现逐字节一致；后续只冻结文档。
- 专项76项包含46项核心及30项证据治理；与相关1128项、完整回归重叠，不相加。
- 九项离线工程门禁通过；许可证门禁仍因12件Archive来源权利不通过，`make check`整体通过未声明。
- CI冻结时未启动，本地回归不代表Linux/macOS/Windows CI或生产安装验收。

## 后续实施顺序

独立持久Key Backend与逻辑Store身份 → 新事件CAS同事务Seal与认证前缀/投影
→ 全部历史及模型前消费入口 → Artifact正文与二进制认证
→ 默认产品Root、升级、备份迁移及三平台真实验收。
缺失原证明的旧历史必须失败关闭并保持原数据，不允许在当前Scope下重新签发。
