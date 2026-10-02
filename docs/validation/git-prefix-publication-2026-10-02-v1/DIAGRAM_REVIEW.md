---
doc_type: validation-evidence
status: current
version: 3
code_revision: 7bbce1033925eaf758e295b3c76fc65dee446f30
owners: [core]
modules: [session]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/session/test_git_prefix_publication.py
  - tests/session/test_git_publication.py
  - tests/session/test_publication_seal.py
  - tests/agent/test_authenticated_store.py
  - tests/artifacts/test_authenticated_body.py
  - tests/product_config/test_product_state_backup.py
  - tests/product_config/test_product_state_restore.py
supersedes: []
---

# 图形渲染与校阅记录

四张图均使用本地Mermaid CLI 11.6.0、现有本地Chrome、neutral主题与PingFang SC字体，未加载外部图像/字体。mmd源与PNG/SVG逐一对应。

| 图 | 目视校阅结论 |
|---|---|
| architecture | PASS；当前端口与后续未装配消费区分、原Key/Scope/hash复用边界可读 |
| issue-sequence | PASS；真实签发顺序、仅Seal保护及Scope → cancel → 实际Scope → Binding-open顺序明确 |
| verify-flow | PASS；MAC在body前、失败不补签、无当前Scope/issue明确 |
| recovery-boundary | PASS；历史认证不授Root/Lease/批准/执行权，完整W1未完成明确 |

初轮三个flowchart使用字面换行转义，渲染命令成功但标签出现可见转义字符。仅将标签改为明确换行后重渲染，最终逐图观察中文完整、无裁切、含义与源码一致。初轮源/PNG/SVG及渲染日志在私有独占证据保留，没有隐藏命令失败或修改生产实现。签发时序图首轮无需修正。

## 资料版本2

四图源/PNG/SVG与原封印逐字相同，未重新渲染；已有视觉结果保持。原图及校阅成员摘要由ARCHIVE_REFERENCES与当前MANIFEST分别定位，生产实现未改变。

## 版本3签发顺序修复

签发图新增最终checkpoint之后的“再查实际原Scope身份/context”，其mmd/PNG/SVG三文件重新以原本地渲染器输出。实际打开PNG逐项核验：保护后Scope → 最终cancel → 实际Scope → Binding-open顺序与源码一致；中文完整、字号可读、无裁切。实际渲染[PNG日志](logs/closure-v3-render-issue-sequence-png-v3.log)及[SVG日志](logs/closure-v3-render-issue-sequence-svg-v3.log)可定位。旧图在原封印和本次before快照保留。其余三图9文件SHA不变，沿用原视觉校阅。
