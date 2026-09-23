---
doc_type: validation-evidence
status: current
version: 1
code_revision: 12bfc1108efc461742712c98fe782ce4b1849f05
owners:
  - core
modules:
  - agent
  - app_server
  - session
  - documentation
related_adrs:
  - docs/adr/0089-bounded-local-transport-lifecycle.md
  - docs/adr/0092-reproducible-local-soak-and-release-thresholds.md
related_tests:
  - tests/benchmarks/test_run_many_threads_soak_release.py
  - tests/benchmarks/test_soak_many_threads.py
  - tests/benchmarks/test_soak_evidence.py
  - tests/benchmarks/test_soak_attempt.py
supersedes: []
---

# 0.9.3d多Thread三平台正式负载基线

## 1. 结论与证据边界

源码Revision `12bfc1108efc461742712c98fe782ce4b1849f05`的[手动多Thread Soak工作流 35870233482](https://github.com/carrie1988/Harnessix/actions/runs/35870233482)在Linux、macOS和Windows三个独立Job均成功。每个平台通过真实[`AgentRuntime`](../../../src/harnessix/agent/runtime.py)与[`AgentApplicationService.list_threads`](../../../src/harnessix/app_server/service.py)在持久SQLite上创建500个Thread、预热一次、正式重启三次，并在每次重启后遍历10页50条的列表。Run和Attempt原件下载后均由Reader重读，15份原始文件SHA-256与标准库最近秩分位数再次核对；来源见[证据Manifest](bundle-manifest.json)，汇总见[评审包](review-packet.json)。同Revision[常规CI 35870214455](https://github.com/carrie1988/Harnessix/actions/runs/35870214455)终态须独立核对，不以手动工作流绿色替代。

**证据等级仅为三平台各一次正式负载基线，`manifest.status=baseline`不等于阈值PASS或0.9.3d完成。** 当前v1低敏证据没有单独保存每轮Thread集合摘要或分页Proof；Runner在执行期核对了完整集合，但离线Reader只能验证样本、Manifest和提交事实，无法从归档字节独立重演身份集合。启动与分页超时后当前SQLite任务的自然排空也没有独立全局期限。这两项在冻结Profile和第二独立候选前仍须关闭或形成有证明的发布边界。

## 2. 测量边界、固定负载与数值

`app_service_startup`从打开已填充Session上的新`AgentRuntime`到构造`AgentApplicationService`结束；`thread_list_page`是逐页`list_threads`时延。两者不是完整产品stdio握手的`product_startup`，不能与[产品重启场景](../soak-restart-three-platform-2026-09-23-v1/README.md)混用阈值。每平台11条预热样本不计入正式统计；正式为3条启动、30条页面及1条进程RSS。Provider模型请求数0；无Turn、Action或外部效果，故不能证明Action恢复。

| 平台 | Python/档位 | Run ID | 启动P50/P95/P99（ns） | 页P50/P95/P99（ns） | RSS峰值（bytes） | DB端点（bytes） |
|---|---|---|---:|---:|---:|---:|
| Linux | 3.12.3 / `c4-m16` | `c5f3d2416cc140ef80d95f773049fb44` | 669,132,362 / 795,810,024 / 795,810,024 | 332,042,832 / 657,744,564 / 659,543,741 | 64,577,536 | 589,824 |
| macOS | 3.12.10 / `c3-m7` | `fc77033b2f5448bdad16aee4c698e108` | 1,432,157,583 / 1,455,887,125 / 1,455,887,125 | 639,261,416 / 1,444,554,750 / 1,551,159,416 | 73,793,536 | 634,880 |
| Windows | 3.12.10 / `c4-m16` | `1eec212404234751900fe3cbfac14618` | 1,592,203,100 / 1,628,829,100 / 1,628,829,100 | 817,336,000 / 1,596,449,200 / 1,617,821,600 | 64,749,568 | 634,880 |

三平台DB起始端点均为98,304 bytes，末端数值只是SQLite主文件首尾水位；WAL、Artifact末端均为0，不说明运行中峰值。RSS是Runner进程高水位，不包括独立服务端；硬件档位和托管机噪声不同，不能横向比较绝对时延或宣称C端容量SLO。各项故障计数均为0，但此无Action场景没有故障注入，0不代表恢复能力已受测。

## 3. 原始文件、身份与独立复核

| 平台 | Run/Attempt原件 | Manifest SHA-256 | GitHub上传件SHA-256 |
|---|---|---|---|
| Linux | [Run](raw/linux/c5f3d2416cc140ef80d95f773049fb44/manifest.json) / [Attempt](raw/linux/attempts/c5f3d2416cc140ef80d95f773049fb44/FINAL.json) | `3b1b2215473c8dae6f081769d07b9b02e2bfb10d98fa590cc65d9028cce11d0b` | `a26de9ef99cff855c4f6bcb4e0025a9009762d72912e7da92a0c97899f52a467` |
| macOS | [Run](raw/macos/fc77033b2f5448bdad16aee4c698e108/manifest.json) / [Attempt](raw/macos/attempts/fc77033b2f5448bdad16aee4c698e108/FINAL.json) | `2e27d4f9d3fb2f09ddac03a260c4ac175e6a4753d6a62848820b2eb3db2b3856` | `6a32f88e752bf17b0170bfc068a4c08bebdba4fc4920c1abcea81b59ac6a6031` |
| Windows | [Run](raw/windows/1eec212404234751900fe3cbfac14618/manifest.json) / [Attempt](raw/windows/attempts/1eec212404234751900fe3cbfac14618/FINAL.json) | `1e2825182d6f811d8122e28b491a0ad709e4aa070259026e699b9c5fa54b841e` | `b9a32e43b8a6b9bb5278926680659607928d1774f61e9977146005aa3bf8c497` |

每平台三份Run文件（`samples.jsonl`、`manifest.json`、`COMMITTED.json`）和两份Attempt文件（`STARTED.json`、`FINAL.json`）均以下载原字节保存，见[`raw`](raw)。[证据Manifest](bundle-manifest.json)逐文件记录字节数与摘要；GitHub ZIP摘要仅标识下载包，不代替规范文件核验。[`.gitattributes`](../../../.gitattributes)对此目录`raw/**`设置`-text`，避免Windows Checkout换行转换改写SHA。白名单文件经过常见密钥、鉴权Header和宿主绝对路径模式扫描，无命中；这不是对任意文本的形式化保密证明。

只读重核命令不会调用模型或修改产品State：

```bash
uv run python - <<'PY'
from pathlib import Path
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run

root = Path('docs/validation/soak-many-threads-three-platform-2026-09-23-v1/raw')
for platform in ('linux', 'macos', 'windows'):
    base = root / platform
    runs = tuple(p for p in base.iterdir() if p.is_dir() and p.name != 'attempts')
    assert len(runs) == 1
    run, digest = read_published_run(runs[0])
    _, final = read_attempt(base / 'attempts' / run.run_id)
    assert run.platform == platform and run.status == 'baseline'
    assert run.load.thread_count == 500 and run.load.warmup_count == 11
    assert run.sample_counts == {'app_service_startup': 3, 'thread_list_page': 30, 'rss_peak': 1}
    assert final is not None and final.outcome == 'committed'
    assert final.manifest_sha256 == digest
PY
```

## 4. 评审状态、风险与后续门禁

| 检查 | 结果 | 边界 |
|---|---|---|
| 三平台真实固定负载 | 通过 | 三个独立Job成功，各自新State和Run ID；没有模型或Action效果。 |
| Run/Attempt与摘要 | 通过 | Reader、15份原件SHA、标准库分位数和上传ZIP摘要一致。 |
| 同Revision六实例CI | 待核对 | [CI 35870214455](https://github.com/carrie1988/Harnessix/actions/runs/35870214455)须达到终态。 |
| 场景Proof与超时排空 | 未关闭 | v1缺逐轮匿名集合/分页证明；超时后自然排空无独立全局期限。 |
| 冻结阈值与独立候选 | 未执行 | 不凭单次基线、其他场景Profile或手工Manifest判PASS。 |
| 0.9.3d整体 | 未完成 | Action恢复及长会话/Artifact的多平台验收仍独立进行。 |

GitHub上传件保留14日；本目录保存规范原件和摘要供长期核查。发现基线Revision的CI或场景语义缺陷时，本目录降级为诊断事实，不能原地修补或追认Profile；修复须新Revision和新Run。设计、数据流、接口、失败与部署边界见[多Thread三平台采集详设](../../changes/m09-3d-many-threads-three-platform-evidence.md)。
