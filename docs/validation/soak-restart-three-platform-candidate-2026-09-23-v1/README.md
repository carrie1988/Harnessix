---
doc_type: validation-evidence
status: current
version: 1
code_revision: e81ebada78f67f4c447e4ae0089ec143ccd6cf34
owners:
  - core
modules:
  - product_config
  - app_server
  - sdk
  - session
  - trusted_actions
  - documentation
related_adrs:
  - docs/adr/0089-bounded-local-transport-lifecycle.md
  - docs/adr/0091-action-runtime-fencing-and-bounded-reconciliation.md
  - docs/adr/0092-reproducible-local-soak-and-release-thresholds.md
related_tests:
  - tests/benchmarks/test_run_restart_soak_candidate.py
  - tests/benchmarks/test_soak_restart.py
  - tests/benchmarks/test_soak_restart_proof.py
  - tests/benchmarks/test_soak_threshold.py
supersedes: []
---

# 0.9.3d完整产品重启三平台冻结阈值第二独立Run验证

## 1. 结论与证据身份

在[三平台正式基线和封印Profile](../soak-restart-three-platform-2026-09-23-v1/README.md)已经提交后，Revision `e81ebada78f67f4c447e4ae0089ec143ccd6cf34`的[手动候选工作流 35867540361](https://github.com/carrie1988/Harnessix/actions/runs/35867540361)在Linux、macOS、Windows三个独立Job中均成功。每个平台使用**新Run ID和独立临时State**，以相同的500 Thread、一次预热、一次受控硬退出、三次正式新进程启动执行真实完整产品组合根；`STARTED v2`在任何负载前持久绑定本平台冻结Profile ID和SHA-256。候选Run状态仍为`unverified`，只有独立阈值复验报告给出`PASS/within_limits`。

上传件下载后由Run Reader、Attempt Reader、Report Reader分别重读，并用标准库重算24份原始文件SHA-256、正式样本数及最近秩分位数；对每个平台再次调用`verify_and_publish`独立生成新报告，三份结论与原报告同为`PASS`。原始文件及GitHub上传件来源见[证据Manifest](bundle-manifest.json)，定量结果和剩余门禁见[评审包](review-packet.json)。对应源码的[常规CI 35867509424](https://github.com/carrie1988/Harnessix/actions/runs/35867509424)首次文档Job因Mermaid冷启动超时失败，其余五Job成功；仅重跑失败文档Job后，六Job最终全部成功。首次超时仍保留为环境抖动诊断，不把重跑通过当成排他根因证明；本归档不宣称完整0.9.3d发布验收。

**结论边界：固定完整产品重启场景的三平台单次候选在预冻结阈值内，不代表0.9.3d整体完成，也不证明真实模型、外部Action副作用或大量C端用户容量。** 三个平台仅各有三条正式启动样本；Profile是宽松性能回归护栏，不是商业SLO。`action_recovery`固定故障矩阵及其余场景仍需分别验收。模型Provider请求数均为0。

## 2. 平台、负载、指标与报告

| 平台/档位 | 候选Run ID | Profile ID | 启动P50/P95/P99（ns） | RSS峰值（bytes） | 报告ID/状态 |
|---|---|---|---:|---:|---|
| Linux `c4-m16` | `66772f5e6db84d479315898b4c4c9444` | `33e970328c664aac8dcbd63f329b33e1` | 2,678,184,742 / 2,686,291,295 / 2,686,291,295 | 100,937,728 | `eb830d67d28f4bb7b8691a776c34ed05` / PASS |
| macOS `c3-m7` | `bba25f244dc043e78b950c492f147aef` | `f002516ffd0f4c309f1e0f6cadcde49e` | 4,439,189,292 / 7,322,346,958 / 7,322,346,958 | 107,954,176 | `cff880f179cc45a79ab6b8649dacc19d` / PASS |
| Windows `c4-m16` | `c4e5fbeb3ecc410fa83b260bd1cff8fb` | `87d7b6a0e45c4bdebb02eedef21c2f83` | 5,606,725,100 / 5,840,673,600 / 5,840,673,600 | 105,148,416 | `c61d424d1c874d4d8ba8ffaba1deb3ca` / PASS |

各平台均有三条正式`product_startup`样本、一条`rss_peak`样本、一次**预期**EOF；超时、UNKNOWN、重复效果、孤儿均0。完整Thread集合摘要在五个周期内不漂移，Owner代际依次为`1～5`。SQLite主文件端点依次为Linux 1,277,952、macOS 1,376,256、Windows 1,376,256 bytes，WAL与Artifact端点均为0；这些只表示首尾水位。RSS是Runner与正常退出产品子进程各自峰值的较大者，不是同时驻留总量。不同平台只与各自Profile比较，不互相比绝对时延。

## 3. 原始Run、Attempt、Report与摘要

| 平台 | 原始Run/预绑定Attempt/独立Report | Run Manifest SHA-256 | GitHub上传件SHA-256 |
|---|---|---|---|
| Linux | [Run](raw/linux/candidate/66772f5e6db84d479315898b4c4c9444/manifest.json) / [STARTED v2](raw/linux/candidate/attempts/66772f5e6db84d479315898b4c4c9444/STARTED.json) / [Report](raw/linux/reports/eb830d67d28f4bb7b8691a776c34ed05/report.json) | `25552f3478121900afc1a4b35a0f26dc341098bb818244ecfd322b3d491f2ab9` | `f38241fdcf678e89ba55ac4184328016f36ab72dc8580238446f62d1d912dad5` |
| macOS | [Run](raw/macos/candidate/bba25f244dc043e78b950c492f147aef/manifest.json) / [STARTED v2](raw/macos/candidate/attempts/bba25f244dc043e78b950c492f147aef/STARTED.json) / [Report](raw/macos/reports/cff880f179cc45a79ab6b8649dacc19d/report.json) | `a657f1297bad1c9df231e648bb3254a9e18d7ce544ea06d67fbbfbfad776dfa9` | `ba68dc9e2572b79395d8e68c5bbe104239143ce22de5f5463b725c52cd0f1896` |
| Windows | [Run](raw/windows/candidate/c4e5fbeb3ecc410fa83b260bd1cff8fb/manifest.json) / [STARTED v2](raw/windows/candidate/attempts/c4e5fbeb3ecc410fa83b260bd1cff8fb/STARTED.json) / [Report](raw/windows/reports/c61d424d1c874d4d8ba8ffaba1deb3ca/report.json) | `42f98b19ab22fbdae42394c58b2ddd110e05ef8cc35baa1c3daf2809e413b713` | `cabba2bba220225d4cb3258561fbccac176a68efee730d193b05cb29588979c3` |

每平台八份规范原件：Run四文件、Attempt两文件、Report两文件。[证据Manifest](bundle-manifest.json)逐份列出路径、字节数和SHA-256；上传ZIP摘要标识原始GitHub下载包，但不能代替逐文件核验。[`.gitattributes`](../../../.gitattributes)对本目录`raw/**`设置`-text`，避免Windows Checkout改写规范JSON字节。下载件仅扫描低敏证据白名单中的常见宿主路径、鉴权Header和密钥模式；未发现匹配并不代表对任意文本的形式化无泄漏证明。

仓库根可只读重核候选、预绑定和报告；不调用模型或修改业务State：

```bash
uv run python - <<'PY'
from pathlib import Path
from scripts.soak_attempt import SoakAttemptStartV2, read_attempt
from scripts.soak_evidence import read_published_run
from scripts.soak_threshold import read_profile, read_report

root = Path('docs/validation/soak-restart-three-platform-candidate-2026-09-23-v1/raw')
baseline = Path('docs/validation/soak-restart-three-platform-2026-09-23-v1')
for platform in ('linux', 'macos', 'windows'):
    runs = tuple(p for p in (root / platform / 'candidate').iterdir() if p.is_dir() and p.name != 'attempts')
    reports = tuple((root / platform / 'reports').iterdir())
    profiles = tuple((baseline / 'profiles' / platform).iterdir())
    assert len(runs) == len(reports) == len(profiles) == 1
    run, digest = read_published_run(runs[0])
    started, final = read_attempt(root / platform / 'candidate' / 'attempts' / run.run_id)
    profile, profile_sha = read_profile(profiles[0])
    report = read_report(reports[0])
    assert isinstance(started, SoakAttemptStartV2)
    assert started.threshold_profile_ref == run.threshold_profile_ref
    assert run.threshold_profile_ref.profile_id == profile.profile_id
    assert run.threshold_profile_ref.sha256 == profile_sha
    assert final is not None and final.outcome == 'committed' and final.manifest_sha256 == digest
    assert report.status == 'PASS' and report.candidate_manifest_sha256 == digest
PY
```

## 4. 评审结论、失败边界与剩余门禁

| 检查 | 结论 | 边界 |
|---|---|---|
| 三平台第二独立负载 | 通过 | 三个Job成功；各有不同于基线的Run ID和隔离State。 |
| Profile负载前预绑定 | 通过 | STARTED v2早于候选Manifest；ID/SHA与封印Profile、Manifest一致。 |
| 原件、统计及独立报告 | 通过 | 24份原件SHA、三平台最近秩分位和独立重算报告均为PASS。 |
| 常规六实例CI | 通过，保留首次超时诊断 | 对应Revision的[CI 35867509424](https://github.com/carrie1988/Harnessix/actions/runs/35867509424)首次五Job成功、文档Job超时失败；只重跑失败Job后六Job全部成功。 |
| 其余0.9.3d与发布门禁 | 未完成 | Action恢复、长会话、多Thread、Artifact三平台正式验收及0.9.4～0.9.6仍需推进。 |

首次候选Revision `64663f2`的[工作流 35867100728](https://github.com/carrie1988/Harnessix/actions/runs/35867100728)仅Linux和macOS成功，Windows因Git换行转换导致Profile规范字节损坏而在负载前失败；修复Revision `e81ebad`用`-text`保护基线及候选原件，并在隔离`core.autocrlf=true` Checkout复核SHA恢复一致。本目录只归档**修复后新工作流**的三份候选原件，不把首次两平台结果并入正式结论。

GitHub上传件保留期14日；本目录保存原始规范字节和文件摘要供长期复核。这里只关闭固定重启场景的三平台单次工程阈值候选，不自动聚合其他场景或宣称1.0商用完成。设计、限制及故障语义见[冻结阈值详细设计](../../changes/m09-3d-product-restart-frozen-profile-candidate.md)。
