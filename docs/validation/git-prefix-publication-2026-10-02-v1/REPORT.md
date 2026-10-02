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

# Git独立尾锚认证端口验证报告

## 1. 验证对象与结论

**P1已最小修复并本地验证，新的独立复核待完成；默认产品未装配，完整W1未完成。** 输入HEAD：`7bbce1033925eaf758e295b3c76fc65dee446f30`。源码由下列SHA256固定；版本3仅调整最终实际Scope复核及其测试，不构成发布/Wheel验收。原四源SHA及原审查反证保留于[P1记录](P1_CLOSURE.json)。

端口复用原Binding Key/生命周期/原Scope/签名/正文哈希，独立有限purpose/新域。真实Verifier无issue、不读当前Scope；先验身份/MAC及唯一编码，再观察正文；仅低敏Seal经过保护，私有body不parse/公开。这不证明完整catalog、全前缀覆盖、当前Root归属或执行权。

## 2. P1闭环实际结果与历史524记录

原独立审查9PASS/1FAIL发现最终checkpoint关闭真实Scope后交付候选。原Seal在Scope仍open时签名并保护，此发现不证明关闭后签名、MAC伪造、泄密或耐久写效果。原独立[RED XML投影](junit/peer-original-negative.xml)和其有限原成员SHA保留，不重写为GREEN。

| 本次阶段 | 实际结果 | 公开结果投影 |
|---|---|---|
| 正式行为RED，生产未修 | **1 failed，DID NOT RAISE KernelError** | [log](logs/closure-v3-red-final-scope-close.log)、[XML](junit/closure-v3-red-final-scope-close.xml) |
| 最小修复后同一负例 | **1 passed** | [log](logs/closure-v3-green-final-scope-close.log)、[XML](junit/closure-v3-green-final-scope-close.xml) |
| 尾锚组 | **152 passed = 原146＋新增6** | [log](logs/closure-v3-green-prefix-suite.log)、[XML](junit/closure-v3-green-prefix-suite.xml) |
| 原关联全组＋尾锚 | **530 passed = 原378＋尾锚152；0失败/错误/跳过** | [log](logs/closure-v3-green-related-suite.log)、[XML](junit/closure-v3-green-related-suite.xml) |
| 原独立负例文件开发侧复跑 | **10 passed**，文件SHA未变，非新独立签字 | [log](logs/closure-v3-green-peer-replay-v2.log)、[XML](junit/closure-v3-green-peer-replay-v2.xml) |
| Ruff check / format | 四文件PASS | [check](logs/closure-v3-final-ruff.log)、[format](logs/closure-v3-final-format.log) |
| mypy生产 / 含测试 | 原strict三源PASS；四文件限定follow-imports=silent PASS | [生产](logs/closure-v3-final-mypy-production.log)、[含测试](logs/closure-v3-final-mypy-with-test.log) |
| 源码与旧证据保持 | 原146测试AST、原六测试hash、旧v1/v2封印及原peer反证不变 | [校验](support/closure-v3-integrity.json)、[log](logs/closure-v3-closure-preservation-check-v3.log) |

新增6项覆盖：真实Scope在最终checkpoint正常关闭、同回调关闭后抛TurnCancelled/TimeoutError/CancelledError的原异常对象优先级（3项）、第三次实际Scope读取时关闭Binding、成功路径的真实读取顺序。生产增量只是一项原`artifact.scope_digest(protection)`调用及中文注释，放在最终checkpoint后，末尾Binding-open仍在；没有弱摘要替身或旧MAC/模型/体积/权限放宽。原源码[diff](support/git_prefix_publication.py.closure-v3.diff)与[测试新增diff](support/test_git_prefix_publication.py.closure-v3.diff)可核对。

**以下是原v1历史结果，不是当前版本复跑数；不能用524绿态掩盖P1，亦不将不同组的530与10相加。**

| 阶段 | 实际结果 | 公开结果投影 |
|---|---|---|
| 原相关组基线 | 378 passed | [log](logs/baseline-old-session-related.log)、[XML](junit/baseline-old-session-related.xml) |
| 新入口RED | 1 collection error，exit2；不是行为断言红态 | [log](logs/red-new-prefix-tests.log)、[XML](junit/red-new-prefix-tests.xml) |
| 首轮新增/关联GREEN | 141 / 519 passed | [新增](logs/green-new-prefix-tests.log)、[关联](logs/green-session-related.log) |
| 生命周期补测 | 144 passed | [log](logs/green-new-prefix-tests-v2.log) |
| 最终原关联组＋新用例 | **524 passed = 原378＋新146；0失败/错误/跳过** | [log](logs/final-session-related.log)、[XML](junit/final-session-related.xml) |
| Ruff check / format | 四文件PASS | [check](logs/final-ruff.log)、[format](logs/final-ruff-format.log) |
| mypy生产 | 原strict，三个生产文件PASS | [log](logs/final-mypy-production.log) |
| mypy含新测试 | 四文件限定`--follow-imports=silent` PASS；非全仓检查 | [log](logs/final-mypy-test.log) |
| 原行为保持 | 原codec字面与去新增后AST保持；六个原测试文件等于输入HEAD | [JSON](INTEGRITY.json)、[log](logs/final-integrity-v2.log) |

