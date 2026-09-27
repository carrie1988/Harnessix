---
doc_type: validation-evidence
status: draft
version: 1
code_revision: pending
owners: [core]
modules: [agent, app_server, protocol, session, secrets]
related_adrs:
  - docs/adr/0099-input-persistence-and-command-publication.md
  - docs/adr/0098-model-stream-publication-boundary.md
related_tests:
  - tests/agent/test_input_publication_runtime.py
  - tests/app_server/test_command_publication.py
supersedes: []
---

# 用户输入持久前与命令回执保护验收

## 1. 验收范围与环境

本机macOS、Python 3.13、实际Runtime/SQLite/SDK及确定性Provider；不执行网络模型。
完整源码、原版独立负例、专项、全量、发行物、门禁和历史字节证明在冻结后记录，草案不是正式发布验收。

## 2. 证据文件

[bundle-manifest](bundle-manifest.json)、[contract-facts](contract-facts.json)、[verification](verification.json)、
[review-packet](review-packet.json)、[ci-observation](ci-observation.json)与本README组成单一六文件交付目录。

## 3. 风险和未验收边界

JSON-RPC id及错误path回显、Scope丢失后的SDK cancel可用性、旧历史授权、跨重启Seal、所有Provider凭据、
12项Archive权利、三平台真实发布和Beta均独立开放；当前切片不关闭0.9.4a或0.9。
