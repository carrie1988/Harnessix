---
doc_type: validation-evidence
status: current
version: 1
code_revision: 75c4b7ddfe91824bcb667023c003e4aad18992e4
owners: [core]
modules: [tools, processes, product_config]
related_adrs:
  - docs/adr/0043-git-and-controlled-test-feedback.md
  - docs/adr/0063-windows-v1-platform-support.md
related_tests:
  - tests/tools/test_git.py
  - tests/tools/test_git_platform_contracts.py
  - tests/tools/test_git_delivery_reader.py
  - tests/product_config/test_git_baseline.py
  - tests/product_config/test_product_patch_rollback.py
supersedes: []
---

# 平台测试输入兼容验证

## 1. 结论与变更边界

基线为`5d8216b07415b608e416add233f5cca76e5a09ff`，测试源提交为
`75c4b7ddfe91824bcb667023c003e4aad18992e4`。
Python3.12.7与3.13.8指定Git关联回归各128项通过，Rollback整文件各22项通过，均无失败、错误或跳过。
变化范围为Git读取夹具、Rollback大负载参数标识、原模块文档及本验证目录；生产代码、预算、Schema、锁文件不变。
`code_revision`绑定实际测试源提交；回归运行时的未提交受测字节与该提交逐字节一致，
精确输入同时以文件SHA256和私有快照绑定。文档提交独立于测试源提交。

**本地限定回归通过；原生Windows、R4及商业发布验收不在此证据范围内。**
没有真实模型请求或费用账本操作；测试源提交只含两个测试文件，生产输入保持基线字节不变。
本验证执行不创建项目Git提交，不推送。
测试中的Git写入仅用于私有临时仓库的准备和断言，不修改项目仓库的Index、Ref或HEAD。

## 2. 根因与夹具修正

### 2.1 Git模拟NT输入

旧夹具在模拟NT分支仍提供POSIX临时根。Python3.13在Windows路径语义下不再将
只有一个前导斜杠或反斜杠的路径视为绝对路径，见
[Python官方`os.path.isabs`说明](https://docs.python.org/3.13/library/os.path.html#os.path.isabs)。
旧Python3.13焦点的六个NT用例因此被既有仓库根校验拒绝，六个POSIX用例通过；
这不是原生Windows运行时故障的证明。

[`测试夹具`](../../../tests/tools/test_git_delivery_reader.py)为模拟NT根提供
`PureWindowsPath("C:/workspace/工程")`，观察值采用Git正斜杠与CRLF；POSIX仍使用真实临时路径。
只模拟平台、进程创建端口和输入根，不替换真实`repository_root_matches`或豁免仓库边界。
普通读取与交付读取分别覆盖无盘符、不同盘符、子目录、盘符相对路径、父目录、多余行，
共12个负例；要求`path_denied`并且只运行根查询，不继续配置或状态查询。
中文负例以字符串`.encode()`表达，字节语义不变。
现行平台边界见[Tools模块](../../modules/tools.md#git平台输入模拟与python313兼容验证)。

### 2.2 Rollback大负载参数标识

原Windows CI Job `109857490289`实际终态为FAIL，不能沿用成功状态。
日志中首个错误发生于pytest的`_update_current_test_var`向`os.environ["PYTEST_CURRENT_TEST"]`赋值，
不是生产Rollback事务调用；原两个大bytes参数没有显式ids，收集所得nodeid长度为600129和2400127，
超过日志报告的32767字符环境变量上限。随后setup/teardown错误产生连锁失败；
修复后必须独立获取新原生CI结果，不能由本地通过推断连锁失败已在Windows关闭。

[`Rollback测试`](../../../tests/product_config/test_product_patch_rollback.py)仅为原参数增加
`ids=["large-text", "binary"]`。新nodeid长度为137和133，整文件仍22项，
文本600001字节、二进制600000字节、测试主体及断言不变；收集证据不等于测试执行。
双版本整文件22PASS使用独立验证原件，本专项不重复执行Rollback测试。

## 3. 验证结果与原件

| 原件/阶段 | Python | 通过 | 失败 | 错误/跳过 | 范围 |
|---|---|---:|---:|---:|---|
| `original-python313` | 3.13.8 | 6 | 6 | 0/0 | 原生成查询焦点12项 |
| `fixed-python312` | 3.12.7 | 71 | 0 | 0/0 | Reader 53项及平台合同18项 |
| `fixed-python313` | 3.13.8 | 71 | 0 | 0/0 | Reader 53项及平台合同18项 |
| `regression-python312` | 3.12.7 | 128 | 0 | 0/0 | 指定四文件关联回归 |
| `regression-python313` | 3.13.8 | 128 | 0 | 0/0 | 指定四文件关联回归 |
| `rollback-fixed-python312` | 3.12.7 | 22 | 0 | 0/0 | Rollback整文件独立验证 |
| `rollback-fixed-python313` | 3.13.8 | 22 | 0 | 0/0 | Rollback整文件独立验证 |

最终每版本包括Git读取7项、平台合同18项、Reader 53项、产品交付基准50项。
两版本执行同一组128项，不能当作256个独立场景；旧71项已包含于扩大回归，不重复相加。
旧失败原件保留并校验原始字节SHA256，后继通过不覆盖失败历史。
旧71项原件未记录独立的候选字节绑定，不把其结果冒充最终受测SHA；
最终双版本日志及JUnit均绑定同一新输入快照，并在每次运行前后核验无漂移。

私有原件定位为`Harnessix/verification/git-query-platform-input-20260930-v1`，
最终运行子目录为`closeout-platform-input-20260930T123710Z`。
早期Git关联回归`closeout-20260930T122645Z`原件保留；Rollback辅助文件增加ids后重新绑定并复跑Git回归，
旧输入快照不冒充新候选。Windows原日志及Rollback收集/测试原件位于
`Harnessix/verification/provider-engineering-20260930-v5`，以事实文件分别绑定。
旧文件和新日志、JUnit、机器记录均保留在私有目录，不复制临时路径或原始异常正文到公开资料。
目录0700，日志及JUnit 0600；测试子进程使用umask022、隔离HOME、禁用字节码及pytest缓存。
Git为2.53.0，Python包来源确认为受测工作区的`src/harnessix`。

## 4. 门禁、完整性与审阅

[事实](facts.json)记录运行环境、实际计数、输入SHA256、原FAIL与新PASS原件及门禁结果；
[Verification](verification.json)记录有限通过条件与未覆盖范围；
[Review Packet](review-packet.json)提供审阅清单；[Manifest](manifest.json)绑定交付文件。
私有`run.json`保存933个源码、合同、治理及受测文件输入指纹，数量是快照覆盖，不是测试覆盖率。
Manifest不自包含自己的哈希；其余四个验证文件和三个变化文件按最终字节绑定。

必要门禁为变化测试文件的Ruff格式/规则检查、既有Schema生成一致性、项目文档静态及显式变化路径检查、
Secret规则自检和八个交付文件的定向完整扫描。文档检查不发现或遍历主工作区的未跟踪测试。
未修改图，不增加ADR或架构设计图；不宣称重新渲染既有全库Mermaid。
未执行全矩阵、全量回归、发行物构建或全仓Secret复扫。

剩余检查为交付资料审阅与提交；测试源提交绑定已完成。
新原生Windows CI及进程/NTFS/SDK验收保持独立开放。
若受测夹具、生产输入或所列辅助文件变化，原输入绑定失效，应重新执行受影响的限定验证。
