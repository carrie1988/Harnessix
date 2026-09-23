---
doc_type: validation-evidence
status: current
version: 1
code_revision: cf7e6b4dba5357354abcd3822bcb2c1e2215bd7c
owners:
  - core
modules:
  - sdk
  - app_server
  - session
  - documentation
related_adrs:
  - docs/adr/0089-bounded-local-transport-lifecycle.md
  - docs/adr/0092-reproducible-local-soak-and-release-thresholds.md
related_tests:
  - tests/benchmarks/test_run_sdk_soak_candidate.py
  - tests/benchmarks/test_soak_sdk_capacity.py
  - tests/benchmarks/test_soak_threshold.py
  - tests/benchmarks/test_soak_attempt.py
supersedes: []
---

# 0.9.3d SDK容量三平台冻结阈值第二独立Run验证

## 1. 结论与证据身份

在基线和[三份Profile](../soak-sdk-three-platform-2026-09-23-v1/README.md#5-冻结阈值profile与后续复验)已经提交、签封后，Revision `cf7e6b4dba5357354abcd3822bcb2c1e2215bd7c`的[手动候选工作流 35843752933](https://github.com/carrie1988/Harnessix/actions/runs/35843752933)在Linux、macOS、Windows三个独立Job中均成功。每个平台使用**新Run ID和独立临时State**，以相同的64协商容量、1轮预热、3轮正式轮次、20条正常往返执行真实SDK/stdio链；`STARTED v2`在负载前持久绑定本平台冻结Profile ID与SHA-256。候选Run状态保持`unverified`，只有独立阈值复验报告给出`PASS/within_limits`。

上传件下载后分别用Run Reader、Attempt Reader和Report Reader重读；又独立以标准库重算24份文件SHA-256、正式样本数和最近秩分位数，并针对各平台重新执行`verify_and_publish`，三份新报告亦为`PASS`。原始文件与来源见[证据Manifest](bundle-manifest.json)，定量结果和剩余门禁见[评审包](review-packet.json)。对应源码Revision的[常规CI 35843734178](https://github.com/carrie1988/Harnessix/actions/runs/35843734178)六实例全部成功，因此SDK容量固定场景的三平台阈值复验门禁已关闭；不扩展为其他Soak场景或整体发布结论。

**结论边界：SDK容量固定场景的三平台单次候选在预冻结阈值内，不代表0.9.3d整体完成，也不证明终端用户端到端SLO或大量C端用户容量。** Profile采用托管机粗硬件档位，时延余量为基线值的100%，RSS/文件增长余量为50%；这些是回归护栏，不是商业延迟承诺。[工程决策和失败语义](../../changes/m09-3d-sdk-frozen-profile-candidate.md)解释了选值和限制。模型Provider请求数均为0。

## 2. 平台、负载、指标与报告

| 平台/档位 | 候选Run ID | Profile ID | 时延P50/P95/P99（ns） | RSS峰值（bytes） | 报告ID/状态 |
|---|---|---|---:|---:|---|
| Linux `c4-m16` | `49908b03512544838dc220eebd6b7b39` | `e7fc115add194e589d35fb9574bfa5c9` | 1293585 / 1410068 / 1455887 | 66301952 | `777fb150c5f44ccfbf1de09df353678a` / PASS |
| macOS `c3-m7` | `28b84da9f0f247e0a48aca088a51609d` | `167f432a612f4c58b2e0bc7c80aea4a3` | 2625250 / 3124417 / 3168250 | 75120640 | `71a9ff2cdbb245e3a08f735ea43e8e36` / PASS |
| Windows `c4-m16` | `48b70212e1bd43ee842cda638f7f26cb` | `5bcdf40d1baf48279ffa1ba9c7bd46c0` | 2613800 / 2791900 / 3114300 | 64749568 | `51b74eaef8fd48ed8c657684647c690d` / PASS |

各平台均有20条正式`sdk_roundtrip`样本、1条`rss_peak`样本、4次**预期**取消、0次超时/EOF/未知效果/重复效果/孤儿。启动前后的SQLite主文件从0增长到98304字节、WAL端点仍为0；它们只代表测量端点，不代表运行时瞬时磁盘峰值。RSS是客户端与服务端进程各自峰值中的较大者，不是两进程同时驻留内存的总和。三个平台不互相比较绝对性能，只与各自Profile比较。

## 3. 原始Run、Attempt、Report与摘要

| 平台 | 原始Run/预绑定Attempt/独立Report | Run Manifest SHA-256 | GitHub上传件摘要 |
|---|---|---|---|
| Linux | [Run](raw/linux/candidate/49908b03512544838dc220eebd6b7b39/manifest.json) / [STARTED v2](raw/linux/candidate/attempts/49908b03512544838dc220eebd6b7b39/STARTED.json) / [Report](raw/linux/reports/777fb150c5f44ccfbf1de09df353678a/report.json) | `be6574968c89a330cce4683faff06fa9fee5b3607493d7b75600f59099afa11b` | `sha256:705658fb2608e9dd144e2fc5cc628fea219cbd46f516a64d31f48ced921ce042` |
| macOS | [Run](raw/macos/candidate/28b84da9f0f247e0a48aca088a51609d/manifest.json) / [STARTED v2](raw/macos/candidate/attempts/28b84da9f0f247e0a48aca088a51609d/STARTED.json) / [Report](raw/macos/reports/71a9ff2cdbb245e3a08f735ea43e8e36/report.json) | `330ab9df4256b9acdf84520cb3ea8c9f6fbb614d8b0cf5514ff5f69bd4d754fb` | `sha256:46c54e5618fe40b96c51a4f96dc8015c745dadc163c7223fff094a34a7b00922` |
| Windows | [Run](raw/windows/candidate/48b70212e1bd43ee842cda638f7f26cb/manifest.json) / [STARTED v2](raw/windows/candidate/attempts/48b70212e1bd43ee842cda638f7f26cb/STARTED.json) / [Report](raw/windows/reports/51b74eaef8fd48ed8c657684647c690d/report.json) | `9932836e7d362940ffb208967492bd25aeef24f32c78f05a02885c610c0ec16b` | `sha256:0350090871463d62f6026245fbe10a33ed6468e950fad34d24adf02d01d8d103` |

每个平台的8份原件包括Run四文件（`samples.jsonl`、`sdk-proof.json`、`manifest.json`、`COMMITTED.json`）、Attempt两文件（`STARTED.json`、`FINAL.json`）与Report两文件（`report.json`、`SEALED.json`）。[证据Manifest](bundle-manifest.json)列出24份文件的仓库相对路径、字节数与SHA-256；上传件摘要仅标识GitHub归档包，不代替这些原始文件摘要。复制时保留原始字节；[`.gitattributes`](../../../.gitattributes)禁止Git在Windows对原件自动换行，防止规范摘要漂移。

仓库根执行以下只读检查，可再次核对候选、预绑定和报告；无需模型Key或业务Workspace：

```bash
uv run python - <<'PY'
from pathlib import Path
from scripts.soak_attempt import SoakAttemptStartV2, read_attempt
from scripts.soak_evidence import read_published_run
from scripts.soak_threshold import read_profile, read_report

root = Path('docs/validation/soak-sdk-three-platform-candidate-2026-09-23-v1/raw')
baseline = Path('docs/validation/soak-sdk-three-platform-2026-09-23-v1')
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
| 三平台第二独立负载 | 通过 | 三个Job分别成功；均有不同于基线的Run ID和隔离State。 |
| Profile负载前预绑定 | 通过 | STARTED v2早于候选Manifest，ID/SHA与签封Profile、Manifest一致。 |
| 原件、统计及独立报告 | 通过 | 24份原件SHA与Manifest匹配；三份报告Reader及仓库外重新计算均为PASS。 |
| 常规六实例CI | 通过 | 对应Revision的[CI 35843734178](https://github.com/carrie1988/Harnessix/actions/runs/35843734178)六个Job全部成功。 |
| 其余0.9.3d与发布门禁 | 未完成 | 长会话、多Thread、Artifact、Action恢复、重启及0.9.4～0.9.6仍须分别验收。 |

GitHub上传件保留期14日，仓库原始规范字节及Profile/Report各自摘要封印用于长期只读复核。此处已关闭的门禁只属于**固定SDK容量场景在三平台各一次第二Run的工程护栏**；不自动聚合其他场景或宣称1.0商用完成。
