---
doc_type: validation-evidence
status: current
version: 1
code_revision: 8162953c80035ffea1cb7b9f6fc23995e3d2bbfb
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

## 3. 证据、异常与审查边界

设计在实现前冻结；原RED、完整收集清单、三组日志XML、选择器差分、源码摘要、现行文档与图示
归档于私有windows-native-material-acceptance-20261004-v1，目录0700／文件0600。
只登记原Run／Job／step元数据，不读取业务日志、原始stderr或CDB。
没有模型请求、Keychain／凭据提取、费用规则变更、未知费用释放或旧失败Run重跑。

新候选尚待实际原生验收，不以本机或collect-only成功替代。若任一新组失败，完整验收仍为NO-GO，
保留具体组和未执行范围，不虚构单case结论，不用另外一组成功覆盖失败。

## 4. 后继与商用范围

新候选通过正常push触发一次自动CI，不增加手动最低专项运行。需要实际三组、
原NTFS／读取／重启／完整备份恢复及最终同候选门禁证据。
完整Git产品T／Bridge／D／Checkpoint与独立Commit、Backup v2、消费者Windows11、真实R3、
独立Beta及R1～R6仍开放；并行验收不缩减原业务目标。

新治理读取当前YAML及冻结Git源码均显式UTF-8，保持跨平台中文步骤比较一致。
首次文档检查实际发现三个语义章节标题不符合规范，修正命名并重新验证；初始发现保存，不覆盖旧资料。
