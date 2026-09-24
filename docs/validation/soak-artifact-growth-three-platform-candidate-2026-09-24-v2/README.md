---
doc_type: validation-evidence
status: current
version: 1
code_revision: d4a3ee20a8e17fab466c690e86a9a48991430398
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

# 0.9.3d Artifact增长三平台第二轮候选性能FAIL诊断

## 1. 结论与证据身份

在[60件负载三平台v2基线和封印Profile](../soak-artifact-growth-three-platform-2026-09-24-v2/README.md)提交后，Revision `d4a3ee20a8e17fab466c690e86a9a48991430398`的[手动候选工作流 35966178443](https://github.com/carrie1988/Harnessix/actions/runs/35966178443)三个独立Job均完成执行：**macOS与Windows报告为`PASS/within_limits`，Linux报告为`FAIL/limit_exceeded`**，唯一越限项为`artifact_publish.p99`。原始文件及上传件来源`93e6f05720a949f7860b708bd2cb5ee6`，定量结果见[评审包](review-packet.json)。对应[常规CI 35966150370](https://github.com/carrie1988/Harnessix/actions/runs/35966150370)六实例成功。

**该Revision的固定Artifact增长场景候选仍未通过，本目录为第二轮FAIL诊断原件**：不删除失败报告、不调高已封印Profile、不把两平台PASS解释为场景通过。

## 2. 失败事实

| 平台/档位 | 候选Run ID | 报告状态 | 越限指标 | 候选发布P50/P95/P99（ns） | 基线发布P50/P95/P99（ns） |
|---|---|---|---|---|---|
| Linux `c4-m16` | `b03ab6342b864ab6987aca09db327bb7` | FAIL | `artifact_publish.p99` | 38,878,401 / 60,762,945 / **780,141,535** | 35,489,939 / 75,788,457 / 88,631,683 |
| macOS `c3-m7` | ---
doc_type: validation-evidence
status: current
version: 1
code_revision: d4a3ee20a8e17fab466c690e86a9a48991430398
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

# 0.9.3d Artifact增长三平台第二轮候选性能FAIL诊断

## 1. 结论与证据身份

在[60件负载三平台v2基线和封印Profile](../soak-artifact-growth-three-platform-2026-09-24-v2/README.md)提交后，Revision `d4a3ee20a8e17fab466c690e86a9a48991430398`的[手动候选工作流 35966178443](https://github.com/carrie1988/Harnessix/actions/runs/35966178443)三个独立Job均完成执行：**macOS与Windows报告为`PASS/within_limits`，Linux报告为`FAIL/limit_exceeded`**，唯一越限项为`artifact_publish.p99`。原始文件及上传件来源`2f29273ee17643bbaf02bec7d9eb6641`，定量结果见[评审包](review-packet.json)。对应[常规CI 35966150370](https://github.com/carrie1988/Harnessix/actions/runs/35966150370)六实例成功。

**该Revision的固定Artifact增长场景候选仍未通过，本目录为第二轮FAIL诊断原件**：不删除失败报告、不调高已封印Profile、不把两平台PASS解释为场景通过。

## 2. 失败事实

| 平台/档位 | 候选Run ID | 报告状态 | 越限指标 | 候选发布P50/P95/P99（ns） | 基线发布P50/P95/P99（ns） |
|---|---|---|---|---|---|
| Linux `c4-m16` | `3c502835b6c546af847c9358b58badb0`对应新Run `见bundle-manifest` | FAIL | `artifact_publish.p99` | 38,878,401 / 60,762,945 / **780,141,535** | 35,489,939 / 75,788,457 / 88,631,683 |
| macOS `c3-m7` | 见[证据Manifest](bundle-manifest.json) | PASS | 无 | 21,776,375 / 39,007,583 / 48,949,458 | 20,335,708 / 41,315,417 / 75,326,625 |
| Windows `c4-m16` | 见[证据Manifest](bundle-manifest.json) | PASS | 无 | 140,614,100 / 227,469,700 / 261,473,500 | 198,197,200 / 566,344,900 / 693,870,000 |

Linux候选的`artifact_publish.p99`为780,141,535 ns，冻结上限为177,263,366 ns；同一份候选的`p50`（38.9ms）与`p95`（60.8ms）均在限内，62件发布、585次分页与到期清理语义全部成立，故障计数全为0。

## 3. 根因分析

1. 越限由**单件近限发布约780毫秒的I/O停顿**造成：60件正式样本中`p99`等于近限件子样本（15件）的最大值，一次共享Runner磁盘停顿即直接顶破上限；同一候选的`p95`（第3高值）未受影响，证明这不是整体性退化。
2. 基线与候选产品代码一致（两Revision之间只有证据文档变更），`p50`/`p95`稳定；连续两轮候选的全部证据都指向同一机制——共享Runner磁盘条件的单点停顿，而非产品代码回归。
3. 负载设计结论：`n=60`时`p99`仍是子样本最大值，统计上无法吸收单点停顿；首轮FAIL（20件）与本轮FAIL（60件）共同证明该指标需要更深的尾部样本。

## 4. 处置与边界

FAIL原件保留，旧Profile保持封印。第二轮负载修复预登记：正式负载由60件提高到**300件**（近限件约75件），使`p99`成为近限件子样本的**第3高值**而非最大值，`p95`成为第15高值；单点I/O停顿不再能单独决定分位数。阈值方法（时延上浮100%、RSS与文件增长上浮50%）与预热2件不变；若第三基线/候选仍出现尾部越限，将继续保留FAIL并如实上报，不以调宽阈值追认。新基线归档为独立v3目录，前两轮基线、Profile与FAIL全部保持只读。
 | 无 | 21,776,375 / 39,007,583 / 48,949,458 | 20,335,708 / 41,315,417 / 75,326,625 |
| Windows `c4-m16` | ---
doc_type: validation-evidence
status: current
version: 1
code_revision: d4a3ee20a8e17fab466c690e86a9a48991430398
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

# 0.9.3d Artifact增长三平台第二轮候选性能FAIL诊断

## 1. 结论与证据身份

在[60件负载三平台v2基线和封印Profile](../soak-artifact-growth-three-platform-2026-09-24-v2/README.md)提交后，Revision `d4a3ee20a8e17fab466c690e86a9a48991430398`的[手动候选工作流 35966178443](https://github.com/carrie1988/Harnessix/actions/runs/35966178443)三个独立Job均完成执行：**macOS与Windows报告为`PASS/within_limits`，Linux报告为`FAIL/limit_exceeded`**，唯一越限项为`artifact_publish.p99`。原始文件及上传件来源见[证据Manifest](bundle-manifest.json)，定量结果见[评审包](review-packet.json)。对应[常规CI 35966150370](https://github.com/carrie1988/Harnessix/actions/runs/35966150370)六实例成功。

**该Revision的固定Artifact增长场景候选仍未通过，本目录为第二轮FAIL诊断原件**：不删除失败报告、不调高已封印Profile、不把两平台PASS解释为场景通过。

## 2. 失败事实

| 平台/档位 | 候选Run ID | 报告状态 | 越限指标 | 候选发布P50/P95/P99（ns） | 基线发布P50/P95/P99（ns） |
|---|---|---|---|---|---|
| Linux `c4-m16` | `3c502835b6c546af847c9358b58badb0`对应新Run `见bundle-manifest` | FAIL | `artifact_publish.p99` | 38,878,401 / 60,762,945 / **780,141,535** | 35,489,939 / 75,788,457 / 88,631,683 |
| macOS `c3-m7` | 见[证据Manifest](bundle-manifest.json) | PASS | 无 | 21,776,375 / 39,007,583 / 48,949,458 | 20,335,708 / 41,315,417 / 75,326,625 |
| Windows `c4-m16` | 见[证据Manifest](bundle-manifest.json) | PASS | 无 | 140,614,100 / 227,469,700 / 261,473,500 | 198,197,200 / 566,344,900 / 693,870,000 |

Linux候选的`artifact_publish.p99`为780,141,535 ns，冻结上限为177,263,366 ns；同一份候选的`p50`（38.9ms）与`p95`（60.8ms）均在限内，62件发布、585次分页与到期清理语义全部成立，故障计数全为0。

## 3. 根因分析

1. 越限由**单件近限发布约780毫秒的I/O停顿**造成：60件正式样本中`p99`等于近限件子样本（15件）的最大值，一次共享Runner磁盘停顿即直接顶破上限；同一候选的`p95`（第3高值）未受影响，证明这不是整体性退化。
2. 基线与候选产品代码一致（两Revision之间只有证据文档变更），`p50`/`p95`稳定；连续两轮候选的全部证据都指向同一机制——共享Runner磁盘条件的单点停顿，而非产品代码回归。
3. 负载设计结论：`n=60`时`p99`仍是子样本最大值，统计上无法吸收单点停顿；首轮FAIL（20件）与本轮FAIL（60件）共同证明该指标需要更深的尾部样本。

## 4. 处置与边界

FAIL原件保留，旧Profile保持封印。第二轮负载修复预登记：正式负载由60件提高到**300件**（近限件约75件），使`p99`成为近限件子样本的**第3高值**而非最大值，`p95`成为第15高值；单点I/O停顿不再能单独决定分位数。阈值方法（时延上浮100%、RSS与文件增长上浮50%）与预热2件不变；若第三基线/候选仍出现尾部越限，将继续保留FAIL并如实上报，不以调宽阈值追认。新基线归档为独立v3目录，前两轮基线、Profile与FAIL全部保持只读。
 | 无 | 140,614,100 / 227,469,700 / 261,473,500 | 198,197,200 / 566,344,900 / 693,870,000 |

Linux候选的`artifact_publish.p99`为780,141,535 ns，冻结上限为177,263,366 ns；同一份候选的`p50`（38.9ms）与`p95`（60.8ms）均在限内，62件发布、585次分页与到期清理语义全部成立，故障计数全为0。

## 3. 根因分析

1. 越限由**单件近限发布约780毫秒的I/O停顿**造成：60件正式样本中`p99`等于近限件子样本（15件）的最大值，一次共享Runner磁盘停顿即直接顶破上限；同一候选的`p95`（第3高值）未受影响，证明这不是整体性退化。
2. 基线与候选产品代码一致（两Revision之间只有证据文档变更），`p50`/`p95`稳定；连续两轮候选的全部证据都指向同一机制——共享Runner磁盘条件的单点停顿，而非产品代码回归。
3. 负载设计结论：`n=60`时`p99`仍是子样本最大值，统计上无法吸收单点停顿；首轮FAIL（20件）与本轮FAIL（60件）共同证明该指标需要更深的尾部样本。

## 4. 处置与边界

FAIL原件保留，旧Profile保持封印。第二轮负载修复预登记：正式负载由60件提高到**300件**（近限件约75件），使`p99`成为近限件子样本的**第3高值**而非最大值，`p95`成为第15高值；单点I/O停顿不再能单独决定分位数。阈值方法（时延上浮100%、RSS与文件增长上浮50%）与预热2件不变；若第三基线/候选仍出现尾部越限，将继续保留FAIL并如实上报，不以调宽阈值追认。新基线归档为独立v3目录，前两轮基线、Profile与FAIL全部保持只读。
