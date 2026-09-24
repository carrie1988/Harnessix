---
doc_type: validation-evidence
status: current
version: 1
code_revision: b3e0d6f731d29b2749aab2827a9ebb302c2e87d6
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
supersedes: []
---

# 0.9.3d Action恢复三平台正式规模基线

## 1. 结论与证据边界

源码Revision `b3e0d6f731d29b2749aab2827a9ebb302c2e87d6`的[手动Action恢复Soak工作流](https://github.com/carrie1988/Harnessix/actions/runs/35961268504)在Linux、macOS和Windows三个独立Job中均成功。每个平台执行固定2轮预热与20轮正式故障矩阵；每轮依次覆盖写效果返回边界UNKNOWN、宿主受控硬退出Owner失效、Audit已提交而Execution Plan缺失、Artifact引用窗口孤儿四类故障，并以一次计时的真实`scan_product_action_recovery`跨Store扫描收尾。UNKNOWN效果只经`reconcile`对账收敛，崩溃Route由新Owner先收敛为`unknown`再对账，全Run重复效果为0。所有平台均有完整Run、`COMMITTED`和Attempt `FINAL(committed)`，原始文件位于本目录的[`raw`](raw)。上传件来源及18份文件摘要见[证据Manifest](bundle-manifest.json)，跨平台核验结果见[评审包](review-packet.json)。

**本目录的证据等级仅为三平台各一次正式规模基线**：`manifest.status=baseline`不等于性能阈值PASS，也不等于0.9.3d整体完成。三平台Profile已按此基线冻结并封存于[`profiles`](profiles)；第二独立候选与PASS报告须由冻结阈值复验工作流另行产生，不能写回基线Run。长会话场景的三平台基线与候选仍未完成。前一Revision `2cc255e`的同场景运行因该Revision常规CI的Windows基准作业失败（Runner失败路径未关闭SQLite句柄）未归档，仅保留为GitHub上传件诊断；本基线在修复Revision重新执行，对应[常规CI 35960330442](https://github.com/carrie1988/Harnessix/actions/runs/35960330442)六实例成功。

## 2. 调用链、环境与数值

调用链为`Soak Runner → TrustedActionRouter（真实计划/审批/执行/对账）→ 受控崩溃子进程（一次真实写效果后os._exit(73)）→ 新Owner runtime_owner → recover_interrupted + reconcile → 计时scan_product_action_recovery → Execution Plan/Audit/Session/Artifact四个真实Store`。`recovery_scan`只计恢复扫描本身的单调时钟耗时；故障构造与对账不包含在该样本内。受控Executor与崩溃子进程是确定性故障替身，产品主链与恢复端口均为当前生产实现；Runner不调用Execute进行恢复。

| 平台 | Python/档位 | Run ID | 扫描P50/P95/P99（ns） | 峰值RSS（bytes） | DB/WAL端点（bytes） |
|---|---|---|---:|---:|---:|
| Linux | 3.12.3 / `c4-m16` | `3d5cd5bf755b4d1692704075fb3655a2` | 27,851,872 / 40,490,374 / 42,864,260 | 63,307,776 | 688,128 / 6,909,304 |
| macOS | 3.12.10 / `c3-m7` | `524244f41bd74feaad2709dac9f42f71` | 24,839,292 / 42,010,792 / 43,862,333 | 71,122,944 | 688,128 / 6,884,584 |
| Windows | 3.12.10 / `c4-m16` | `ba2635503c0549adb416abd7c15168b4` | 42,931,900 / 60,040,800 / 60,479,900 | 65,388,544 | 688,128 / 6,884,584 |

每平台仅有20条正式扫描样本和一条RSS样本，不能据此推断长期尾延迟。三个平台故障计数一致：`unknown_effect=44`（22轮×2类UNKNOWN故障）、`duplicate_effect=0`、`orphan=0`，Owner代际逐轮递增至22。WAL端点包含审计与操作账本增长；SQLite数字是首尾水位，不能代表运行期磁盘峰值。三个平台的硬件档位不同，不跨平台套用绝对时延阈值。

## 3. 原始证据与独立复核

| 平台 | 原始Run/Attempt | Manifest SHA-256 | 上传ZIP SHA-256 |
|---|---|---|---|
| Linux | [Run](raw/linux/3d5cd5bf755b4d1692704075fb3655a2/manifest.json) / [Attempt](raw/linux/attempts/3d5cd5bf755b4d1692704075fb3655a2/FINAL.json) | `53ff3dd86f464ceab64a5f8dea13bc7ff7aa88fc17e3756aa54da55d4196da8f` | `a7d9c315bbe0f0d3f0046a1c527c767605c9e8567cc04a56035ae77e5caf52c7` |
| macOS | [Run](raw/macos/524244f41bd74feaad2709dac9f42f71/manifest.json) / [Attempt](raw/macos/attempts/524244f41bd74feaad2709dac9f42f71/FINAL.json) | `f57aba60af3c0d83ba7f2395a162c5fee59b55597130aa1a448033092b256866` | `51c425f257ad164b8f9ece451a432c0161880998f4e731f7b3945d8340ae9bdb` |
| Windows | [Run](raw/windows/ba2635503c0549adb416abd7c15168b4/manifest.json) / [Attempt](raw/windows/attempts/ba2635503c0549adb416abd7c15168b4/FINAL.json) | `3b8628edced406ad5cffab14584853578bbf32598cffca7d159563851aca79d9` | `aeb2cefe7654681f9ef9c7545a8f16eebbc48d63be3d46dee52039daade3cb7c` |

各平台恰有四份Run文件（`samples.jsonl`、`action-proof.json`、`manifest.json`、`COMMITTED.json`）和两份Attempt文件（`STARTED.json`、`FINAL.json`）。下载后通过[`read_published_run`](../../../scripts/soak_evidence.py)和[`read_attempt`](../../../scripts/soak_attempt.py)逐份重读，再用标准库按最近秩重算P50/P95/P99，逐文件重算SHA-256；22轮Owner代际均为`1..22`严格递增，逐轮扫描Route、修复与孤儿计数与Proof一致。常见绝对路径、鉴权Header和密钥模式扫描未发现匹配；这只是针对本白名单证据的检查，不宣称对任意文本的形式化无泄漏证明。

仓库根目录可只读重核Run/Attempt；不会调用模型或更改业务State：

```bash
uv run python - <<'PY'
from pathlib import Path
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run

root = Path('docs/validation/soak-action-recovery-three-platform-2026-09-24-v1/raw')
for platform, run_id in (
    ('linux', '3d5cd5bf755b4d1692704075fb3655a2'),
    ('macos', '524244f41bd74feaad2709dac9f42f71'),
    ('windows', 'ba2635503c0549adb416abd7c15168b4'),
):
    manifest, digest = read_published_run(root / platform / run_id)
    _, final = read_attempt(root / platform / 'attempts' / run_id)
    assert manifest.platform == platform
    assert manifest.load.fault_matrix_version == 'action-recovery-v1'
    assert manifest.sample_counts == {'recovery_scan': 20, 'rss_peak': 1}
    assert manifest.fault_counts.unknown_effect == 44
    assert manifest.fault_counts.duplicate_effect == 0
    assert final is not None and final.outcome == 'committed'
    assert final.manifest_sha256 == digest
PY
```

## 4. 三平台预冻结Profile与工程阈值

三份Profile均由对应原始Run/Attempt通过[`publish_profile`](../../../scripts/soak_threshold.py)生成并封印，冻结后不可覆盖。`recovery_scan`的P50/P95/P99按基线逐项上浮100%（10000bp），`rss_peak`三个分位数上浮50%（5000bp），DB/WAL/Artifact正向增长上浮50%；上限精确等于`ceil(基线×(10000+余量)/10000)`，候选观测不参与计算。Python范围冻结为`3.12.0`～`3.12.99`，`hardware_class`逐平台绑定基线档位；托管机档位漂移只能判`unverified`，不能跨平台借用阈值。

| 平台 | Profile ID | 扫描P99上限（ns） | RSS上限（bytes） | DB/WAL增长上限（bytes） |
|---|---|---:|---:|---:|
| Linux | `2eb7187e47f34246ab6ed25211a244ab` | 85,728,520 | 94,961,664 | 866,304 / 10,122,840 |
| macOS | `9a21d033db4f4eee8730090509aaba47` | 87,724,666 | 106,684,416 | 866,304 / 10,085,760 |
| Windows | `ab14d36e18384cefa1d83de8063a4408` | 120,959,800 | 98,082,816 | 866,304 / 10,085,760 |

预期故障计数与基线精确一致：`unknown_effect=44`、`duplicate_effect=0`、`orphan=0`，其余为0；额外超时、取消、EOF、重复效果或未预期孤儿均不得PASS。这些护栏只覆盖固定故障矩阵的恢复扫描与当前固定负载，不声明任意外部系统的恢复SLO。

## 5. 证据边界与后续门禁

本基线只证明固定Revision、固定故障矩阵在三个托管Runner完成且恢复语义成立；固定场景门禁关闭还需要第二独立Run在冻结Profile下取得三平台PASS报告，且候选Revision常规CI六实例成功。单平台或缩小负载结果不得充作发布PASS；失败证据与PASS证据同等保留。