范围为`tests/session`、`tests/agent/test_authenticated_store.py`、`tests/artifacts/test_authenticated_body.py`、`tests/product_config/test_product_state_backup.py`、`tests/product_config/test_product_state_restore.py`。当前524→530比较仅将原Artifact publication_epoch及原claims store_genesis_epoch两个已存在的uuid4显示ID按各自确定参数槽位核对，其余身份精确比较；原断言/预算不改。历史阶段保留原封印；本次最终530覆盖全部原524槽位及新增6项，原测试/断言不改。

## 3. 失败证据保持

本次另保留三条非预期工具层失败：首次复跑外部peer文件未指定项目配置，pytest rootdir不属于本仓，6个async用例未执行，得到4PASS/6FAIL（[log](logs/closure-v3-green-peer-replay.log)、[XML](junit/closure-v3-green-peer-replay.xml)）。按原独立命令补`-c pyproject.toml`后10PASS，未修改peer文件或断言。保持检查[初轮](logs/closure-v3-closure-preservation-check.log)因上述结果未达10PASS停止；[第二轮](logs/closure-v3-closure-preservation-check-v2.log)因原claims随机UUID显示ID未按槽位核对停止；[实际差异](logs/closure-v3-closure-identity-diagnostic.log)定位后仅修证据比较脚本，[第三轮](logs/closure-v3-closure-preservation-check-v3.log)通过。原正式RED及全部失败原始日志/XML均保留，不覆盖。

全部原失败保留于原不可变归档；公开投影保留其有限成员/SHA256定位，不能当作逐字原日志。

| 记录 | 原失败及限定修正 |
|---|---|
| [preflight](logs/preflight-shared-state.log) | 全clean预设及首次runner启动失败；共享变动保留，没有清理他人改动 |
| [RED](logs/red-new-prefix-tests.log) | 新入口缺失导致collection error |
| [mypy初轮](logs/mypy-prefix-test.log) | 新测试三项静态错误：None返回值、copy更新字典推断、非显式导出保护函数 |
| [mypy补测](logs/mypy-prefix-test-v2.log) | 新测试非显式导出asyncio访问错误，限定改为原生模块引用 |
| [patch](logs/patch-context-failure.log) | 折行后的patch上下文不匹配，原子拒绝未改文件 |
| [保持初检](logs/final-integrity.log) / [diagnostic](logs/integrity-identity-diagnostic.log) | 原Artifact随机uuid4显示ID造成字面比较差异，仅限定原参数槽位比较 |
| [diff缺失](logs/integrity-followup-unavailable-diff.log) | 初检提前失败未生成diff，后续cat失败；v2实际校验后生成diff |
| [图形校阅](DIAGRAM_REVIEW.md) | 三图首轮标签出现字面换行标记，图源修正后渲染/目视通过；原初稿仍保留 |

原成员与摘要见[ARCHIVE_REFERENCES.json](ARCHIVE_REFERENCES.json)，实际UTC/退出码见[命令记录](support/commands.jsonl)。原封印没有改写。

## 4. 源码与SDD封印

| 源码/测试 | SHA256 |
|---|---|
| `src/harnessix/session/git_prefix_contracts.py` | `dd7063beb13799f6d76fc4f58a236727875aee232147fad5d3d18e08377e52a5` |
| `src/harnessix/session/git_prefix_publication.py` | `8fe909946fdb8dd6e18231338944af433b2616dc06248416e16e4da9e56ada9e` |
| `src/harnessix/session/store_publication.py` | `ca7d1a96b18c4b974a34697084a38302f9aca2a86ba1394678273ce526353c87` |
| `tests/session/test_git_prefix_publication.py` | `3fe41bb1e802d5307b83923ef4d2728d740340ab76588611987cfa138e4395e9` |

当前四源按上表冻结；原四SHA见[P1记录](P1_CLOSURE.json)。`git_prefix_contracts.py`及`store_publication.py`与原封印逐字保持，原146测试AST去除本次4个新增函数后保持；原`_claims/_signed/_verified/_git_body_digest`源码未变。原[Binding增量diff](support/store-publication.diff)继续保留。详细[SDD](../../changes/m09-r4-git-prefix-publication.md)版本3 SHA256为`d08a4762af52b40dba0699ce116ac48eca436609e2c6d6afa1e7e2edfe453782`，实际Scope复核、取消异常优先级与末尾Binding检查同步说明。

## 5. 输入合同与可复核定位

