---
doc_type: validation-evidence
status: current
version: 2
code_revision: 0f1948c3a258943698a8fe3e4309b81e78b8d5b3
owners: [core]
modules: [delivery, product_config, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/governance/test_windows_native_material_acceptance.py
  - tests/product_config/test_git_material_input.py
  - tests/product_config/test_git_material_cas_integration.py
  - tests/processes/test_windows_raw_receipt.py
  - tests/workspace/test_snapshot_request_refactor.py
  - tests/workspace/test_snapshot_capacity.py
supersedes: []
---

# Windows完整Git材料并行原生验收记录

## 1. 背景与实现范围

固定0583b53的[最低Commit专项](../windows-directory-write-share-2026-10-03-v1/README.md#6-sdk符号根修复的固定原生通过)
成功不替代完整对象容量与认证raw矩阵。常规CI Run37134036729的Windows Job111234779832中，
原认证raw／Git基准合并步骤failure，实际308秒；仅元数据不证明各case结果或唯一超时根因。

按[总体与详细设计](../../changes/m09-r4-windows-native-material-acceptance.md)，
[现有CI](../../../.github/workflows/ci.yml)将原27选择器迁为三个独立Windows矩阵成员，
fail-fast=false、不忽略失败、原每个pytest五分钟、固定checkout／setup-uv及锁定依赖保持。
原核心Windows job其余步骤、其他CI job和全局权限保持；没有生产材料或最低专项变更。

总runner分钟可能增加，不声称旧合并五分钟的总CI资源上限保持；原单命令／单操作及最低专项正式期限不改。
分组目的为原完整范围的并行验收，不是删测试、降低容量或扩大生产执行时间。

## 2. 选择器与参数化覆盖证明

[治理测试](../../../tests/governance/test_windows_native_material_acceptance.py)冻结8162953原选择器，
Counter核对遗漏、重复和替换，逐对象核对其他原生步骤；旧YAML缺少矩阵的RED实际1失败，修复后7项GREEN。
collect-only分别收集旧27项及新三组，1306个参数化节点的多重集合完全相等：

| 组 | 原选择器 | 参数化节点 | 本机通过 | 本机跳过 | 失败／错误 |
| --- | ---: | ---: | ---: | ---: | ---: |
| authenticated-raw | 16 | 386 | 367 | 19 | 0／0 |
| object-input | 6 | 303 | 299 | 4 | 0／0 |
| cas-reference | 5 | 617 | 617 | 0 | 0／0 |
| 原范围合计 | 27 | 1306 | 1283 | 23 | 0／0 |

本机使用Python3.12、实际Git与原pytest，在三个独立临时基目录并行执行；这是POSIX范围关联回归。
Windows-only跳过单列，不能记作Windows原生通过；治理7项不与该业务范围混为全仓验收。
原最低专项18输入及contract.json原字节仍一致，生产材料源码未变化。

## 3. 固定0f候选实际结果

固定提交`0f1948c3a258943698a8fe3e4309b81e78b8d5b3`的[CI Run 37136790041](https://github.com/carrie1988/Harnessix/actions/runs/37136790041)、
attempt 1已终态；三个完整Windows原生矩阵组均为`success`：

| 组 | Job | 结果 | pytest耗时 |
|---|---|---|---:|
| authenticated-raw | [111242837376](https://github.com/carrie1988/Harnessix/actions/runs/37136790041/job/111242837376) | success | 141秒 |
| object-input | [111242837311](https://github.com/carrie1988/Harnessix/actions/runs/37136790041/job/111242837311) | success | 180秒 |
| cas-reference | [111242837410](https://github.com/carrie1988/Harnessix/actions/runs/37136790041/job/111242837410) | success | 119秒 |

27个selector保持不变；1306个参数化节点是本地collect-only的多重集合证据，不能表述为Windows原生1306节点无跳过通过。
三个Job成功只证明各自完整组的Job结论，不改变表2中本机`23`个Windows-only跳过的含义。

核心Windows [Job 111242837353](https://github.com/carrie1988/Harnessix/actions/runs/37136790041/job/111242837353)
的五个业务步骤均为`success`：NTFS事务与审批编码写链、Git读取与取消回收、产品重启与State创建、完整业务状态备份、完整业务状态恢复；
其后的readability检查失败，因而整个Run仍为`FAIL`，后续广泛回归未运行。容器沙箱[Job 111242837229](https://github.com/carrie1988/Harnessix/actions/runs/37136790041/job/111242837229)
与文档[Job 111242837340](https://github.com/carrie1988/Harnessix/actions/runs/37136790041/job/111242837340)为`success`。
旧[Run 37134036729](https://github.com/carrie1988/Harnessix/actions/runs/37134036729)及其308秒失败保留，不将308秒认定为唯一超时根因。

## 4. Snapshot候选与本地差分

正式Snapshot候选不属于上述固定0f CI候选：仅将完整资源请求构建、隐式`cwd/read`补齐和必需资源额度准入提取为私有
[`_snapshot_resource_requests`](../../../src/harnessix/workspace/snapshot.py#L289-L302)，调用位置仍在原cwd验证之后、逐叶观察之前。
原Resource 256、External Root 16、单文件8MiB、总正文32MiB和单目录10000项等策略保持；`capture_workspace_snapshot`由原136行／复杂度30降为124行／27，策略文件原字节不变。
既有8项原`0f`源码动态加载差分测试基础上，测试设计新增4项直接helper请求序列断言，覆盖POSIX/Windows显式／隐式cwd、混合Location、非字典序和尾补；测试现为12项，6个关联文件最终为58节点、54通过／4个Windows-only跳过、零失败／错误，见`snapshot-related-sequence.xml`。
此前6个关联文件的54节点结果为50通过、4个Windows-only跳过，仅作为首次历史结果保留，不能替代该候选的新Windows原生验收。Snapshot候选修复后的三个完整材料分组已再次并行完成本机回归：authenticated-raw为386节点、367通过／19个Windows-only跳过、27.958秒；
object-input为303节点、299通过／4个Windows-only跳过、63.451秒；cas-reference为617节点、617通过、31.506秒；合计1283通过、23个Windows-only跳过、零失败／错误。
该结果属于Snapshot候选的本机回归，不等于新的Windows原生结果。生产源码SHA-256
`42bf5ef5ae0b4f6c8eec258fa1b757828177b10e41824b44d3b7b8df05693b77`与独立审查一致；
新增顺序测试的后继摘要及闭环结果见`snapshot-sequence-finding-closure.json`，不覆盖独立审查原输入。

可读性策略实际SHA-256为`176ee35bafa474d71f29fe20c484853d6a326ceb2dcf0188193f34b031b75f2c`；18份原最小专项输入重新实测字节不变，
并由`snapshot-source-invariants.json`登记。新安装与升级治理涉及2个文件、41项通过，仅验证门禁正反例，不构成实际安装或升级验收。
相关SDD第四图已完成渲染与视觉检查并通过；438项生产源码mypy通过，本地离线Wheel构建及制品Secret scan通过；这些结果不改变Windows原生门禁或实际安装升级结论。

独立Snapshot审查未发现P0/P1；发现P2测试缺口：最终Snapshot排序可能掩盖请求遍历顺序。补充的4项直接helper序列断言不修改生产源码或原生端口，最终关联回归为58节点、54通过／4个Windows-only跳过、零失败／错误，见`snapshot-related-sequence.xml`。

限定证据位于私有目录`~/Library/Application Support/Harnessix/verification/windows-native-material-acceptance-20261004-v1/`，本记录使用其中的
`snapshot-local-results.json`、`snapshot-current-material-results.json`、`snapshot-related-final.xml`、`snapshot-related-sequence.xml`、`snapshot-source-invariants.json`、
`readability-drift-analysis.json`和`native-jobs-observation-2.json`的有界字段。

## 5. 证据、异常与审查边界

设计在实现前冻结；原RED、完整收集清单、三组日志XML、选择器差分、源码摘要、现行文档与图示
归档于私有windows-native-material-acceptance-20261004-v1，目录0700／文件0600。
只登记原Run／Job／step元数据，不读取业务日志、原始stderr或CDB。
没有模型请求、Keychain／凭据提取、费用规则变更、未知费用释放或旧失败Run重跑。

固定0f候选的三组结果已登记；Snapshot候选尚待新原生验收，不以本机或collect-only成功替代。
若该候选任一新组失败，完整验收仍为NO-GO，保留具体组和未执行范围，不虚构单case结论，不用另外一组成功覆盖失败。
固定0f的原生`PASS`不证明Snapshot候选的原生`PASS`。

## 6. 后继与商用范围

Snapshot候选应通过正常push取得一次与候选源码一致的自动CI，不增加手动最低专项运行。仍需同候选三组、
原NTFS／读取／重启／完整备份恢复及最终同候选门禁证据。
完整Git产品T／Bridge／D／Checkpoint、独立Commit与默认完整Git/Backup v2、消费者Windows11、真实R3、
独立Beta及R1～R6仍开放；R3费用未决，并行验收不缩减原业务目标。

新治理读取当前YAML及冻结Git源码均显式UTF-8，保持跨平台中文步骤比较一致。
首次文档检查实际发现三个语义章节标题不符合规范，修正命名并重新验证；初始发现保存，不覆盖旧资料。
