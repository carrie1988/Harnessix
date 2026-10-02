---
doc_type: validation-evidence
status: current
version: 4
code_revision: f887aae8bf54789fa2424f7cbc62bd335a1ccd47
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

# Git 尾锚端口评审包

## 评审对象

输入HEAD、全部源码和输入摘要见[SUMMARY.json](SUMMARY.json)、[INPUTS.json](INPUTS.json)。详细合同见[SDD](../../changes/m09-r4-git-prefix-publication.md)，当前530关联用例、原peer文件10用例复跑及所有失败见[REPORT.md](REPORT.md)。

## 必查边界

1. claims精确标量、真实验证见证和新快照：construct/copy/subclass/extra不直接越界，验证标记不进入持久数据。
2. 新purpose/末尾NUL新域：原五kind和旧MAC/Seal不改，无新Key/任意JSON用途。
3. 真Verifier没有公共issue，不调用当前Scope；Store/Key/MAC/canonical/claims先于body观察。
4. 私有body只做原完整SHA/长度，不parse、不保护出口；原64MiB/4096B和64KiB检查点保持。
5. 原Scope身份/冻结context保护后重验；最终checkpoint之后必须再读实际原Scope，末尾Binding-open保留。原取消/期限抛出对象优先传播；不得以弱snapshot代替。
6. 默认产品无消费装配；不把端口、rev范围、历史MAC当作DB CAS/全集覆盖/当前Root/批准/执行权。

## 证据入口

- [当前最终XML](junit/closure-v3-green-related-suite.xml)、[原524 XML](junit/final-session-related.xml)、[原基线XML](junit/baseline-old-session-related.xml)
- [代码保持校验](INTEGRITY.json)、[旧文件精确diff](support/store-publication.diff)
- [命令](COMMANDS.md)、[包清单](MANIFEST.json)、[图形校阅](DIAGRAM_REVIEW.md)

## P1保留与独立复核门限

[P1记录](P1_CLOSURE.json)保留原四SHA/原独立9PASS1FAIL/本地固定RED及最小修复GREEN。该历史记录发布后的独立复核已经完成：[新独立闭环记录](INTEGRATION.json)固定当前四SHA与原真实Scope负例，原10负例、530关联槽位及2项共同关闭优先级均通过，两个未修改源及旧证据SHA保持。开发侧10PASS仍不作为独立签字，限定端口审查不代替产品归属或完整Git业务验收。

## 后续集成门限

完整canonical catalog及全集关联、同GitDB事务尾锚和revision CAS、只读Loader、genesis/legacy门限、Backup v2整根恢复与明确新Root重绑/新批准均另行实现及验收。没有发布或完整W1结论。
