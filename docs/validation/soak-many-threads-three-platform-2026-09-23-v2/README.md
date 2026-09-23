---
doc_type: validation-evidence
status: current
version: 1
code_revision: 6e64a5cba2108ac77de90a5726b45773b4482c75
owners:
  - core
modules:
  - agent
  - app_server
  - session
  - documentation
related_adrs:
  - docs/adr/0092-reproducible-local-soak-and-release-thresholds.md
related_tests:
  - tests/benchmarks/test_soak_many_threads.py
  - tests/benchmarks/test_soak_threshold.py
  - tests/benchmarks/test_run_many_threads_soak_release.py
supersedes: []
---

# 0.9.3d多Thread三平台v6逐轮证明正式基线

## 1. 结论与范围

Revision `6e64a5cba2108ac77de90a5726b45773b4482c75`的[手动多Thread工作流 35876022276](https://github.com/carrie1988/Harnessix/actions/runs/35876022276)在Linux、macOS、Windows各完成一次固定500 Thread、每页50条、预热一次、正式重启三次的真实Runtime/App Service负载。三个平台的Run与Attempt均成功；归档的18份原始文件与三个GitHub ZIP逐字节一致，Run/Attempt Reader、v6逐轮分页Proof、原始样本最近秩分位数均独立复核通过。[证据清单](bundle-manifest.json)绑定来源上传件及每个文件的SHA-256，[评审包](review-packet.json)记录数值与未关闭门禁。

**这是三平台各一次正式基线，不是场景PASS。** `manifest.status=baseline`只表示固定负载及证据合同成立。冻结工程阈值、负载前绑定的第二独立候选和三平台独立报告尚未完成；0.9.3d其他场景及0.9.4～0.9.6亦未由本证据关闭。源码Revision的[常规CI 35876009038](https://github.com/carrie1988/Harnessix/actions/runs/35876009038)须与手动Soak分开核对终态，不以三平台工作流绿色替代全仓CI。

## 2. 负载、证明与数据流程

每个平台在独立的干净Checkout和临时业务State内，经真实[`AgentRuntime`](../../../src/harnessix/agent/runtime.py)创建并从SQLite重读500个Thread；每次新建Runtime与[`AgentApplicationService`](../../../src/harnessix/app_server/service.py)后遍历10页，每页最多50条。执行期以明文身份集合检查重复、缺失和持久化一致性，但上传件不包含明文Thread ID、Workspace路径、SQLite State或模型请求正文。`SoakProvider.request_count=0`；没有Agent Turn、Action或外部效果。

现行[`SoakThreadProof`](../../../scripts/soak_thread_proof.py)为每个Run保存四轮、每轮10页的匿名标签与启动/分页样本索引；每轮500个标签唯一且集合摘要相同。[`read_published_run`](../../../scripts/soak_evidence.py)重新检查Proof规范字节、Manifest摘要、页边界、样本顺序、正式样本数与最后提交标记。每个平台共11条预热样本、3条正式启动、30条正式分页及1条RSS样本，合计45条；结果另由标准库排序和最近秩索引复算。这些证明不能独立读取已删除的临时SQLite，来源可信度仍依赖真实Runner、源码Revision和工作流评审。发布入口的20分钟子进程硬期限未在正常成功路径触发；本证据不证明底层SQLite任意I/O都可强制取消。

## 3. 平台数值与身份

| 平台 | Python/硬件档位 | Run ID | 启动P50/P95/P99（ns） | 分页P50/P95/P99（ns） | RSS峰值（bytes） | DB末端（bytes） |
|---|---|---|---:|---:|---:|---:|
| Linux | 3.12.3 / `c4-m16` | `f8ea8f00fbaf49328709bc1f7f6a2a96` | 673,383,291 / 676,030,022 / 676,030,022 | 335,773,337 / 668,742,489 / 669,079,487 | 60,469,248 | 589,824 |
| macOS | 3.12.10 / `c3-m7` | `7addaab82c124924988a90f430c09bb0` | 1,287,078,709 / 1,897,536,666 / 1,897,536,666 | 588,565,875 / 1,166,799,000 / 1,381,205,916 | 65,372,160 | 634,880 |
| Windows | 3.12.10 / `c4-m16` | `298a7b2494d64ec981ea5f181c6c0715` | 1,687,470,000 / 1,737,679,100 / 1,737,679,100 | 827,828,600 / 1,702,970,500 / 1,844,074,400 | 62,246,912 | 634,880 |

三平台DB起始端点均为98,304 bytes；WAL与Artifact末端均为0。DB数值是主文件首尾字节数，不是运行期磁盘峰值。RSS只来自被测Runner进程的系统高水位。托管机平台与硬件档位不同，跨平台绝对值不可互相作为阈值或解释为C端多租户SLA。

## 4. 原件、完整性、失败边界与复核

| 平台 | Run / Attempt原件 | Manifest SHA-256 | GitHub ZIP SHA-256 |
|---|---|---|---|
| Linux | [Run](raw/linux/f8ea8f00fbaf49328709bc1f7f6a2a96/manifest.json) / [Attempt](raw/linux/attempts/f8ea8f00fbaf49328709bc1f7f6a2a96/FINAL.json) | `8064b6772c3a088561bc4393c4224891e42b450d130b43a8448c4c97252a449a` | `33745d83da9b98bb4bdd2a53ba10288ea905af1d4ab75e9f940596ffe89cca1b` |
| macOS | [Run](raw/macos/7addaab82c124924988a90f430c09bb0/manifest.json) / [Attempt](raw/macos/attempts/7addaab82c124924988a90f430c09bb0/FINAL.json) | `1639f2f0c6d062682cf662b46affd14d591d6b4700ede05487bb1d65a6570185` | `0ea21c3999983d3051fbde88926049c5117c509a18a8fccb146eab527ef28b1d` |
| Windows | [Run](raw/windows/298a7b2494d64ec981ea5f181c6c0715/manifest.json) / [Attempt](raw/windows/attempts/298a7b2494d64ec981ea5f181c6c0715/FINAL.json) | `284e18862770849d66d395e99ed2ae31bfe6de59852847580f7fc0d2882579eb` | `cbcc32c2f4c182a126db7aad0d99f25de7ff111c5092f17571d3bbe2575303d0` |

每平台的六份原件为`samples.jsonl`、`thread-proof.json`、`manifest.json`、`COMMITTED.json`、`STARTED.json`和`FINAL.json`。[`.gitattributes`](../../../.gitattributes)对此目录`raw/**`设置`-text`，确保Windows Checkout不做换行转换；ZIP摘要标识上传件，不能替代逐文件SHA、规范字节和Reader核验。归档白名单经常见密钥、鉴权Header、宿主绝对路径和个人标识模式扫描无命中，此扫描不构成对任意内容的形式化保密保证。

只读复核命令：

```bash
uv run python - <<'PY'
from pathlib import Path
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run
from scripts.soak_manifest import SoakManifestV6

root = Path('docs/validation/soak-many-threads-three-platform-2026-09-23-v2/raw')
for platform in ('linux', 'macos', 'windows'):
    base = root / platform
    runs = tuple(path for path in base.iterdir() if path.is_dir() and path.name != 'attempts')
    assert len(runs) == 1
    run, digest = read_published_run(runs[0])
    _, final = read_attempt(base / 'attempts' / run.run_id)
    assert isinstance(run, SoakManifestV6) and run.platform == platform
    assert run.status == 'baseline' and run.load.thread_count == 500
    assert run.load.warmup_count == 11
    assert run.sample_counts == {'app_service_startup': 3, 'thread_list_page': 30, 'rss_peak': 1}
    assert final is not None and final.outcome == 'committed'
    assert final.manifest_sha256 == digest
PY
```

## 5. 评审状态与后续门禁

| 检查 | 状态 | 边界 |
|---|---|---|
| 三平台真实固定负载 | 通过 | 三个独立Job和各自不同的State/Run ID；模型请求数0。 |
| v6 Proof、Run/Attempt、文件和统计 | 通过 | 18份原件、三个ZIP、四轮分页及45条样本各平台独立复核。 |
| 同Revision六实例常规CI | 待终态 | [CI 35876009038](https://github.com/carrie1988/Harnessix/actions/runs/35876009038)独立检查，不用手动Soak替代。 |
| 三平台冻结Profile与第二Run | 未执行 | 单次基线不判PASS；不得以历史v1或完整产品重启Profile替代。 |
| 0.9.3d整体 | 未完成 | Action恢复、长会话和Artifact等场景另行验证。 |

GitHub上传件保留14日；本目录归档规范原件与文件摘要。任何平台在后续CI、Proof或阈值复核中发现缺陷，应保留原件作为诊断，修复后在新Revision重新采集，不能原地补写或追认PASS。源码设计及相关失效路径见[逐轮分页证明v6详设](../../changes/m09-3d-many-threads-proof-v6.md)与[三平台采集详设](../../changes/m09-3d-many-threads-three-platform-evidence.md)。
