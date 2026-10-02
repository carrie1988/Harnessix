---
doc_type: validation-evidence
status: current
version: 1
code_revision: 80c1dad13c98aaabb2db1630c62134a241df44aa
owners: [core]
modules: [delivery, product_config, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/governance/test_windows_git_native_branch_observation.py
supersedes: []
---

# Windows Git 定点分支观察：发布基线集成准备

## 1. 当前身份与适用范围

本包记录已封印诊断候选在发布基线上的安全快进与精确提交准备，不是Windows原生运行报告。
原研究base为`dfba34e707ed845f3e9d844461e124015c22dca7`，当前集成base为`80c1dad13c98aaabb2db1630c62134a241df44aa`。实际执行`git merge --ff-only`，退出0；没有reset、stash或rebase。
原候选23件新增文件、16件原只读输入逐字节不变；tracked/index在快进前后均clean，快进前后均只有精确23件候选untracked。
新集成资料只增加本目录6件文件，准备提交的精确新增集合为29件，见[OWNED-FILES.json](OWNED-FILES.json)。没有本地修改任何既有生产文件、workflow、模块或路线图。

[原设计及23件候选](../../changes/m09-r4-git-native-branch-observation.md)继续使用dfba研究身份，不改写原包、原测试、初次FAIL或元数据。
当前39件身份输入为原23件加16件只读输入，不是全仓；原代码限域26件的SHA保持不变。
原候选manifest SHA为`68477380b046e8ee033896784333d915ab676f5e4f549cada1629fcbde9e8b37`。

## 2. 独立审查原封印引用

独立审查归档`git-native-branch-observation-peer-20261002-v1`已终结为`SEALED_BOUNDED_REVIEW_COMPLETE`，结论为仅诊断可合入、原生未验证。
其manifest SHA为`46d2b5e488ab48044d9a63d18a9552bfaf923c2b2aaa169c023743c5540ab68b`；68件封印成员重验0漂移，39件受审输入0漂移，P0/P1/P2均0。
原16件输入与80c1dad发布树一致。当前JUnit重计为50＋19＋3＝72PASS；72是独立审查实际执行结果，不是本次重跑，也不与原关联治理组360PASS累加。
独立审查初次19项中的16PASS/3FAIL及全部原件保持，初次失败属于审查夹具，未改写为产品发现。

[SOURCE.json](SOURCE.json)只发布成员名、长度、SHA和有限结果，不复制包含个人路径或运行正文的私有报告。
审查范围不含新增6件集成资料；这些新增文件由本包显式DocGate/Secret检查覆盖，不能外推为已完成新的独立审查。

## 3. 文件表与提交边界

| 文件 | 职责 |
|---|---|
| [SOURCE.json](SOURCE.json) | dfba/80身份、39件实际SHA、原26件代码身份及独立审查封印引用 |
| [OWNED-FILES.json](OWNED-FILES.json) | 23件不变候选加6件新资料的精确29路径；只读16件不进入提交集合 |
| [verification.json](verification.json) | 实际快进核验及显式覆盖未跟踪新增文件的有限门禁 |
| [review-packet.json](review-packet.json) | 精确commit/push前字节核验、并行集成保护与一次固定revision原生观察参数 |
| [manifest.json](manifest.json) | 当前集成交付及原39件身份的真实字节摘要；自身SHA由外部封印提供 |

整合提交必须只新增精确29件文件，不能用目录或glob带入其他材料。发布负责人须核验最终索引文件集合及blob SHA，记录新完整40位commit后再推送。
如并行集成已推进发布基线，重新检查16件原输入与冻结候选，不回退、覆盖或自动放宽合同。80c1dad是本包集成base，不是已含本候选的最终commit。

## 4. 有限门禁与证据边界

DocGate使用现有文档策略和实际读取API，显式覆盖原候选3件Markdown及本包README，共4件；检查metadata、链接、标题、Mermaid结构及精确新增集合的同步规则。
Secret使用现有`scan_paths`及默认固定预算，显式传入全部29件owned路径，包括本目录全部6件、JSON、源码及PNG；不能只依赖git tracked发现跳过新文件。
实际门禁结果以[verification.json](verification.json)为准。私有检查回执保留每次被检查输入的长度及SHA，最终检查在全部资料封存后进行。
本包不重复源码测试、图渲染或官方二进制检查，不把旧绿色结果计成新原生证据；原包结果和独立审查结果分别引用。

## 5. 后续一次原生观察

已授权的下一执行为精确提交推送后的一次`workflow_dispatch`，输入`execute=true`且`expected_revision`必须等于该最终commit SHA；只一个Windows Job、attempt1，无重跑。
默认入口仍dryrun；现场原Git/官方固定PDB/工具/符号/机器码任一不匹配必须拒绝，不替换程序、不下载大型SDK、不放宽绑定。
实际门槛为原发行Git的RVA `0x70b74`和`0x70bc1`返回分支观察，不以ARM、echo或helper成功代替。FSTAT失败时INDEX只能明确未进入，不能补造返回。
原20秒命令、45秒操作、240秒watchdog、5分钟step、13hook/22argv、RO+DOD、Owner/Job、MAC/双raw/EOF/protection和容量合同全部不变。
公开artifact只允许`result.json`及`result-sha256.json`。原pytest失败继续非0；branch见证完整不等于产品Owner链验收通过，`original_sdk_acceptance=false`。

当前没有stage、commit、push、dispatch或Windows/CDB执行；最终commit尚未创建。历史原生FAIL继续保持，根因UNKNOWN；Windows/R3/fullCommit与商业门禁不关闭。
