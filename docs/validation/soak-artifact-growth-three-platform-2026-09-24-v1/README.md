---
doc_type: validation-evidence
status: current
version: 1
code_revision: b3e0d6f731d29b2749aab2827a9ebb302c2e87d6
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

# 0.9.3d Artifact增长三平台正式规模基线

## 1. 结论与证据边界

源码Revision `b3e0d6f731d29b2749aab2827a9ebb302c2e87d6`的[手动Artifact增长Soak工作流](https://github.com/carrie1988/Harnessix/actions/runs/35961271264)在Linux、macOS和Windows三个独立Job中均成功。每个平台执行固定2件预热与20件正式混合大小件（小件与接近`MAX_ARTIFACT_BYTES=1 MiB`上限的近限件交替）：经真实`AgentRuntime`、Coding Tool与`SQLiteArtifactStore`逐Turn发布一件，随后完整分页读取全部记录，最后受控到期清理并核对正文、墓碑与Manifest数量。所有平台均有完整Run、`COMMITTED`和Attempt `FINAL(committed)`，原始文件位于本目录的[`raw`](raw)。上传件来源及18份文件摘要见[证据Manifest](bundle-manifest.json)，跨平台核验结果见[评审包](review-packet.json)。

**本目录的证据等级仅为三平台各一次正式规模基线**：`manifest.status=baseline`不等于性能阈值PASS，也不等于0.9.3d整体完成。三平台Profile已按此基线冻结并封存于[`profiles`](profiles)；第二独立候选与PASS报告须由冻结阈值复验工作流另行产生，不能写回基线Run。历史macOS诊断件（v1/v2/v3单平台Run）保持只读，不因本三平台基线而改变属性。前一Revision `2cc255e`的同场景运行因该Revision常规CI的Windows基准作业失败未归档；本基线在修复Revision重新执行，对应[常规CI 35960330442](https://github.com/carrie1988/Harnessix/actions/runs/35960330442)六实例成功。

## 2. 调用链、环境与数值

调用链为`Soak Runner → AgentRuntime（SoakProvider确定性替身）→ CodingToolRuntime grep → SQLiteArtifactStore.publish/read/collect → Session共库`。`artifact_publish`只计真实发布方法耗时，`artifact_read`按每次分页调用计时；Turn其余部分不包含在样本内。每平台22件全部完成发布、195次分页读取覆盖全部记录，到期清理后正文为0、墓碑与Manifest各22。

| 平台 | Python/档位 | Run ID | 发布P50/P95/P99（ns） | 读取P50/P95/P99（ns） | 峰值RSS（bytes） |
|---|---|---|---:|---:|---:|
| Linux | 3.12.3 / `c4-m16` | `50f1c840e9924794be66721ba4d6f81b` | 16,719,721 / 49,069,968 / 50,031,242 | 13,847,479 / 14,644,908 / 16,448,791 | 82,272,256 |
| macOS | 3.12.10 / `c3-m7` | `5c5dcc062f6c496a93a1849cf62829bb` | 11,250,250 / 29,062,584 / 30,726,459 | 9,311,833 / 10,245,833 / 10,578,125 | 92,831,744 |
| Windows | 3.12.10 / `c4-m16` | `d0f0ab1903554f90b25a5eb58d9231f0` | 82,476,100 / 164,972,300 / 183,639,800 | 16,451,200 / 18,166,600 / 24,464,300 | 79,941,632 |

三个平台端点一致：DB从102,400增至5,062,656字节，Artifact正文水位4,374,662字节，WAL端点为0。近限件单件不超过1 MiB且不低于上限的80%；故障计数全为0。每平台仅20条发布样本，不能据此推断长期尾延迟；三个平台硬件档位不同，不跨平台套用绝对时延阈值。

## 3. 原始证据与独立复核

| 平台 | 原始Run/Attempt | Manifest SHA-256 | 上传ZIP SHA-256 |
|---|---|---|---|
| Linux | [Run](raw/linux/50f1c840e9924794be66721ba4d6f81b/manifest.json) / [Attempt](raw/linux/attempts/50f1c840e9924794be66721ba4d6f81b/FINAL.json) | `f5963a992ffe402543cc5777b460ca7a9f2ef5ca6b39940d54ca5486a974085e` | `cf4639cb2efb4107a64c98eb9ce82b7ea4e243a044a0bf5ad262296a95139062` |
| macOS | [Run](raw/macos/5c5dcc062f6c496a93a1849cf62829bb/manifest.json) / [Attempt](raw/macos/attempts/5c5dcc062f6c496a93a1849cf62829bb/FINAL.json) | `16b95f9cdf008438c36612164c00832a0ca1e500390a3b4e2e2324855f7cfbe4` | `86e4400a770bbe5c4ca1c4197cfcadbb5cfe300259c565d6200b3e0608654dc8` |
| Windows | [Run](raw/windows/d0f0ab1903554f90b25a5eb58d9231f0/manifest.json) / [Attempt](raw/windows/attempts/d0f0ab1903554f90b25a5eb58d9231f0/FINAL.json) | `872cf2a138955a1b0694c08b716c5f1ec8a90a3840830378edcbb68b267569d5` | `35dbd694f1215a3acd84bc6e1fed4173eb12b314e9d145f23087f168a46f5059` |

各平台恰有四份Run文件（`samples.jsonl`、`artifact-proof.json`、`manifest.json`、`COMMITTED.json`）和两份Attempt文件（`STARTED.json`、`FINAL.json`）。下载后通过[`read_published_run`](../../../scripts/soak_evidence.py)和[`read_attempt`](../../../scripts/soak_attempt.py)逐份重读，再用标准库按最近秩重算P50/P95/P99，逐文件重算SHA-256；逐件Proof的大小档、记录数、分页样本索引与原始样本一一对应。常见绝对路径、鉴权Header和密钥模式扫描未发现匹配；这只是针对本白名单证据的检查，不宣称对任意文本的形式化无泄漏证明。

仓库根目录可只读重核Run/Attempt；不会调用模型或更改业务State：

```bash
uv run python - <<'PY'
from pathlib import Path
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run

root = Path('docs/validation/soak-artifact-growth-three-platform-2026-09-24-v1/raw')
for platform, run_id in (
    ('linux', '50f1c840e9924794be66721ba4d6f81b'),
    ('macos', '5c5dcc062f6c496a93a1849cf62829bb'),
    ('windows', 'd0f0ab1903554f90b25a5eb58d9231f0'),
):
    manifest, digest = read_published_run(root / platform / run_id)
    _, final = read_attempt(root / platform / 'attempts' / run_id)
    assert manifest.platform == platform
    assert manifest.load.artifact_count == 22
    assert manifest.sample_counts['artifact_publish'] == 20
    assert not any(manifest.fault_counts.model_dump().values())
    assert final is not None and final.outcome == 'committed'
    assert final.manifest_sha256 == digest
PY
```

## 4. 三平台预冻结Profile与工程阈值

三份Profile均由对应原始Run/Attempt通过[`publish_profile`](../../../scripts/soak_threshold.py)生成并封印，冻结后不可覆盖。`artifact_publish`与`artifact_read`的P50/P95/P99按基线逐项上浮100%（10000bp），`rss_peak`三个分位数上浮50%（5000bp），DB/WAL/Artifact正向增长上浮50%；上限精确等于`ceil(基线×(10000+余量)/10000)`，候选观测不参与计算。Python范围冻结为`3.12.0`～`3.12.99`，`hardware_class`逐平台绑定基线档位。

| 平台 | Profile ID | 发布P99上限（ns） | 读取P99上限（ns） | RSS上限（bytes） | DB/WAL/Artifact增长上限（bytes） |
|---|---|---:|---:|---:|---:|
| Linux | `71671f84684c4f90bf9f93039be67129` | 100,062,484 | 32,897,582 | 123,408,384 | 7,440,384 / 0 / 6,561,993 |
| macOS | `38d3138e83d0486fa579ba16a152e69b` | 61,452,918 | 21,156,250 | 139,247,616 | 7,440,384 / 0 / 6,561,993 |
| Windows | `fd33e868aa834a5faf1776f45ab5b734` | 367,279,600 | 48,928,600 | 119,912,448 | 7,440,384 / 0 / 6,561,993 |

预期故障计数与基线精确一致（全为0）；任何额外取消、超时、EOF、UNKNOWN、重复效果或孤儿均不得PASS。这些护栏只覆盖固定22件混合负载的发布、分页与清理，不声明任意规模Artifact存储的SLO。

## 5. 证据边界与后续门禁

本基线只证明固定Revision、固定负载在三个托管Runner完成且清理语义成立；固定场景门禁关闭还需要第二独立Run在冻结Profile下取得三平台PASS报告，且候选Revision常规CI六实例成功。单平台或缩小负载结果不得充作发布PASS；失败证据与PASS证据同等保留。
