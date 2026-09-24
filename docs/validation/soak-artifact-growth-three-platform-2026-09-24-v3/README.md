---
doc_type: validation-evidence
status: current
version: 1
code_revision: 6f55e747bdecca1c8550467621028dfcd02079b2
owners:
  - core
modules:
  - artifacts
  - agent
  - session
  - documentation
related_adrs:
  - docs/adr/0092-reproducible-local-soak-and-release-thresholds.md
related_tests:
  - tests/benchmarks/test_soak_artifact_growth.py
  - tests/benchmarks/test_soak_artifact_proof.py
  - tests/artifacts/test_batch_verify.py
supersedes: []
---

# 0.9.3d Artifact增长三平台正式规模基线v3（300件负载）

## 1. 结论与证据边界

源码Revision `6f55e747bdecca1c8550467621028dfcd02079b2`的[手动Artifact增长Soak工作流](https://github.com/carrie1988/Harnessix/actions/runs/36009386203)在Linux、macOS和Windows三个独立Job中均成功。每个平台执行固定2件预热与**300件**正式混合大小件：302件全部完成发布、2925次分页读取覆盖全部记录，到期清理分批收集后正文为0、墓碑与Manifest各302。本基线包含[历史Artifact批量验证修复](../../changes/m09-3d-history-artifact-batch-verification.md)——此前300件负载在约280轮以`context_artifact_timeout`失败（逐条历史验证单步O(N²)），批量入口使三平台首次完成该规模。所有平台均有完整Run、`COMMITTED`和Attempt `FINAL(committed)`，原始文件位于本目录的[`raw`](raw)。上传件来源及18份文件摘要见[证据Manifest](bundle-manifest.json)，跨平台核验结果见[评审包](review-packet.json)。对应[常规CI 36009364391](https://github.com/carrie1988/Harnessix/actions/runs/36009364391)六实例成功。

**本目录的证据等级仅为三平台各一次正式规模基线**：`manifest.status=baseline`不等于性能阈值PASS。三平台Profile已按此基线冻结并封存于[`profiles`](profiles)；第二独立候选与PASS报告须由冻结阈值复验工作流另行产生。前两轮候选FAIL（[20件](../soak-artifact-growth-three-platform-candidate-2026-09-24-v1/README.md)、[60件](../soak-artifact-growth-three-platform-candidate-2026-09-24-v2/README.md)）、v1/v2基线与全部旧Profile保持只读。

## 2. 调用链、环境与数值

调用链为`Soak Runner → AgentRuntime（SoakProvider确定性替身）→ CodingToolRuntime grep → SQLiteArtifactStore.publish/read/collect（夹具Policy：1000件/128 MiB）→ 历史Artifact批量验证 → Session共库`。`artifact_publish`只计真实发布方法耗时，`artifact_read`按每次分页调用计时。

| 平台 | Python/档位 | Run ID | 发布P50/P95/P99（ns） | 读取P50/P95/P99（ns） | 峰值RSS（bytes） |
|---|---|---|---:|---:|---:|
| Linux | 3.12.3 / `c4-m16` | `c32dd097dac9408e8d288e3175a38bc9` | 128,028,761 / 297,561,806 / 334,873,316 | 21,658,481 / 69,950,430 / 81,886,894 | 216,641,536 |
| macOS | 3.12.10 / `c3-m7` | `e24f78020f4e4b41a4c5784355260b79` | 139,471,083 / 281,569,041 / 332,585,209 | 26,144,625 / 54,732,791 / 72,130,416 | 371,359,744 |
| Windows | 3.12.10 / `c4-m16` | `0ea1d42c6fe04e75a5527beb37433c0a` | 313,451,300 / 543,125,600 / 638,441,400 | 35,420,600 / 71,029,800 / 84,153,000 | 192,573,440 |

三个平台端点一致：DB从102,400增至73,842,688字节，Artifact正文水位65,590,782字节，WAL端点为0。故障计数全为0。发布时延随历史增长的逐件正文校验是合同成本（正文SHA-256与记录数核对），其量级如实记录；不同平台只与各自Profile比较。

## 3. 原始证据与独立复核

| 平台 | 原始Run/Attempt | Manifest SHA-256 | 上传ZIP SHA-256 |
|---|---|---|---|
| Linux | [Run](raw/linux/c32dd097dac9408e8d288e3175a38bc9/manifest.json) / [Attempt](raw/linux/attempts/c32dd097dac9408e8d288e3175a38bc9/FINAL.json) | `730fa7588fb10afd9cc46b1f8dcfb9669cfdd969f675561c16f15938127381e5` | `d34eab55cb06a0d0a5d392d94c559578f04b51cbf4c291479cf9169187772255` |
| macOS | [Run](raw/macos/e24f78020f4e4b41a4c5784355260b79/manifest.json) / [Attempt](raw/macos/attempts/e24f78020f4e4b41a4c5784355260b79/FINAL.json) | `4043cc995a8a7efb721f194ee4758ffe879f0f0c4b478d413d9dc0b79ec7aee5` | `dd4c0062ba8194e535261d0d5586524e4738995abcc7057b44a19ac55517c299` |
| Windows | [Run](raw/windows/0ea1d42c6fe04e75a5527beb37433c0a/manifest.json) / [Attempt](raw/windows/attempts/0ea1d42c6fe04e75a5527beb37433c0a/FINAL.json) | `f55005128e6aae899ab4087bc6be3e6f590c8fd295c86321f1b2b3619f4dec77` | `f47f5c91d214aa879f2c0ece03481f90083c9b6e1853becc7af86b407debf188` |

各平台恰有四份Run文件（`samples.jsonl`、`artifact-proof.json`、`manifest.json`、`COMMITTED.json`）和两份Attempt文件（`STARTED.json`、`FINAL.json`）。下载后通过[`read_published_run`](../../../scripts/soak_evidence.py)和[`read_attempt`](../../../scripts/soak_attempt.py)逐份重读，再用标准库按最近秩重算300条发布与2925条读取样本的P50/P95/P99，逐文件重算SHA-256；逐件Proof与原始样本一一对应。常见绝对路径、鉴权Header和密钥模式扫描未发现匹配；这只是针对本白名单证据的检查，不宣称对任意文本的形式化无泄漏证明。

## 4. 三平台预冻结Profile与工程阈值

三份Profile均由对应原始Run/Attempt通过[`publish_profile`](../../../scripts/soak_threshold.py)生成并封印，冻结后不可覆盖。`artifact_publish`与`artifact_read`的P50/P95/P99按基线逐项上浮100%（10000bp），`rss_peak`三个分位数上浮50%（5000bp），DB/WAL/Artifact正向增长上浮50%；上限精确等于`ceil(基线×(10000+余量)/10000)`，候选观测不参与计算。Python范围冻结为`3.12.0`～`3.12.99`，`hardware_class`逐平台绑定基线档位。

| 平台 | Profile ID | 发布P99上限（ns） | 读取P99上限（ns） | RSS上限（bytes） |
|---|---|---:|---:|---:|
| Linux | `70a57857f5e04d189f64ebb65473f6bd` | 669,746,632 | 163,773,788 | 324,962,304 |
| macOS | `a59713b864dd4064851658b03ca886f8` | 665,170,418 | 144,260,832 | 557,039,616 |
| Windows | `6a58fce7813e4593b2af9d25c9b8add4` | 1,276,882,800 | 168,306,000 | 288,860,160 |

预期故障计数与基线精确一致（全为0）。300件负载使发布`p99`为近限件子样本第3高值，可吸收单点I/O停顿；前两轮候选的单点停顿越限机制在本规模下不再成立。这些护栏只覆盖固定302件混合负载的发布、分页与清理，不声明任意规模Artifact存储的SLO。

## 5. 证据边界与后续门禁

本基线只证明固定Revision、固定302件负载在三个托管Runner完成且发布/分页/清理语义成立；固定场景门禁关闭还需要第二独立Run在冻结Profile下取得三平台PASS报告，且候选Revision常规CI六实例成功。前两轮FAIL与全部旧证据保持只读；单平台或缩小负载结果不得充作发布PASS。
