---
doc_type: validation-evidence
status: reviewing
version: 1
code_revision: c63f970f3386632b9720ae33d1a7f056e70c0510
owners:
  - core
modules:
  - agent
  - session
  - artifacts
  - documentation
related_adrs:
  - docs/adr/0092-reproducible-local-soak-and-release-thresholds.md
related_tests:
  - tests/benchmarks/test_soak_artifact_growth.py
  - tests/agent/test_store.py
supersedes: []
---

# macOS Artifact增长Soak第二次规模诊断

## 1. 判定与范围

干净Revision `c63f970f3386632b9720ae33d1a7f056e70c0510`在macOS上完成单Thread、2件预热、
20件正式Artifact的真实`AgentRuntime → CodingToolRuntime.grep → SQLiteArtifactStore`负载。
22件Artifact均完整分页、校验引用、受控到期清理并留下Tombstone；原始Run和Attempt可独立重读。

**本Run仍是诊断事实，不是可冻结的发行基线或发布PASS。** 对应
[CI 35822751423](https://github.com/carrie1988/Harnessix/actions/runs/35822751423)的macOS、Linux Python
3.12/3.13、Container和Documentation作业通过，Windows Benchmark作业 `107057914984`失败：
`test_artifact_turn_timeout_records_failed_attempt`使用10毫秒Turn期限时，临时`session.db`在目录清理时
仍被占用，报`WinError 32`。此前只读同步SQLite句柄已在Revision `d257f99`显式关闭；当前定位指向
取消与异步连接建立/关闭的资源回收竞态，CI日志本身未记录精确交错。后续修复不能追认本Run的发行属性，须在新的干净
Revision重跑正式规模并以其精确CI验收。

## 2. 固定负载和统计

| 项目 | 原始事实 |
|---|---|
| Run ID | `7c21892e418b4e09b873bf23c9d92c94` |
| UTC时间 | 2026-09-23 05:32:38.130068～05:32:43.904423 |
| 环境 | macOS、Python 3.13.8、16逻辑CPU、`c16-m48`；RSS来源`getrusage`，原始单位bytes |
| 负载 | 单Thread、2预热+20正式Turn、22件Artifact、固定Provider共44次步骤请求；无网络模型调用 |
| 覆盖 | 5件近上限、17件小件；22件共197页，正式读取样本195页 |
| 清理 | 清理前逻辑正文4374662字节；过期22、墓碑22、Manifest保留22、清理后活正文0 |
| 故障计数 | Manifest六类故障均为0；本次不包含复合故障注入 |

`nearest_rank_v1`以正式样本升序排列后取`ceil(p × n / 100)`位置；时延单位纳秒，RSS单位字节：

| 指标 | 样本数 | P50 | P95 | P99 |
|---|---:|---:|---:|---:|
| `artifact_publish` | 20 | 7353334 | 13856458 | 15884792 |
| `artifact_read` | 195 | 5990375 | 6710250 | 7257417 |
| `rss_peak` | 1 | 76447744 | 76447744 | 76447744 |

Session DB端点由98304增至5058560字节，WAL两个端点均为0；DB端点不等于运行期磁盘峰值，
RSS为整个Runner进程高水位而非Artifact Store独占内存。单次P95不用于反推工程阈值。

## 3. 原始文件、校验和与复核

| 文件 | SHA-256 |
|---|---|
| [`samples.jsonl`](7c21892e418b4e09b873bf23c9d92c94/samples.jsonl) | `2e3436368012b3571133ddc977fe00e503321ad55bddd25e192982fb75ccce35` |
| [`artifact-proof.json`](7c21892e418b4e09b873bf23c9d92c94/artifact-proof.json) | `4a7f6d9c3946094ab99923d261b9e22151e913cb3d7e466da8fe0b75d626bdee` |
| [`manifest.json`](7c21892e418b4e09b873bf23c9d92c94/manifest.json) | `b6d1edadec0d6b98e7b28d568f5ead7402b9c1776c58d747e1d1e6f8d08eafff` |
| [`COMMITTED.json`](7c21892e418b4e09b873bf23c9d92c94/COMMITTED.json) | `6f38f75d40d7f9ce9484bf9d512e3b1f80e77e32a68af31f291d34369f0887db` |
| [`STARTED.json`](attempts/7c21892e418b4e09b873bf23c9d92c94/STARTED.json) | `d426d8c53cfea0f59ff939ac1ed4913ce8afb2fd14dffdf4a6049feb440b0907` |
| [`FINAL.json`](attempts/7c21892e418b4e09b873bf23c9d92c94/FINAL.json) | `6887b9858b44319bbf177d2482192d372c97f3336637f946d7a7b749254067a3` |

复制件与生成原件逐字节相同。`read_published_run`和`read_attempt`重读后确认
`FINAL.outcome=committed`且Manifest摘要一致；独立标准库程序重算三个指标的样本数及P50/P95/P99、
22件/197页、5件近上限和清理前后逻辑正文。白名单隐私扫描未检出本机绝对路径、临时数据库名、
固定搜索正文、HTTP认证Header或凭据前缀。Proof不提供正文重建，也不能证明24小时真实TTL漂移。

在仓库根目录离线复核，不调用模型或改变业务状态：

```bash
uv run python - <<'PY'
from pathlib import Path
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run

root = Path('docs/validation/soak-macos-artifact-2026-09-23-v2')
run_id = '7c21892e418b4e09b873bf23c9d92c94'
manifest, digest = read_published_run(root / run_id)
_, final = read_attempt(root / 'attempts' / run_id)
assert manifest.load.artifact_count == 22
assert manifest.sample_counts['artifact_publish'] == 20
assert manifest.sample_counts['artifact_read'] == 195
assert final is not None and final.outcome == 'committed'
assert final.manifest_sha256 == digest
PY
```

后续须验证异步连接取消修复的Windows原生CI，再从该新Revision生成新的Run；另需独立冻结阈值Profile、
第二次同环境复验、Linux/Windows正式负载及0.9.3d其他场景。不得覆盖本目录的原始失败谱系。
