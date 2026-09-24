---
doc_type: validation-evidence
status: current
version: 1
code_revision: 65cbcc5caab2ba167763844e974c9cd7c036ed0d
owners:
  - core
modules:
  - trusted_actions
  - execution
  - session
  - artifacts
  - product_config
  - documentation
related_adrs:
  - docs/adr/0091-action-runtime-fencing-and-bounded-reconciliation.md
  - docs/adr/0092-reproducible-local-soak-and-release-thresholds.md
related_tests:
  - tests/benchmarks/test_soak_action_recovery.py
  - tests/benchmarks/test_soak_action_proof.py
  - tests/benchmarks/test_run_action_recovery_soak_candidate.py
supersedes: []
---

# 0.9.3d Action恢复三平台冻结阈值第二独立Run验证

## 1. 结论与证据身份

在[三平台正式基线和封印Profile](../soak-action-recovery-three-platform-2026-09-24-v1/README.md)已经提交后，Revision `65cbcc5caab2ba167763844e974c9cd7c036ed0d`的[手动候选工作流 35961896276](https://github.com/carrie1988/Harnessix/actions/runs/35961896276)在Linux、macOS、Windows三个独立Job中均成功。每个平台使用**新Run ID和独立临时State**，以相同的2轮预热与20轮正式固定四故障矩阵执行真实Trusted Action主链；`STARTED v2`在任何负载前持久绑定本平台冻结Profile ID和SHA-256。候选Run状态仍为`unverified`，只有独立阈值复验报告给出`PASS/within_limits`。

上传件下载后由Run Reader、Attempt Reader、Report Reader分别重读，并用标准库重算24份原始文件SHA-256、正式样本数及最近秩分位数；对每个平台再次调用`verify_and_publish`独立生成新报告，三份结论与原报告同为`PASS`。原始文件及GitHub上传件来源见[证据Manifest](bundle-manifest.json)，定量结果和剩余门禁见[评审包](review-packet.json)。对应源码的[常规CI 35961875693](https://github.com/carrie1988/Harnessix/actions/runs/35961875693)六实例全部成功。

**结论边界：固定Action恢复故障矩阵场景的三平台单次候选在预冻结阈值内，本场景工程护栏通过；不代表0.9.3d整体完成，也不证明任意外部系统的恢复SLO。** 三个平台仅各有20条正式扫描样本；Profile是宽松性能回归护栏，不是商业SLO。长会话与Artifact增长场景仍需分别验收。模型Provider请求数均为0。

## 2. 平台、负载、指标与报告

| 平台/档位 | 候选Run ID | Profile ID | 扫描P50/P95/P99（ns） | RSS峰值（bytes） | 报告ID/状态 |
|---|---|---|---:|---:|---|
| Linux `c4-m16` | `9ffa64a33b3444cbbc5f4e05d5be26ac` | `2eb7187e47f34246ab6ed25211a244ab` | 10,593,445 / 16,202,029 / 16,939,377 | 63,873,024 | `463f4abeffd14731bb4c48ba14b679d4` / PASS |
| macOS `c3-m7` | `642c3a99e9614942b0da902a4f1beadf` | `9a21d033db4f4eee8730090509aaba47` | 17,655,583 / 42,193,334 / 59,016,875 | 70,959,104 | `2d32136c63134102b3dd708ab5e14538` / PASS |
| Windows `c4-m16` | `78bd5a41a7f34fec87a9e3500c37551a` | `ab14d36e18384cefa1d83de8063a4408` | 38,183,800 / 56,264,000 / 58,963,700 | 67,129,344 | `f1c131fc3dcc49a99a1dd04e089808be` / PASS |

各平台均有20条正式`recovery_scan`样本、一条`rss_peak`样本和44次**预期**UNKNOWN；取消、超时、EOF、重复效果、孤儿均0。22轮Owner代际严格递增，每轮四类故障的对账计数与v7 Proof一致。不同平台只与各自Profile比较，不互相比绝对时延。

## 3. 原始Run、Attempt、Report与摘要

| 平台 | 原始Run/预绑定Attempt/独立Report | Run Manifest SHA-256 | GitHub上传件SHA-256 |
|---|---|---|---|
| Linux | [Run](raw/linux/candidate/9ffa64a33b3444cbbc5f4e05d5be26ac/manifest.json) / [STARTED v2](raw/linux/candidate/attempts/9ffa64a33b3444cbbc5f4e05d5be26ac/STARTED.json) / [Report](raw/linux/reports/463f4abeffd14731bb4c48ba14b679d4/report.json) | `5b3c84e3bc2d4551e2cf07fa82064b3ba1027c319c7e25676e431b46e753759a` | `62af0b51297fa147775b1485b63794e61183bbb8efe7b94a1d4f3bddea4a2b9e` |
| macOS | [Run](raw/macos/candidate/642c3a99e9614942b0da902a4f1beadf/manifest.json) / [STARTED v2](raw/macos/candidate/attempts/642c3a99e9614942b0da902a4f1beadf/STARTED.json) / [Report](raw/macos/reports/2d32136c63134102b3dd708ab5e14538/report.json) | `454d5ade75b64f6fdc0d505f6104b4a183fc036e4626b731dcc220e9115600ed` | `65ca5d92eec5aafc95bef095871bf99ad8809781db93540f7a931a61e8854d4d` |
| Windows | [Run](raw/windows/candidate/78bd5a41a7f34fec87a9e3500c37551a/manifest.json) / [STARTED v2](raw/windows/candidate/attempts/78bd5a41a7f34fec87a9e3500c37551a/STARTED.json) / [Report](raw/windows/reports/f1c131fc3dcc49a99a1dd04e089808be/report.json) | `537389a309cafa54a6d22caa9059b976f8a5175d5943f7d31ef2d80b49e99589` | `6ec41beed2beedfde8cf560c80bab1c999fbe4cd71ee2afc071d25ef11b2fe63` |

每平台八份规范原件：Run四文件、Attempt两文件、Report两文件。[证据Manifest](bundle-manifest.json)逐份列出路径、字节数和SHA-256；上传ZIP摘要标识原始GitHub下载包，但不能代替逐文件核验。[`.gitattributes`](../../../.gitattributes)对本目录`raw/**`设置`-text`，避免Windows Checkout改写规范JSON字节。下载件仅扫描低敏证据白名单中的常见宿主路径、鉴权Header和密钥模式；未发现匹配并不代表对任意文本的形式化无泄漏证明。

仓库根可只读重核候选、预绑定和报告；不调用模型或修改业务State：

```bash
uv run python - <<'PY'
from pathlib import Path
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run
from scripts.soak_threshold import read_report

root = Path('docs/validation/soak-action-recovery-three-platform-candidate-2026-09-24-v1/raw')
for platform, run_id, report_id in (
    ('linux', '9ffa64a33b3444cbbc5f4e05d5be26ac', '463f4abeffd14731bb4c48ba14b679d4'),
    ('macos', '642c3a99e9614942b0da902a4f1beadf', '2d32136c63134102b3dd708ab5e14538'),
    ('windows', '78bd5a41a7f34fec87a9e3500c37551a', 'f1c131fc3dcc49a99a1dd04e089808be'),
):
    manifest, digest = read_published_run(root / platform / 'candidate' / run_id)
    started, final = read_attempt(root / platform / 'candidate' / 'attempts' / run_id)
    report = read_report(root / platform / 'reports' / report_id)
    assert manifest.platform == platform and manifest.status == 'unverified'
    assert manifest.threshold_profile_ref is not None
    assert started.threshold_profile_ref == manifest.threshold_profile_ref
    assert final is not None and final.outcome == 'committed'
    assert final.manifest_sha256 == digest
    assert report.status == 'PASS' and report.candidate_manifest_sha256 == digest
PY
```

## 4. 证据边界与后续门禁

本归档只关闭固定Action恢复故障矩阵场景的工程护栏：基线、冻结Profile与第二独立Run均在真实产品主链上完成，恢复语义（UNKNOWN只对账、宿主中断收敛、零重复效果、跨Store扫描计数）在全部三个平台成立。0.9.3d其余门禁（长会话与Artifact增长候选、总体发布评审）不由此归档关闭；失败证据与PASS证据同等保留。