| 项目相对输入 | SHA256 |
|---|---|
| `src/harnessix/session/store_publication.py` | `620a67fc885946a12497698aca149247e3e735d7423f1af5a73313887e74ed66` |
| `src/harnessix/session/git_publication_contracts.py` | `2281750683d21467ba9dc0b7d9e347f839a9ff5c1dfb168f2a28e7baab3da8c4` |
| `src/harnessix/session/publication_seal.py` | `0e11b4d93037d6a7ebe7d9e51f05c0e44ab750da10aa4340f24d63a003c2ba36` |
| `tests/session/test_git_publication.py` | `74999bf1717adf9683a8a8ec6cfebc7aa26d6bc96e318713d7b441b87d403581` |
| `tests/session/test_publication_seal.py` | `8ec95cc36c797a5814aa54c28ab87d5745414a98f832938de40b6183ef3af956` |
| `docs/changes/m09-r4-git-delivery-business-backup-closure.md` | `5e932c6fbb3146cb89cfa4d433a23407d105cf9094ecdfd262c2ba6bf4912fb9` |
| `pyproject.toml` | `e4929170958bb3d104b0b45ac90cc1806b67c11fae6439d7add7e84eba734bd9` |

设计输入的有限定位：archive_id=`git-inventory-owned-loader-design-20261001-v1`，member=`design.md`，SHA256=`227661f85be870c6a76f4e22989d07332311ba856e8942906acc215c7297dbd6`，使用其§5.2/§5.3。该定位不是主机路径，也不公开设计原文。六个原测试和运行时二进制摘要见[INPUTS.json](INPUTS.json)。

## 6. 公开证据规则与图形

公开资料只保留项目相对源码和有限archive/member/SHA256定位，不含个人/私有绝对路径、真实凭据或对话内容。历史原命令、原异常栈和环境定位仅保留在受控归档；公开日志/XML为明确标注的结果投影，保留数量/阶段/退出码，投影SHA与原成员SHA分列，不把投影伪称原件。

所有Markdown含合法YAML头，status=current及完整owner/module/ADR/test/supersedes。签发图mmd/PNG/SVG按新顺序更新并实际本地渲染/目视通过，其他三图9文件逐字保持原封印：[架构](diagrams/architecture.png)、[签发](diagrams/issue-sequence.png)、[验真](diagrams/verify-flow.png)、[恢复](diagrams/recovery-boundary.png)。

包清单[MANIFEST.json](MANIFEST.json)绑定SDD/四源和本包文件，排除自身避免自哈希环。[README](README.md)是入口，[COMMANDS](COMMANDS.md)区分历史定位与可复验模板，[VERIFICATION](VERIFICATION.json)和正式[DOCGATE](DOCGATE.json)记录文档范围结果。

## 7. 未覆盖验收范围

没有新增数据库、迁移、新Key、Tool或默认产品消费。没有Windows-native、默认产品Git闭环、Backup v2整根恢复/新Root重绑、发行Wheel或完整W1验收；历史MAC有效不提供当前执行权，也不证明合法整份旧备份未回滚。

## 8. 历史版本2正式文档门禁结果

版本2实际执行`scripts/documentation_check.py --root . --format json`，全仓静态检查退出码0、**0 finding**：465份文档、11206条链接、965幅Mermaid、26个源码包。没有启用changed-from差异模式或重新渲染。本文范围及范围外均零finding，原初检全部17项记录保留；源/测试四文件、四图和原私有175项封印记录逐字保持。

另核对全部6份范围内Markdown元数据、78处本地交叉引用、公开JSON/XML/结果投影和有限归档成员SHA256。原6份XML用例身份与结果计数保持；原命令/失败原件未公开复制。上述为版本2历史验证，不推定当前版本3DocGate通过；版本3实际结果另列，不替代默认产品或完整W1验收。

## 9. 版本3正式文档门禁与范围外并行变更

首次实际执行全仓静态`scripts/documentation_check.py --root . --format json`，本尾锚范围**0 finding**；全仓退出码1，范围外1项，实际统计为466份文档、11241条链接、965幅Mermaid、26个源码包。此结果不表述为全仓零finding。范围外项是`docs/validation/git-store-v2-schema-2026-10-02-v1/pytest-cache/README.md`缺YAML头，不属于本变更写集，没有修改该文件或门禁策略。原输出保留在有限归档成员及SHA256定位中；[DOCGATE.json](DOCGATE.json)完整保留finding。默认产品与完整W1限制保持。

最终再次实际运行全仓静态门禁：**本尾锚范围0 finding；全仓退出码1、范围外4 finding**，468份文档、11254条链接、965幅Mermaid、26个源码包。范围外并行新增`docs/changes/m09-r4-git-store-v2-schema.md`有3项缺语义章节（失败/恢复/取消/超时、持久化/事务/数据流程、风险/取舍），另有上述pytest-cache README的YAML头1项；未改其文档、fixture或门禁策略。初次及最终失败原输出均保留。

本包实际完整性校验PASS：6份Markdown合法元数据与本地链接、13份JUnit公开投影逐项保持原用例身份/结果、全部有限archive/member/SHA引用验真、两不变源码和旧v1/v2/peer证据保持。公开不含个人绝对路径或真实credential；新四源冻结待独立复核，不作全仓DocGate或完整产品通过结论。
