---
doc_type: validation-evidence
status: current
version: 1
code_revision: 46ca9a2006b5f857ecb55924ba969cf2e331187e
owners:
  - core
modules:
  - agent
  - session
  - context
  - documentation
related_adrs:
  - docs/adr/0092-reproducible-local-soak-and-release-thresholds.md
related_tests:
  - tests/benchmarks/test_soak_long_session.py
  - tests/benchmarks/test_soak_context_proof.py
supersedes: []
---

# 0.9.3d 长会话Context/Compaction两平台正式规模基线与Windows诊断

## 1. 结论与证据边界

源码Revision `46ca9a2006b5f857ecb55924ba969cf2e331187e`的[手动长会话Soak工作流](https://github.com/carrie1988/Harnessix/actions/runs/35973891664)中，**Linux与macOS两个Job成功**完成固定1000 Turn真实Context/Compaction负载（5次预热、199次摘要与窗口发布、逐Turn低敏事件Proof），**Windows Job在测量阶段以`soak_turn_timeout`失败**。Linux/macOS原始Run、`COMMITTED`和Attempt `FINAL(committed)`位于本目录的[`raw`](raw)；Windows仅有失败Attempt诊断原件。上传件来源及文件摘要见[证据Manifest](bundle-manifest.json)，核验结果与Windows诊断见[评审包](review-packet.json)。对应[常规CI 35971925613](https://github.com/carrie1988/Harnessix/actions/runs/35971925613)六实例成功。

**本目录的证据等级仅为Linux/macOS各一次正式规模基线**：`manifest.status=baseline`不等于性能阈值PASS，长会话场景的三平台门禁**未关闭**。Linux/macOS两份Profile已按各自基线冻结并封存于[`profiles`](profiles)；Windows没有基线也没有Profile，其证据状态只能记为`unverified`。历史macOS单平台诊断件（soak-macos-2026-09-23系列）保持只读。

## 2. 调用链、环境与数值

调用链为`Soak Runner → AgentRuntime（SoakProvider确定性替身）→ ContextEngine + Compaction → SQLiteSessionStore`，逐Turn记录本地时延并核验Replay一致性、逐Turn事件Proof与压缩账本。

| 平台 | Python/档位 | Run ID | Turn P50/P95/P99（ns） | 峰值RSS（bytes） | DB端点（bytes） |
|---|---|---|---:|---:|---:|
| Linux | 3.12.3 / `c4-m16` | `630fd3827b8e4dab8d16768ad77d7ff0` | 3,922,978,337 / 9,573,853,163 / 12,202,611,425 | 259,420,160 | 18,370,560 |
| macOS | 3.12.10 / `c3-m7` | `2c9eb96d850041059b25bf017da921de` | 3,056,506,791 / 7,152,122,584 / 9,609,343,500 | 399,523,840 | 18,370,560 |

两平台均为1000条正式Turn样本、199次摘要请求、故障计数全0、DB从102,400增至18,370,560字节、WAL端点为0。Windows失败事实：Attempt `eb27dd535bca4635837535b4d098e9ab`在`measuring`阶段以`soak_turn_timeout`结束，未产生Run。分析：Linux的Turn P99为12.2秒、macOS为9.6秒，而固定场景每Turn期限为30秒；Session追加路径每批次重写完整快照（随历史增长总量为O(N²)），Windows托管Runner的SQLite写入成本在相同负载下显著高于另两个平台，使单Turn在测量阶段越过30秒期限。该边界是当前Session持久化设计在Windows上的实测规模限制，不是Turn语义缺陷；Windows正式基线须待Session快照路径的规模优化另行设计后重新执行，不通过放宽每Turn期限或降低1000 Turn门槛取得。

## 3. 原始证据与独立复核

| 平台 | 原始Run/Attempt | Manifest SHA-256 | 上传ZIP SHA-256 |
|---|---|---|---|
| Linux | [Run](raw/linux/630fd3827b8e4dab8d16768ad77d7ff0/manifest.json) / [Attempt](raw/linux/attempts/630fd3827b8e4dab8d16768ad77d7ff0/FINAL.json) | `f7f3cddc13d5d4aca4a77073349fe0a5c54a216576fb3ff31687ec1a6ef0e9f4` | `a2f6bde9f3f72edb0884f22874ed349eba6433b6b90184dea26b350bef93162a` |
| macOS | [Run](raw/macos/2c9eb96d850041059b25bf017da921de/manifest.json) / [Attempt](raw/macos/attempts/2c9eb96d850041059b25bf017da921de/FINAL.json) | `3beb123f183686a66c58d02bd7b16f3afbb43dc573c8eba81b798c1df8175865` | `5c33ce3a31c4a53c5379c58e7627cfd4a59dc8057206aae46a45be371b388ae9` |
| Windows | 仅[失败Attempt](raw/windows/attempts/eb27dd535bca4635837535b4d098e9ab/FINAL.json) | 无Run | `d197a0cff022753bf4e6264f7055f28fccfbc1083e4ae189a6de83e8f766c856` |

Linux/macOS各有四份Run文件（`samples.jsonl`、`context-proof.json`、`manifest.json`、`COMMITTED.json`）和两份Attempt文件。下载后通过[`read_published_run`](../../../scripts/soak_evidence.py)和[`read_attempt`](../../../scripts/soak_attempt.py)逐份重读，再用标准库按最近秩重算1000条Turn样本的P50/P95/P99，逐文件重算SHA-256。常见绝对路径、鉴权Header和密钥模式扫描未发现匹配；这只是针对本白名单证据的检查，不宣称对任意文本的形式化无泄漏证明。

## 4. 两平台预冻结Profile与工程阈值

两份Profile均由对应原始Run/Attempt通过[`publish_profile`](../../../scripts/soak_threshold.py)生成并封印。`turn_local`的P50/P95/P99按基线逐项上浮100%（10000bp），`rss_peak`三个分位数上浮50%（5000bp），DB/WAL/Artifact正向增长上浮50%；上限精确等于`ceil(基线×(10000+余量)/10000)`。Python范围冻结为`3.12.0`～`3.12.99`，`hardware_class`逐平台绑定基线档位。

| 平台 | Profile ID | Turn P99上限（ns） | RSS上限（bytes） | DB增长上限（bytes） |
|---|---|---:|---:|---:|
| Linux | `ba9ecd527e0d4694b75891c5c8111199` | 24,405,222,850 | 389,130,240 | 27,402,240 |
| macOS | `8a93bc6860334396a5f4a6c0a6075c1a` | 19,218,687,000 | 599,285,760 | 27,402,240 |

预期故障计数与基线精确一致（全为0）。Windows无Profile：任何Windows复验只能判`unverified`，不得借用Linux/macOS阈值。

## 5. 证据边界与后续门禁

Linux/macOS的场景候选复验可在冻结Profile下进行；即使两平台候选PASS，长会话场景的三平台门禁仍保持未关闭，直到Windows在相同固定负载下取得正式基线、冻结Profile与第二独立Run。Session快照规模优化的专项设计是Windows正式基线的前置条件，不属于本归档。
