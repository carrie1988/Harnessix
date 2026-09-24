---
doc_type: validation-evidence
status: current
version: 1
code_revision: 5f1b3c1152c2eb7577b9265efa63f2ec1f186cdc
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
supersedes: []
---

# 0.9.3d Artifact增长三平台正式规模基线v2（60件负载）

## 1. 结论与证据边界

源码Revision `5f1b3c1152c2eb7577b9265efa63f2ec1f186cdc`的[手动Artifact增长Soak工作流](https://github.com/carrie1988/Harnessix/actions/runs/35965259787)在Linux、macOS和Windows三个独立Job中均成功。本基线是[首轮候选FAIL](../soak-artifact-growth-three-platform-candidate-2026-09-24-v1/README.md)后的负载修复版：正式负载由20件提高到**60件**（近限件15件），预热2件与测量语义不变。每平台62件全部完成发布、585次分页读取覆盖全部记录，到期清理后正文为0、墓碑与Manifest各62。所有平台均有完整Run、`COMMITTED`和Attempt `FINAL(committed)`，原始文件位于本目录的[`raw`](raw)。上传件来源及18份文件摘要见[证据Manifest](bundle-manifest.json)，跨平台核验结果见[评审包](review-packet.json)。对应[常规CI 35964147780](https://github.com/carrie1988/Harnessix/actions/runs/35964147780)六实例成功。

**本目录的证据等级仅为三平台各一次正式规模基线**：`manifest.status=baseline`不等于性能阈值PASS。三平台Profile已按此基线冻结并封存于[`profiles`](profiles)；第二独立候选与PASS报告须由冻结阈值复验工作流另行产生。旧20件基线、旧Profile与首轮FAIL原件在v1目录保持只读，不被本目录取代或删除。

## 2. 调用链、环境与数值

调用链与v1基线一致：`Soak Runner → AgentRuntime（SoakProvider确定性替身）→ CodingToolRuntime grep → SQLiteArtifactStore.publish/read/collect → Session共库`。`artifact_publish`只计真实发布方法耗时，`artifact_read`按每次分页调用计时。

| 平台 | Python/档位 | Run ID | 发布P50/P95/P99（ns） | 读取P50/P95/P99（ns） | 峰值RSS（bytes） |
|---|---|---|---:|---:|---:|
| Linux | 3.12.3 / `c4-m16` | `a4df000d9bf24189bae800e9f0c72265` | 35,489,939 / 75,788,457 / 88,631,683 | 10,219,566 / 38,323,375 / 40,268,942 | 99,430,400 |
| macOS | 3.12.10 / `c3-m7` | `0229f1e9808f41d6901d0e40e98ab4b7` | 20,335,708 / 41,315,417 / 75,326,625 | 9,485,166 / 22,526,375 / 23,102,792 | 115,392,512 |
| Windows | 3.12.10 / `c4-m16` | `7316e536dcd24e229f6a4991f0e5e0a8` | 198,197,200 / 566,344,900 / 693,870,000 | 16,717,020 / 30,450,900 / 31,104,600 | 82,903,040 |

三个平台端点一致：DB从102,400增至11,083,776字节，Artifact正文水位9,624,422字节，WAL端点为0。故障计数全为0。Windows本次基线即在显著噪声条件下取得（发布P99约694毫秒），其Profile上限相应较宽；不同平台只与各自Profile比较，不互比绝对时延。

## 3. 原始证据与独立复核

| 平台 | 原始Run/Attempt | Manifest SHA-256 | 上传ZIP SHA-256 |
|---|---|---|---|
| Linux | [Run](raw/linux/a4df000d9bf24189bae800e9f0c72265/manifest.json) / [Attempt](raw/linux/attempts/a4df000d9bf24189bae800e9f0c72265/FINAL.json) | `见bundle-manifest.json` | `e33a5194ef6519a230b4ec9ceb8cd8a809a1f4856f651fd1f81a0306637f520b` |
| macOS | [Run](raw/macos/0229f1e9808f41d6901d0e40e98ab4b7/manifest.json) / [Attempt](raw/macos/attempts/0229f1e9808f41d6901d0e40e98ab4b7/FINAL.json) | `见bundle-manifest.json` | `5a0e3a0ac8f84ea18116b615ac129da2fd5f8c50a221cd3be3d1ada6543d736e` |
| Windows | [Run](raw/windows/7316e536dcd24e229f6a4991f0e5e0a8/manifest.json) / [Attempt](raw/windows/attempts/7316e536dcd24e229f6a4991f0e5e0a8/FINAL.json) | `见bundle-manifest.json` | `b09f71c6c9043c0ac55e42ee81c9ee4b6e0e0fac3cffa9cbf09fbe76b216ee9e` |

各平台恰有四份Run文件（`samples.jsonl`、`artifact-proof.json`、`manifest.json`、`COMMITTED.json`）和两份Attempt文件（`STARTED.json`、`FINAL.json`）。下载后通过[`read_published_run`](../../../scripts/soak_evidence.py)和[`read_attempt`](../../../scripts/soak_attempt.py)逐份重读，再用标准库按最近秩重算P50/P95/P99，逐文件重算SHA-256；逐件Proof的大小档、记录数、分页样本索引与原始样本一一对应。各平台Manifest SHA-256逐字节记录于[证据Manifest](bundle-manifest.json)。常见绝对路径、鉴权Header和密钥模式扫描未发现匹配；这只是针对本白名单证据的检查，不宣称对任意文本的形式化无泄漏证明。

## 4. 三平台预冻结Profile与工程阈值

三份Profile均由对应原始Run/Attempt通过[`publish_profile`](../../../scripts/soak_threshold.py)生成并封印，冻结后不可覆盖。`artifact_publish`与`artifact_read`的P50/P95/P99按基线逐项上浮100%（10000bp），`rss_peak`三个分位数上浮50%（5000bp），DB/WAL/Artifact正向增长上浮50%；上限精确等于`ceil(基线×(10000+余量)/10000)`，候选观测不参与计算。Python范围冻结为`3.12.0`～`3.12.99`，`hardware_class`逐平台绑定基线档位。

| 平台 | Profile ID | 发布P99上限（ns） | 读取P99上限（ns） | RSS上限（bytes） |
|---|---|---:|---:|---:|
| Linux | `41dd000b3b3b46e6bc69541702ebf49b` | 177,263,366 | 80,537,884 | 149,145,600 |
| macOS | `f8be1a64e12645d4a942e1c3abb2010e` | 150,653,250 | 46,205,584 | 173,088,768 |
| Windows | `a7d523345763463088dd676baf084fc7` | 1,387,740,000 | 62,209,200 | 124,354,560 |

预期故障计数与基线精确一致（全为0）。60件负载使`p95`成为近限件子样本的第3高值而非第2高值，尾部估计代表性较20件改善；`p99`仍为近限件最高值，共享Runner磁盘条件残余漂移风险不被本基线消除，候选若再越限将同样保留FAIL原件。这些护栏只覆盖固定62件混合负载的发布、分页与清理，不声明任意规模Artifact存储的SLO。

## 5. 证据边界与后续门禁

本基线只证明固定Revision、固定62件负载在三个托管Runner完成且清理语义成立；固定场景门禁关闭还需要第二独立Run在冻结Profile下取得三平台PASS报告，且候选Revision常规CI六实例成功。首轮FAIL与v1基线保持只读；单平台或缩小负载结果不得充作发布PASS。
