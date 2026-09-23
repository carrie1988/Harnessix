---
doc_type: validation-evidence
status: current
version: 1
code_revision: 6c9a1577f467c99f0eb1b99d7c8270bc811ca583
owners:
  - core
modules:
  - agent
  - context
  - session
  - documentation
related_adrs:
  - docs/adr/0092-reproducible-local-soak-and-release-thresholds.md
related_tests:
  - tests/benchmarks/test_soak_context_proof.py
  - tests/benchmarks/test_soak_long_session.py
  - tests/benchmarks/test_soak_evidence.py
  - tests/benchmarks/test_soak_attempt.py
supersedes: []
---

# 0.9.3d macOS长会话Context/Compaction正式规模基线

## 1. 证据结论与限制

在干净源码Revision `6c9a1577f467c99f0eb1b99d7c8270bc811ca583`上，
[`run_long_session_context`](../../../scripts/soak_long_session.py)经真实`AgentRuntime`、`ContextEngine`、
Compaction账本与`SQLiteSessionStore`完成单Thread、5个预热Turn和1000个正式Turn。每个正式Turn
均有Context和模型历史检查；199个正式Turn内的压缩摘要尝试与活动窗口成功完成。全部13255个持久
事件在生成时经过Replay与投影一致性检查，低敏Proof保留连续序号、类型及Turn归属。复制件经
[`read_published_run`](../../../scripts/soak_evidence.py)与
[`read_attempt`](../../../scripts/soak_attempt.py)双重重读。实现Revision由
[CI 35809702564](https://github.com/carrie1988/Harnessix/actions/runs/35809702564)完成Linux Python 3.12/3.13、
macOS、Windows、固定Container和Documentation六实例验收。

**这是macOS单平台单次规模基线，不是0.9.3d性能PASS，不用于直接冻结或反推阈值。**
Manifest的`baseline`仅表示固定规模、干净Revision及RSS单位核对通过；没有独立Threshold Profile、
第二次同平台复验或Linux/Windows正式运行。SDK、Artifact、Action故障和完整产品重启场景仍待完成。
本场景只测`core_runtime`，不能冒充CLI/TUI、stdio或完整产品启动时延。

## 2. 固定身份和运行边界

| 项目 | 原始事实 |
|---|---|
| 源码Revision | `6c9a1577f467c99f0eb1b99d7c8270bc811ca583`；运行前工作树干净；对应六实例CI成功 |
| Run ID | `f1f2ace710984f09bcc32459949c22ce` |
| 时间（UTC） | 2026-09-23 02:16:59.723854 至 02:53:12.376624；约36分13秒 |
| 场景/合同 | `long_session → core_runtime`；`harnessix.soak-scenario/v2`、`harnessix.soak-manifest/v2` |
| 平台/负载 | macOS；单Thread、5预热Turn、1000正式Turn |
| Provider | 普通确定性夹具1005次请求；独立摘要夹具199次请求；无网络模型或API Key |
| 原始样本 | 正式`turn_local`1000条、`rss_peak`1条；预热时延不进入分位数 |
| Context证据 | 正式Context检查1000、正式Compaction及活动窗口199；Proof共1005个Turn、13255个连续事件标记 |
| 故障计数 | 固定六类均为0；本次未注入故障，不代表故障矩阵已通过 |

## 3. 可重算数值和不可比边界

P50/P95/P99由`samples.jsonl`正式样本按`nearest_rank_v1`重算，单位纳秒或字节。

| 指标 | 样本数 | P50 | P95 | P99 |
|---|---:|---:|---:|---:|
| `turn_local` | 1000 | 1393999500 | 3153631375 | 4201200250 |
| `rss_peak` | 1 | 451936256 | 451936256 | 451936256 |

Session DB端点从98304字节增至18366464字节；这是**端点水位，不是运行期磁盘峰值**。
`rss_peak`是整个Runner进程的`getrusage`峰值，不是单个Turn的独占内存。上版
[v1千Turn诊断](../soak-macos-2026-09-23-v2/README.md)未启用Context或Compaction，
其P95不能与本版直接比较来判断性能回归或设定发布阈值。

## 4. 原始文件、完整性和隐私

| 文件 | SHA-256 | 作用 |
|---|---|---|
| [`samples.jsonl`](f1f2ace710984f09bcc32459949c22ce/samples.jsonl) | `ce4d8fc7d34c96939231f42ffc9a667c44656b97c4abdc2d52bc76e607aae64b` | 预热/正式时延和RSS数值 |
| [`context-proof.json`](f1f2ace710984f09bcc32459949c22ce/context-proof.json) | `96fd0bf76c1428da68e08616e008b14822faded3884f1d2e315f5ac6f55ff5c7` | 逐Turn检查、摘要和有序事件标记；1027404字节 |
| [`manifest.json`](f1f2ace710984f09bcc32459949c22ce/manifest.json) | `e1bc9214cf0afe96d71c764030c1627cc10954f5ac1a69d52012d0590d7849ae` | 运行环境、样本统计、水位和证明摘要 |
| [`COMMITTED.json`](f1f2ace710984f09bcc32459949c22ce/COMMITTED.json) | `bd7cdbe42bf4361e5c4a9d7215366cd11313d7aaadc436b0aaa579a90d13290a` | 最后提交标记，绑定Manifest原字节 |
| [`STARTED.json`](attempts/f1f2ace710984f09bcc32459949c22ce/STARTED.json) | `28e8ab0397c80c042ba597dc65eb1d4580361fbad4823da2042db77a914e3d1e` | 负载前Attempt事实 |
| [`FINAL.json`](attempts/f1f2ace710984f09bcc32459949c22ce/FINAL.json) | `9d9bc77943d2a6433989e9d98dcfa8907f78aac79cd74781b3298e8d3bd23846` | `committed/publishing`终态及Manifest摘要 |

六个文件与生成位置逐字节相同。Run目录精确包含四个文件，Attempt目录精确包含两个文件。
Reader核对规范JSON、文件集合、摘要链、样本计数/分位数、Proof连续事件序号、每Turn压缩事件顺序、
窗口数量与双Provider请求数；独立标准库读取另行重算1000个时延分位数、1000次正式Context检查、
199次压缩窗口和13255个连续事件序号。隐私扫描未检出本机绝对路径、固定Prompt、摘要正文、
Session数据库名或凭据特征。Proof不含业务Thread/Turn/Window UUID和完整事件载荷；因此它可复核
**脱敏结构与计数**，不可独立重建业务Session，也不是远程执行真实性证明。

## 5. 复验和剩余门禁

在仓库根目录执行以下命令可重新校验复制件，不发起模型请求：

```bash
uv run python - <<'PY'
from pathlib import Path
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run

run = Path('docs/validation/soak-macos-2026-09-23-v3/f1f2ace710984f09bcc32459949c22ce')
manifest, _ = read_published_run(run)
_, final = read_attempt(run.parent / 'attempts' / run.name)
assert manifest.load.turn_count == 1000
assert manifest.summary_request_count == 199
assert final is not None and final.outcome == 'committed'
PY
```

后续须先冻结与macOS硬件档位绑定的独立Threshold Profile，再在**新Run**中复验；Linux和Windows
也要分别完成固定负载。0.9.3d其他场景及发布阈值尚未完成，本目录不得单独用作发布PASS。
