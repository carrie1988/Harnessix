---
doc_type: validation-evidence
status: current
version: 1
code_revision: a2245f568ff3cab71c63e97869e66c93a8ebc529
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

# 0.9.3d 长会话Context/Compaction三平台冻结阈值第二独立Run验证

## 1. 结论与证据身份

在[三平台正式基线和封印Profile](../soak-long-session-three-platform-2026-09-24-v1/README.md)已经提交后，Revision `a2245f568ff3cab71c63e97869e66c93a8ebc529`的[手动候选工作流 36101668671](https://github.com/carrie1988/Harnessix/actions/runs/36101668671)在Linux、macOS、Windows三个独立Job中均成功。每个平台使用**新Run ID和独立临时State**，以相同的1000 Turn、5次预热与真实Context/Compaction执行；`STARTED v2`在任何负载前持久绑定本平台冻结Profile ID和SHA-256。候选Run状态仍为`unverified`，只有独立阈值复验报告给出`PASS/within_limits`。

上传件下载后由Run Reader、Attempt Reader、Report Reader分别重读，并用标准库重算24份原始文件SHA-256；对每个平台再次调用`verify_and_publish`独立生成新报告，三份结论与原报告同为`PASS`。原始文件及GitHub上传件来源见[证据Manifest](bundle-manifest.json)，定量结果和剩余门禁见[评审包](review-packet.json)。首轮候选的Windows Job在300分钟工作流期限被取消（不产生Run/Attempt），本次为480分钟期限下的完整重跑；取消事实保留在本说明，不形成任何PASS依据。

**结论边界：固定长会话Context/Compaction场景（1000 Turn）的三平台单次候选在预冻结阈值内，本场景工程护栏通过；不代表0.9.3d整体完成，也不证明真实Provider时延或大量C端用户容量。** 三个平台各1000条正式Turn样本；Profile是宽松性能回归护栏，不是商业SLO。模型Provider请求由确定性替身产生，不涉及真实模型。

## 2. 平台、负载、指标与报告

| 平台/档位 | 候选Run ID | Profile ID | Turn P50/P95/P99（ns） | RSS峰值（bytes） | 报告ID/状态 |
|---|---|---|---:|---:|---|
| Linux `c4-m16` | `633b321b`（完整ID见[证据Manifest](bundle-manifest.json)） | `ddf402fdfa7942858ef73672e8d8e7ec` | 4,256,321,480 / 10,893,292,505 / 12,265,436,820 | 264,126,464 | `fc3c7bb2`（完整ID见证据Manifest） / PASS |
| macOS `c3-m7` | `dd19437f` | `bd9d171ff98c4352b53f85ff0983b19c` | 3,089,335,355 / 8,046,618,721 / 11,212,638,417 | 411,001,856 | `63370d7f` / PASS |
| Windows `c4-m16` | `b57e3fad` | `bc65dc97d9ee4663904cfc138747d728` | 14,352,529,900 / 33,151,157,300 / 35,393,061,900 | 233,312,256 | `e5ef1a37` / PASS |

各平台均为1000条正式`turn_local`样本、199次摘要请求和一条`rss_peak`样本；取消、超时、EOF、UNKNOWN、重复效果、孤儿均0。逐Turn事件Proof与Replay一致性在全部平台成立。不同平台只与各自Profile比较，不互比绝对时延。

## 3. 原始Run、Attempt、Report与摘要

每平台八份规范原件：Run四文件、Attempt两文件、Report两文件，位于`raw/<platform>/candidate`与`raw/<platform>/reports`。[证据Manifest](bundle-manifest.json)逐份列出路径、字节数和SHA-256；上传ZIP摘要标识原始GitHub下载包，但不能代替逐文件核验。[`.gitattributes`](../../../.gitattributes)对本目录`raw/**`设置`-text`，避免Windows Checkout改写规范JSON字节。下载件仅扫描低敏证据白名单中的常见宿主路径、鉴权Header和密钥模式；未发现匹配并不代表对任意文本的形式化无泄漏证明。

仓库根可只读重核候选、预绑定和报告；不调用模型或修改业务State：

```bash
uv run python - <<'PY'
from pathlib import Path
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run
from scripts.soak_threshold import read_report

root = Path('docs/validation/soak-long-session-three-platform-candidate-2026-09-24-v1/raw')
for platform in ('linux', 'macos', 'windows'):
    base = root / platform
    (run_id,) = [p.name for p in (base / 'candidate').iterdir() if p.is_dir() and p.name != 'attempts']
    (report_id,) = [p.name for p in (base / 'reports').iterdir()]
    manifest, digest = read_published_run(base / 'candidate' / run_id)
    started, final = read_attempt(base / 'candidate' / 'attempts' / run_id)
    report = read_report(base / 'reports' / report_id)
    assert manifest.platform == platform and manifest.status == 'unverified'
    assert manifest.sample_counts['turn_local'] == 1000
    assert manifest.threshold_profile_ref is not None
    assert started.threshold_profile_ref == manifest.threshold_profile_ref
    assert final is not None and final.outcome == 'committed'
    assert final.manifest_sha256 == digest
    assert report.status == 'PASS' and report.candidate_manifest_sha256 == digest
PY
```

## 4. 证据边界与后续门禁

本归档关闭固定长会话Context/Compaction场景（1000 Turn）的工程护栏：基线、冻结Profile与第二独立Run均在真实Agent/Session/Context主链上完成，逐Turn事件Proof、压缩账本与Replay一致性在全部三个平台成立。至此0.9.3d全部六个场景均已完成三平台基线、冻结Profile与第二独立PASS；0.9.3d总体关闭与ADR 0092评审结论由总体详设与路线图另行登记，本归档不单独构成1.0发布判定。
