---
doc_type: validation-evidence
status: current
version: 3
code_revision: 70c5161986082b63acd31ab1acf8328c9fad9efd
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
  - tests/benchmarks/test_run_sdk_soak_release.py
  - tests/benchmarks/test_run_sdk_soak_candidate.py
  - tests/benchmarks/test_soak_sdk_capacity.py
  - tests/benchmarks/test_soak_evidence.py
  - tests/benchmarks/test_soak_attempt.py
supersedes: []
---

# 0.9.3d SDK容量三平台正式规模基线与评审包

## 1. 结论与证据等级

源码Revision `70c5161986082b63acd31ab1acf8328c9fad9efd`的[手动SDK Soak工作流](https://github.com/carrie1988/Harnessix/actions/runs/35838270267)在Linux、macOS和Windows三个独立Job中均完成固定**协商容量64、1轮预热、3轮正式容量轮次、20次正常往返**；同Revision的[常规CI 35838258049](https://github.com/carrie1988/Harnessix/actions/runs/35838258049)六实例全部成功；原始证据归档提交`1e4d053`的[CI 35841260994](https://github.com/carrie1988/Harnessix/actions/runs/35841260994)同样六实例成功。每个平台均保存独立Run和Attempt；上传件在仓库外重新下载，随后本地Reader复核三次，另用标准库重算18份原始文件SHA-256、正式样本数与最近秩分位数，结果一致。汇总数据见[评审包](review-packet.json)，上传件来源与每份原始文件摘要见[证据Manifest](bundle-manifest.json)。

**证据等级是三平台各一次正式规模基线，不是性能阈值PASS，也不是0.9.3d或1.0发布完成。** 三个平台使用不同硬件档位，不跨平台比较绝对时延；GitHub托管机型的可用资源也不等于终端用户环境。`manifest.status=baseline`只表明冻结输入和证据完整性达到最低条件。工程余量及各平台Profile已按[冻结阈值详设](../../changes/m09-3d-sdk-frozen-profile-candidate.md)形成封印原件；负载前绑定Profile的[第二独立Run及三份PASS报告](../soak-sdk-three-platform-candidate-2026-09-23-v1/README.md)已经归档；`action_recovery`和`restart`两个场景Runner同样未完成。

首次新增工作流的提交`f4398ac`在GitHub解析阶段因job级`runner.temp`不可用而失败，[失败Run 35838197410](https://github.com/carrie1988/Harnessix/actions/runs/35838197410)未启动任何Soak。修复提交`70c5161`将该上下文移入步骤级；本目录只归档修复后真实运行的三份原件，不把解析失败改写为负载成功。

## 2. 测量边界、环境与数值

真实调用链是`AgentClient → SubprocessAgentTransport → stdio → AgentProtocolServer → AgentApplicationService → SQLite`。四轮均按`64 Pending → 63 Pending + 1 Abandoned → 64 Pending + 0 Abandoned → 0/0`验证取消墓碑和迟到Response；`sdk-proof.json`记录匿名计数、进入顺序、两进程RSS与20条正式样本索引。模型Provider请求数为0。`sdk_roundtrip`是本地SDK请求到完整Response的耗时，不包含模型网络或完整Coding Turn。

| 平台 | Python/档位 | Run ID | 往返P50/P95/P99（ns） | 峰值RSS（bytes） | DB端点（bytes） |
|---|---|---|---:|---:|---:|
| Linux | 3.12.3 / `c4-m16` | `4be017812a684966af5bf00a8f481fa3` | 1626258 / 1713349 / 1851137 | 66035712 | 98304 |
| macOS | 3.12.10 / `c3-m7` | `221b359e68984b899ae8d5afa7826471` | 4399208 / 8811209 / 9422167 | 72728576 | 98304 |
| Windows | 3.12.10 / `c4-m16` | `abae5ca23a244a8680dd3707fc906eca` | 3664700 / 3825000 / 3827500 | 64774144 | 98304 |

每个平台各有20条正式往返样本和1条RSS样本，预期取消4次，超时、EOF、未知效果、重复效果和孤儿均0。SQLite主文件由0增至98304字节，WAL两端均为0；这是采样边界的**端点**，不表示运行期磁盘峰值。RSS取SDK父进程与服务端子进程各自峰值的较大者，不表示两进程同时驻留内存之和。Linux读`/proc/self/status`的`VmHWM`并由KiB乘1024；macOS用`getrusage`原始bytes；Windows用`GetProcessMemoryInfo`原始bytes。macOS本机旧基线使用`c16-m48`且Python 3.13.8，与此次CI的`c3-m7`/3.12.10不同，不能进行同环境回归比较。

## 3. 原始证据、摘要与独立复核

| 平台 | Run/Attempt原件 | Manifest SHA-256 | GitHub上传件摘要 |
|---|---|---|---|
| Linux | [Run](raw/linux/4be017812a684966af5bf00a8f481fa3/manifest.json) / [Attempt](raw/linux/attempts/4be017812a684966af5bf00a8f481fa3/FINAL.json) | `a7625eeba9225cbbb961f4873dca2ef99b6421f34bda84a95f0e56a225e3e5cf` | `sha256:571b1a05bca31d1e8e34ee6cbe2fa61f04cf5ab078dcf5b90969cc1369145f6a` |
| macOS | [Run](raw/macos/221b359e68984b899ae8d5afa7826471/manifest.json) / [Attempt](raw/macos/attempts/221b359e68984b899ae8d5afa7826471/FINAL.json) | `53774ac1296bcd5f6929e8d76806ad4dda22bd7470e44bdd4a1c20ece5e27968` | `sha256:9ed794c992c4c4e8d86b70bf108a23cab5ee76da64406ff306ea5851f99f414b` |
| Windows | [Run](raw/windows/abae5ca23a244a8680dd3707fc906eca/manifest.json) / [Attempt](raw/windows/attempts/abae5ca23a244a8680dd3707fc906eca/FINAL.json) | `0b40aa9b1a76a4aee153ad1f23ca39822273fafe834a6e18a08a4e9f9d359514` | `sha256:02015ecfdffb0160df2b66f94e220adf7653e3893529a35c26e1adc3154e0f69` |

每个平台目录恰有四份Run文件（`samples.jsonl`、`sdk-proof.json`、`manifest.json`、`COMMITTED.json`）和两份Attempt文件（`STARTED.json`、`FINAL.json`）。[证据Manifest](bundle-manifest.json)对18份文件逐一列出相对路径、字节数和SHA-256；[评审包](review-packet.json)列出重读、独立数值复核、环境和剩余门禁。下载后的文件曾检查常见宿主绝对路径、认证Header和凭据前缀，未发现匹配；这不是对任意私有内容的形式化无泄漏证明，发布文件仍严格限于Runner白名单。

仓库根目录执行如下只读核验；它不调用模型、不开启网络，也不修改业务State：

```bash
uv run python - <<'PY'
from pathlib import Path
from scripts.soak_attempt import read_attempt
from scripts.soak_evidence import read_published_run

root = Path('docs/validation/soak-sdk-three-platform-2026-09-23-v1/raw')
for platform, run_id in (
    ('linux', '4be017812a684966af5bf00a8f481fa3'),
    ('macos', '221b359e68984b899ae8d5afa7826471'),
    ('windows', 'abae5ca23a244a8680dd3707fc906eca'),
):
    manifest, digest = read_published_run(root / platform / run_id)
    _, final = read_attempt(root / platform / 'attempts' / run_id)
    assert manifest.platform == platform
    assert manifest.load.pending_limit == 64
    assert manifest.sample_counts['sdk_roundtrip'] == 20
    assert manifest.fault_counts.cancelled == 4
    assert final is not None and final.outcome == 'committed'
    assert final.manifest_sha256 == digest
PY
```

## 4. 评审决定、风险与后续门禁

| 检查 | 结论 | 边界 |
|---|---|---|
| 三平台正式负载执行 | 通过 | 工作流三个Job成功；每平台独立State和Run ID。 |
| Run/Attempt提交与摘要 | 通过 | 三份原件下载后Reader通过；18份文件SHA及Manifest SHA复核一致。 |
| 负载规模与取消/迟到语义 | 通过 | 每平台64容量、四轮Proof、4次预期取消和20条正式往返。 |
| 常规六实例CI | 通过 | 对应Revision的[CI 35838258049](https://github.com/carrie1988/Harnessix/actions/runs/35838258049)已达终态，六个Job全部成功；不以Soak绿色替代全仓门禁。 |
| 工程阈值与独立复验 | 通过固定SDK场景 | 三平台Profile已冻结；[第二独立Run](../soak-sdk-three-platform-candidate-2026-09-23-v1/README.md)负载前预绑定并分别生成PASS报告，仍不等于0.9.3d总体发布通过。 |
| 0.9.3d其他场景 | 未完成 | Action恢复、重启Runner及长会话/多Thread/Artifact三平台正式验收另行完成。 |

若后续发现证据完整性或测量语义错误，本目录保留为诊断事实，不提升为可冻结阈值的基线。GitHub上传件保留期14日；仓库中的原始规范字节与Manifest是长期复核依据。设计与失败语义见[三平台采集详设](../../changes/m09-3d-sdk-cross-platform-evidence.md)和[SDK容量详设](../../changes/m09-3d-sdk-capacity-soak.md)。

## 5. 冻结阈值Profile与后续复验

以下Profile在第二独立Run前分别由完整Run/Attempt调用`publish_profile`产生，含不可覆盖的`SEALED.json`；复核见[`test_run_sdk_soak_candidate.py`](../../../tests/benchmarks/test_run_sdk_soak_candidate.py)。工程余量、环境范围、整数阈值和风险取舍见[专项详设](../../changes/m09-3d-sdk-frozen-profile-candidate.md)。Profile冻结不等于候选通过，原始Run/Attempt的18份文件Manifest仍仅覆盖基线原件。

| 平台 | Profile原件 | Profile SHA-256 | 时延P99上限（ns） | RSS峰值上限（bytes） | DB增长上限（bytes） |
|---|---|---|---:|---:|---:|
| Linux | [profile.json](profiles/linux/e7fc115add194e589d35fb9574bfa5c9/profile.json) / [SEALED.json](profiles/linux/e7fc115add194e589d35fb9574bfa5c9/SEALED.json) | `8fbc557ce99d99f002b63c39f01654756e2abc23c2e4449dde4ebcc8349c5c55` | 3702274 | 99053568 | 147456 |
| macOS | [profile.json](profiles/macos/167f432a612f4c58b2e0bc7c80aea4a3/profile.json) / [SEALED.json](profiles/macos/167f432a612f4c58b2e0bc7c80aea4a3/SEALED.json) | `9a5793b79399810fa7e3f3dab9449f9ce99a73ed4582c03647fe2202c3719b90` | 18844334 | 109092864 | 147456 |
| Windows | [profile.json](profiles/windows/5bcdf40d1baf48279ffa1ba9c7bd46c0/profile.json) / [SEALED.json](profiles/windows/5bcdf40d1baf48279ffa1ba9c7bd46c0/SEALED.json) | `368469b0770590d042ba83c837dd7b8b3a1387abeeb44f185bd22281555a8774` | 7655000 | 97161216 | 147456 |

[三平台手动候选工作流](https://github.com/carrie1988/Harnessix/actions/runs/35843752933)已经在干净Revision运行；`STARTED v2`负载前记录Profile ID/SHA，三个独立Report均为PASS。原始Run/Attempt/Report和独立重算见[候选证据归档](../soak-sdk-three-platform-candidate-2026-09-23-v1/README.md)。此结论仅适用于固定SDK容量场景。
