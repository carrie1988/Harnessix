---
doc_type: validation-evidence
status: current
version: 1
code_revision: 39dda2bb218baed30dcf9b1326f899a3133f497b
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

# 0.9.3d 长会话Context/Compaction三平台正式规模基线

## 1. 结论与证据边界

源码Revision `39dda2bb218baed30dcf9b1326f899a3133f497b`的[手动长会话Soak工作流](https://github.com/carrie1988/Harnessix/actions/runs/36022759216)在Linux、macOS和Windows三个独立Job中均成功。每个平台执行固定1000 Turn真实Context/Compaction负载（5次预热、199次摘要与窗口发布、逐Turn低敏事件Proof、Replay一致性核验）。所有平台均有完整Run、`COMMITTED`和Attempt `FINAL(committed)`，原始文件位于本目录的[`raw`](raw)。上传件来源及18份文件摘要见[证据Manifest](bundle-manifest.json)，跨平台核验结果见[评审包](review-packet.json)。对应[常规CI 36015746312](https://github.com/carrie1988/Harnessix/actions/runs/36015746312)六实例成功。

**本目录的证据等级仅为三平台各一次正式规模基线**：`manifest.status=baseline`不等于性能阈值PASS。三平台Profile已按此基线冻结并封存于[`profiles`](profiles)；第二独立候选与PASS报告须由冻结阈值复验工作流另行产生。Revision `70846f5`的同场景三平台运行因该Revision治理门禁（可读性基线漂移）未过而降级为GitHub上传件诊断，不用于冻结Profile；首次三平台运行的Windows 30秒防挂起失败诊断保留在[两平台归档](../soak-long-session-two-platform-2026-09-24-v1/README.md)。

## 2. 调用链、环境与数值

调用链为`Soak Runner → AgentRuntime（SoakProvider确定性替身）→ ContextEngine + Compaction → SQLiteSessionStore`，逐Turn记录本地时延并核验Replay一致性、逐Turn事件Proof与压缩账本；单Turn防挂起预算为120秒（仅为防挂起护栏，性能判定由样本与冻结Profile承担）。

| 平台 | Python/档位 | Run ID | Turn P50/P95/P99（ns） | 峰值RSS（bytes） | DB端点（bytes） |
|---|---|---|---:|---:|---:|
| Linux | 3.12.3 / `c4-m16` | `2ff4d00537794461837d9ba72ef60d92` | 4,182,679,659 / 10,999,925,755 / 14,120,804,150 | 257,970,176 | 18,370,560 |
| macOS | 3.12.10 / `c3-m7` | `9083bef106c2485ca585867908546537` | 2,869,561,833 / 7,117,688,458 / 9,359,594,750 | 402,964,480 | 18,370,560 |
| Windows | 3.12.10 / `c4-m16` | `88ca93548ec34cdb95cfc3b1052e4b0b` | 13,016,577,600 / 30,693,159,600 / 38,942,609,800 | 222,441,472 | 18,370,560 |

三平台均为1000条正式Turn样本、199次摘要请求、故障计数全0、DB从102,400增至18,370,560字节、WAL端点为0。Windows单Turn时延显著高于另两个平台（P50约13秒、P99约38.9秒），这是Session追加路径随历史增长重写快照的规模成本在Windows托管Runner上的实测表现；该事实如实进入冻结阈值，不跨平台互比绝对时延。

## 3. 原始证据与独立复核

| 平台 | 原始Run/Attempt | Manifest SHA-256 | 上传ZIP SHA-256 |
|---|---|---|---|
| Linux | [Run](raw/linux/2ff4d00537794461837d9ba72ef60d92/manifest.json) / [Attempt](raw/linux/attempts/2ff4d00537794461837d9ba72ef60d92/FINAL.json) | `89107cf40f4e24fdd31d98b820cd800c150518858485c41027a848f8ab02819a` | `3029d0f2ff92418f6ddc0b27cafe1ed8cd44d72015aaae77bffba78a0942a721` |
| macOS | [Run](raw/macos/9083bef106c2485ca585867908546537/manifest.json) / [Attempt](raw/macos/attempts/9083bef106c2485ca585867908546537/FINAL.json) | `ad1178a2d9f1b3597e7dd38eb7c6c617b0a68ff85389d3652f869bfaf33dd671` | `0cc7b2047f41c76fa52311def21377a33241a7e193d7d9b83e7ad3ee7f15b881` |
| Windows | [Run](raw/windows/88ca93548ec34cdb95cfc3b1052e4b0b/manifest.json) / [Attempt](raw/windows/attempts/88ca93548ec34cdb95cfc3b1052e4b0b/FINAL.json) | `83d1ab7b2c55d396975f4384bc63d2bff50d87568a901df9324bdbfd831204d0` | `e1fe2df254deae066619f21fb877c3aa931a342a806e4c0465c21d7fe50f683e` |

各平台恰有四份Run文件（`samples.jsonl`、`context-proof.json`、`manifest.json`、`COMMITTED.json`）和两份Attempt文件（`STARTED.json`、`FINAL.json`）。下载后通过[`read_published_run`](../../../scripts/soak_evidence.py)和[`read_attempt`](../../../scripts/soak_attempt.py)逐份重读，再用标准库按最近秩重算1000条Turn样本的P50/P95/P99，逐文件重算SHA-256。常见绝对路径、鉴权Header和密钥模式扫描未发现匹配；这只是针对本白名单证据的检查，不宣称对任意文本的形式化无泄漏证明。

## 4. 三平台预冻结Profile与工程阈值

三份Profile均由对应原始Run/Attempt通过[`publish_profile`](../../../scripts/soak_threshold.py)生成并封印，冻结后不可覆盖。`turn_local`的P50/P95/P99按基线逐项上浮100%（10000bp），`rss_peak`三个分位数上浮50%（5000bp），DB/WAL/Artifact正向增长上浮50%；上限精确等于`ceil(基线×(10000+余量)/10000)`，候选观测不参与计算。Python范围冻结为`3.12.0`～`3.12.99`，`hardware_class`逐平台绑定基线档位。

| 平台 | Profile ID | Turn P99上限（ns） | RSS上限（bytes） | DB增长上限（bytes） |
|---|---|---:|---:|---:|
| Linux | `ddf402fdfa7942858ef73672e8d8e7ec` | 28,241,608,300 | 386,955,264 | 27,402,240 |
| macOS | `bd9d171ff98c4352b53f85ff0983b19c` | 18,719,189,500 | 604,446,720 | 27,402,240 |
| Windows | `bc65dc97d9ee4663904cfc138747d728` | 77,885,219,600 | 333,662,208 | 27,402,240 |

预期故障计数与基线精确一致（全为0）；任何额外取消、超时、EOF、UNKNOWN、重复效果或孤儿均不得PASS。这些护栏只覆盖固定1000 Turn Context/Compaction负载，不声明真实Provider时延或大量C端用户容量。

## 5. 证据边界与后续门禁

本基线只证明固定Revision、固定负载在三个托管Runner完成且Context/Compaction语义成立；固定场景门禁关闭还需要第二独立Run在冻结Profile下取得三平台PASS报告，且候选Revision常规CI六实例成功。历史诊断与旧证据保持只读；单平台或缩小负载结果不得充作发布PASS。
