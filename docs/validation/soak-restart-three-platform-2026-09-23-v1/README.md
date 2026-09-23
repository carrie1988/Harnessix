---
doc_type: validation-evidence
status: current
version: 2
code_revision: 172b1ee96a89e981a6332b16f60b86e2db654df1
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
  - tests/benchmarks/test_soak_restart.py
  - tests/benchmarks/test_soak_restart_child.py
  - tests/benchmarks/test_soak_restart_proof.py
supersedes: []
---

# 0.9.3d完整产品重启三平台正式规模基线

## 1. 结论与证据边界

源码Revision `172b1ee96a89e981a6332b16f60b86e2db654df1`的[手动产品重启Soak工作流](https://github.com/carrie1988/Harnessix/actions/runs/35864710532)在Linux、macOS和Windows三个独立Job中均成功。每个平台执行固定500 Thread、一次空State预热、一次有持久State的受控硬退出、三次新进程正式启动；五个周期均经过真实`run_product_stdio`组合根、SDK与Agent Protocol，重启后核对完整Thread集合、持久恢复扫描/报告及递增的Owner Fence。所有平台均有完整Run、`COMMITTED`和Attempt `FINAL(committed)`，原始文件位于本目录的[`raw`](raw)。上传件来源及18份文件摘要见[证据Manifest](bundle-manifest.json)，跨平台核验结果见[评审包](review-packet.json)。

**证据等级仅为三平台各一次正式规模基线**：`manifest.status=baseline`不等于性能阈值PASS，也不等于0.9.3d整体完成。三平台Profile虽已按此基线冻结，但尚未运行第二独立候选或完成`action_recovery`故障矩阵。该无Turn场景不产生真实Action效果；故障计数中的`duplicate_effect=0`不能代替Action恢复验证。

## 2. 调用链、环境与数值

调用链为`Soak Runner → AgentClient → SubprocessAgentTransport → run_product_stdio → Agent Runtime/Trusted Action恢复扫描 → 六个产品SQLite文件`。`product_startup`从新子进程的`initialize()`开始计时到协议握手完成；完整列表、持久报告和正常关闭另行核对，不包含在该计时样本内。Runner只执行Initialize/Create/List，不启动Turn；固定假凭据与保留`.test`端点，不调用模型。

| 平台 | Python/档位 | Run ID | 启动P50/P95/P99（ns） | 峰值RSS（bytes） | DB端点（bytes） |
|---|---|---|---:|---:|---:|
| Linux | 3.12.3 / `c4-m16` | `0682a0a8a8a64983903ea69523ecfacd` | 2,601,170,547 / 2,603,270,230 / 2,603,270,230 | 100,696,064 | 1,277,952 |
| macOS | 3.12.10 / `c3-m7` | `2b0be5e4777246f9bef38b216fbe7710` | 3,964,092,000 / 6,157,934,375 / 6,157,934,375 | 108,478,464 | 1,380,352 |
| Windows | 3.12.10 / `c4-m16` | `6857f23f7002484b98d71742cf5df204` | 5,968,719,100 / 6,005,836,500 / 6,005,836,500 | 104,738,816 | 1,376,256 |

每平台仅有三条正式启动样本和一条RSS样本，不能据此推断长期尾延迟或大量真实C端用户容量。RSS是Runner和正常退出子进程各自峰值的较大者，不是同时驻留的总和；受控硬退出子进程无正常退出RSS，不填零。Linux使用`/proc/self/status`的KiB换算，macOS使用`getrusage`的bytes，Windows使用`GetProcessMemoryInfo`的bytes。所有WAL和Artifact端点均为0；SQLite数字是首尾水位，不能代表运行期磁盘峰值。三个平台的硬件档位不同，不跨平台套用绝对时延阈值。

## 3. 原始证据与独立复核

| 平台 | 原始Run/Attempt | Manifest SHA-256 | 上传ZIP SHA-256 |
|---|---|---|---|
| Linux | [Run](raw/linux/0682a0a8a8a64983903ea69523ecfacd/manifest.json) / [Attempt](raw/linux/attempts/0682a0a8a8a64983903ea69523ecfacd/FINAL.json) | `c433ad02d3b325546321101fe94dc7c62cdf82b466b3f2f4bba807427c375829` | `7700a0366ecbd9509e51c2e2eb3eb6e8de9e737131541b34bcd23eaf3026ff91` |
| macOS | [Run](raw/macos/2b0be5e4777246f9bef38b216fbe7710/manifest.json) / [Attempt](raw/macos/attempts/2b0be5e4777246f9bef38b216fbe7710/FINAL.json) | `05f7cc83026b270e3df307ce6b024a7bf280d00522d8cff15b640732081548c0` | `1cb271851827d627e0c81bc0c67721b7a591e0ac4a879d8583081f339319760f` |
| Windows | [Run](raw/windows/6857f23f7002484b98d71742cf5df204/manifest.json) / [Attempt](raw/windows/attempts/6857f23f7002484b98d71742cf5df204/FINAL.json) | `dda784fd50aebfe53d9c8cda9c1bf646541bfc7042335d46e55a285dfd489be9` | `be6acebdb74505c30974ca17dc3f2ff4e8c764737e3eaab20c9cee9cfadd86cf` |

各平台恰有四份Run文件（`samples.jsonl`、`restart-proof.json`、`manifest.json`、`COMMITTED.json`）和两份Attempt文件（`STARTED.json`、`FINAL.json`）。下载后通过[`read_published_run`](../../../scripts/soak_evidence.py)和[`read_attempt`](../../../scripts/soak_attempt.py)逐份重读，再用标准库按最近秩重算P50/P95/P99，逐文件重算SHA-256；五轮Owner代际均为`[1, 2, 3, 4, 5]`，Thread数及匿名集合摘要保持一致。常见绝对路径、鉴权Header和密钥模式扫描未发现匹配；这只是针对本白名单证据的检查，不宣称对任意文本的形式化无泄漏证明。

仓库根目录可只读重核Run/Attempt；不会调用模型或更改业务State：

```bash
uv run python - <<'PY'
from pathlib import Path
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run

root = Path('docs/validation/soak-restart-three-platform-2026-09-23-v1/raw')
for platform, run_id in (
    ('linux', '0682a0a8a8a64983903ea69523ecfacd'),
    ('macos', '2b0be5e4777246f9bef38b216fbe7710'),
    ('windows', '6857f23f7002484b98d71742cf5df204'),
):
    manifest, digest = read_published_run(root / platform / run_id)
    _, final = read_attempt(root / platform / 'attempts' / run_id)
    assert manifest.platform == platform
    assert manifest.load.thread_count == 500
    assert manifest.sample_counts == {'product_startup': 3, 'rss_peak': 1}
    assert manifest.fault_counts.eof == 1
    assert final is not None and final.outcome == 'committed'
    assert final.manifest_sha256 == digest
PY
```

## 4. 评审结论、风险与后续门禁

| 检查 | 当前结论 | 证据边界 |
|---|---|---|
| 三平台正式负载 | 通过 | 三个Job终态成功，独立State及Run ID。 |
| 故障与恢复 | 通过本场景 | 每平台精确一次ACK→EOF、三次恢复后正式新进程启动、五轮持久扫描/报告与Fence代际递增。 |
| 低敏提交与数值 | 通过 | 18份原件SHA、Run/Attempt、最近秩统计独立复核一致。 |
| 常规CI | 通过，保留首次故障 | 对应Revision的[常规CI](https://github.com/carrie1988/Harnessix/actions/runs/35864457452)首次仅文档Job因Mermaid渲染超时失败，其余五Job成功；失败Job重跑后六Job均成功。渲染超时排他根因尚未证明，不把重跑成功解释为原因消失。 |
| 冻结阈值与候选 | Profile已冻结，候选未完成 | 三个平台的Profile原件与封印已入本目录；负载前预绑定的第二Run及独立PASS/FAIL报告尚未取得。 |
| 0.9.3d总体 | 未完成 | `action_recovery`等剩余场景和总体发布评审另行完成。 |

源码与失败语义见[重启场景详细设计](../../changes/m09-3d-product-restart-soak.md)。上传件保留14天；本目录保留原始规范字节和清单供长期重核。出现证据、测量边界或源码兼容问题时保留诊断事实，不能将此单次基线升格为性能PASS。

## 5. 冻结Profile与第二Run边界

三份Profile均由本目录原始Run和Attempt调用[`publish_profile`](../../../scripts/soak_threshold.py)生成，并以`SEALED.json`封印。时延三个分位逐项上浮100%，RSS和DB/WAL/Artifact增长上限逐项上浮50%；基线为0的增长上限保持0。硬件档位、Python 3.12小版本、完整负载、一次预期EOF及无Turn Provider身份均与基线绑定。选型理由、比较合同、失败语义与接口见[冻结阈值详细设计](../../changes/m09-3d-product-restart-frozen-profile-candidate.md)。

| 平台 | Profile原件 | Profile SHA-256 | 启动P99上限（ns） | RSS上限（bytes） | DB增长上限（bytes） |
|---|---|---|---:|---:|---:|
| Linux | [profile.json](profiles/linux/33e970328c664aac8dcbd63f329b33e1/profile.json) / [SEALED.json](profiles/linux/33e970328c664aac8dcbd63f329b33e1/SEALED.json) | `02af474d46740a41aad7c7195fd0e9f92913effc940363d987885ab648f9920a` | 5,206,540,460 | 151,044,096 | 1,916,928 |
| macOS | [profile.json](profiles/macos/f002516ffd0f4c309f1e0f6cadcde49e/profile.json) / [SEALED.json](profiles/macos/f002516ffd0f4c309f1e0f6cadcde49e/SEALED.json) | `6fa09ca8e87520a9992b4ad5ec8ab054b9fb00d83b5594a98ff0b1e011a86f02` | 12,315,868,750 | 162,717,696 | 2,070,528 |
| Windows | [profile.json](profiles/windows/87d7b6a0e45c4bdebb02eedef21c2f83/profile.json) / [SEALED.json](profiles/windows/87d7b6a0e45c4bdebb02eedef21c2f83/SEALED.json) | `4b26a7f91fc231bc14d534f26eac49ba35faa59fb637b6996913698ba59a46e3` | 12,011,673,000 | 157,108,224 | 2,064,384 |

冻结只是事先确定验收尺度，不构成通过结论。[候选工作流](../../../.github/workflows/restart-soak-candidate.yml)必须在Profile提交后由干净Revision运行；每平台的第二Run须有STARTED v2预绑定、完整V5证明、FINAL和独立封印Report。在第二Run及其原件归档前，本场景发布状态保持`unverified`。

[首次候选工作流 35867100728](https://github.com/carrie1988/Harnessix/actions/runs/35867100728)仅Linux和macOS成功，Windows在负载前因规范Profile文件被Git自动换行转换而报`soak_profile_invalid`，没有Windows候选Attempt。隔离的`core.autocrlf=true` Checkout复现了封印SHA不一致；修复仅对本目录`raw/**`、`profiles/**`及后续候选原件设置`-text`，并增加Git属性回归。诊断和修复边界见[专项设计第7节](../../changes/m09-3d-product-restart-frozen-profile-candidate.md)；三平台候选必须在修复后重新取得，不能把首次两平台成功当成发布PASS。
