---
doc_type: validation-evidence
status: current
version: 1
code_revision: 39dda2bb218baed30dcf9b1326f899a3133f497b
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

# 0.9.3d Artifact增长三平台冻结阈值第二独立Run验证

## 1. 结论与证据身份

在[300件负载三平台v3基线和封印Profile](../soak-artifact-growth-three-platform-2026-09-24-v3/README.md)已经提交后，Revision `39dda2bb218baed30dcf9b1326f899a3133f497b`的[手动候选工作流 36015746449](https://github.com/carrie1988/Harnessix/actions/runs/36015746449)在Linux、macOS、Windows三个独立Job中均成功。每个平台使用**新Run ID和独立临时State**，以相同的2件预热与300件正式混合大小件执行真实Agent/Tool/Artifact主链；`STARTED v2`在任何负载前持久绑定本平台冻结Profile ID和SHA-256。候选Run状态仍为`unverified`，只有独立阈值复验报告给出`PASS/within_limits`。

上传件下载后由Run Reader、Attempt Reader、Report Reader分别重读，并用标准库重算24份原始文件SHA-256；对每个平台再次调用`verify_and_publish`独立生成新报告，三份结论与原报告同为`PASS`。原始文件及GitHub上传件来源见[证据Manifest](bundle-manifest.json)，定量结果和剩余门禁见[评审包](review-packet.json)。对应源码的[常规CI 36015746312](https://github.com/carrie1988/Harnessix/actions/runs/36015746312)六实例全部成功。

**结论边界：固定Artifact增长场景（300件负载）的三平台单次候选在预冻结阈值内，本场景工程护栏通过；不代表0.9.3d整体完成，也不证明任意规模Artifact存储的SLO。** 前两轮候选FAIL（[20件](../soak-artifact-growth-three-platform-candidate-2026-09-24-v1/README.md)、[60件](../soak-artifact-growth-three-platform-candidate-2026-09-24-v2/README.md)）与全部旧证据保持只读，不被本PASS改写。模型Provider请求数均为0。

## 2. 平台、负载、指标与报告

| 平台/档位 | 候选Run ID | Profile ID | 发布P50/P95/P99（ns） | 读取P99（ns） | 报告ID/状态 |
|---|---|---|---:|---:|---|
| Linux `c4-m16` | `d2d4117a`（完整ID见[证据Manifest](bundle-manifest.json)） | `70a57857f5e04d189f64ebb65473f6bd` | 118,571,006 / 356,792,125 / 412,586,533 | 81,019,184 | `fe3cae06`（完整ID见证据Manifest） / PASS |
| macOS `c3-m7` | `0be33e77` | `a59713b864dd4064851658b03ca886f8` | 130,002,125 / 267,809,625 / 285,553,167 | 65,132,167 | `4eb75d43` / PASS |
| Windows `c4-m16` | `ce55a8f5` | `6a58fce7813e4593b2af9d25c9b8add4` | 366,014,400 / 947,701,100 / 985,078,100 | 127,525,300 | `f7051682` / PASS |

各平台均有300条正式发布样本、2925条正式读取样本和一条RSS样本；取消、超时、EOF、UNKNOWN、重复效果、孤儿均0。302件发布、全部分页与分批到期清理语义在全部平台成立。不同平台只与各自Profile比较，不互比绝对时延。

## 3. 原始Run、Attempt、Report与摘要

每平台八份规范原件：Run四文件、Attempt两文件、Report两文件，位于`raw/<platform>/candidate`与`raw/<platform>/reports`。[证据Manifest](bundle-manifest.json)逐份列出路径、字节数和SHA-256；上传ZIP摘要标识原始GitHub下载包，但不能代替逐文件核验。[`.gitattributes`](../../../.gitattributes)对本目录`raw/**`设置`-text`，避免Windows Checkout改写规范JSON字节。下载件仅扫描低敏证据白名单中的常见宿主路径、鉴权Header和密钥模式；未发现匹配并不代表对任意文本的形式化无泄漏证明。

仓库根可只读重核候选、预绑定和报告；不调用模型或修改业务State：

```bash
uv run python - <<'PY'
from pathlib import Path
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run
from scripts.soak_threshold import read_report

root = Path('docs/validation/soak-artifact-growth-three-platform-candidate-2026-09-24-v3/raw')
for platform in ('linux', 'macos', 'windows'):
    base = root / platform
    (run_id,) = [p.name for p in (base / 'candidate').iterdir() if p.is_dir() and p.name != 'attempts']
    (report_id,) = [p.name for p in (base / 'reports').iterdir()]
    manifest, digest = read_published_run(base / 'candidate' / run_id)
    started, final = read_attempt(base / 'candidate' / 'attempts' / run_id)
    report = read_report(base / 'reports' / report_id)
    assert manifest.platform == platform and manifest.status == 'unverified'
    assert manifest.threshold_profile_ref is not None
    assert started.threshold_profile_ref == manifest.threshold_profile_ref
    assert final is not None and final.outcome == 'committed'
    assert final.manifest_sha256 == digest
    assert report.status == 'PASS' and report.candidate_manifest_sha256 == digest
PY
```

## 4. 证据边界与后续门禁

本归档关闭固定Artifact增长场景（300件负载）的工程护栏：基线、冻结Profile与第二独立Run均在真实产品主链上完成，发布、分页、到期清理与引用完整性语义在全部三个平台成立，且历史Artifact批量验证修复后300件规模不再触发`context_artifact_timeout`。0.9.3d其余门禁（长会话候选、总体发布评审）不由此归档关闭；失败证据与PASS证据同等保留。
