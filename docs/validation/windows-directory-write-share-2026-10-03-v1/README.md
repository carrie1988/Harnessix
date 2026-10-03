---
doc_type: validation-evidence
status: current
version: 3
code_revision: 0583b53306f3ab869fb35c5b9eece80fbe2251a4
owners: [core]
modules: [delivery, product_config, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_material_snapshot_differential.py
  - tests/product_config/test_git_material_native.py
  - tests/governance/test_git_material_snapshot_differential.py
  - tests/governance/test_windows_git_native_failure_projection.py
supersedes: []
---

# Windows目录写共享兼容与保护验证

## 1. 背景、目标与结论边界

[前置同源原生控制](../windows-existing-fanout-control-2026-10-03-v1/README.md)已取得真实目录共享反例：
原share1下链接被Win32错误32拒绝，仅增加WRITE共享后成功。该包原SDK仍失败，不改写历史结果。
本包落实[总体与详细设计第15节](../../changes/m09-r4-windows-minimum-commit-probe.md#15-windows目录写共享兼容与原保护保留)，
只在原_open用_held_share区分目录3／文件1，两者均禁止DELETE共享。
固定244f9c2原生已取得控制及原保护步骤通过，但SDK加侧车失败，不登记SDK或Windows商用通过。

## 2. 实现、源码与失败语义

- [Windows句柄端口](../../../src/harnessix/delivery/git_material_native_windows.py#L231)：原access、flags、
  类型、reparse、inode、最终路径、私有DACL、单链接和Resources关闭保留；不提升当前句柄写权限。
- [真实控制测试](../../../tests/product_config/test_git_material_snapshot_differential.py)：原四格和同源两臂，
  历史share1明确构造成测试反例，修复后自然share3仍须真实链接及完整读取成功。
- [原Windows安全负例](../../../tests/product_config/test_git_material_native.py#L74)：文件写入、文件和目录改名／删除拒绝，
  metadata-only控制及关闭后的真实正例保持，原测试未修改。
- [治理及别名反例](../../../tests/governance/test_git_material_snapshot_differential.py)：目录／文件共享位，
  copy2内容和hardlink拒绝、三方lstat集合收敛、实际cross-arm文件／目录符号链接拒绝。
- [原worker后验](../../../src/harnessix/delivery/git_material_worker.py#L251)：原snapshot、command和namespace完整复核后才产生原Proof。

显式真实WINFUNCTYPE以use_last_error=True绑定原kernel32，函数地址与原DLL一致；错误读取紧邻失败调用。
两个独立目录打开前核对同一实际目录身份，不声称在同一个HANDLE上原位改变共享模式。
取消、超时、未知效果、完整OID及Owner／批准逻辑没有更改。

## 3. 验证、安全与交付

九件关联文件实际561通过、7跳过、零失败／错误；包含原RO快照、FD归属、私有目录权限、三方别名及原治理。
原两SDK另行实际2通过、零失败／错误／跳过；不与包含范围重复累加，不外推原生。
Windows相关7项跳过单列；四幅变化Mermaid实际渲染并视觉复核通过，Ruff及精确Secret扫描／自检通过。
首次文档检查发现delivery模块未同步，原发现保留；补现行delivery模块设计后再次检查，不绕过门禁。
新手动候选继续运行原四格、同源两臂、三个真实OS控制、原Windows保护及原两SDK。
原四格一分钟、原SDK五分钟、20／45／240／300正式合同保持，新增保护一步一分钟。
十八输入只更新Windows实现与workflow两行，其余16行、固定PE／PDB、13接点及其他字段保持。

限定独立审查的三个P2在测试域分别补三方非别名收敛、显式last-error使能、两个独立打开措辞；
不把证明加固描述为已经修复生产漏洞，也不从保留原校验调用推定安全验收。
原件、设计前置记录、完整图示、Review Packet和来源差分保存于私有windows-directory-write-share-20261003-v1，
目录0700／文件0600。仅读取Run／Job／step元数据，不读取Git、CDB或Job业务日志。
没有模型调用、Keychain／凭据读取、费用规则变更或原失败重跑。

消费者Windows、完整Git交付／Backup v2、真实R3与费用未决、独立Beta和R1～R6继续开放。

## 4. 固定修复候选的实际原生结果及后继路径整改

[Run37132088623](https://github.com/carrie1988/Harnessix/actions/runs/37132088623)、attempt1、Job111229042843，
head244f9c2cac250a856505d9b4043410ef6b197189，终态failure。

| 既有步骤／边界 | 实际结论 |
| --- | --- |
| 固定官方身份仅预检 | success |
| 原hash-direct、hash-held、write-direct、write-held | 四步success |
| 同源fanout／blob单持有配对 | 两步success |
| 实际子文件创建／CreateHardLinkW／历史share1反例 | 三步success |
| 原文件写及文件目录改名删除保护负例 | success |
| 原两个SDK及诊断侧车聚合步骤 | failure，不拆造各case结果 |

目录修复在当前固定控制中取得原生效果与保护证据，不是完整Windows产品、消费者Windows11、
所有历史错误唯一根因或完整Git／Backup v2通过。
独立窄复核在固定244f9c2源码范围未发现P0／P1或新P2；原三个测试证明缺口已在源码及治理范围关闭，
该复核没有执行原生，也不扩大到全部安全或商业验收。

源码另确认原SDK基目录直接位于RUNNER_TEMP，而侧车只从其parent/symbols加载PDB，
与原preflight输出的git-minimum-identity私有根不同。完整诊断缺失仍可将业务成功转换为聚合失败，
因此当前元数据不证明业务case失败或成功。后继按
[详细设计第16节](../../changes/m09-r4-windows-minimum-commit-probe.md#16-原sdk诊断侧车的符号根装配一致性)
仅将SDK基目录改为原preflight根的fresh child，保留全部角色验真、原selector、五分钟及完整性失败门。
治理先RED再GREEN，原失败日志只在私有本机测试归档，不读原生业务日志。
后继新固定候选原生结果见第6节；不重跑旧候选或用路径测试代替原SDK验收。

## 5. SDK符号根修复的本机验证边界

原路径治理断言实际1失败，修复后同断言通过；九件关联文件实际562通过、7个Windows相关跳过，
零失败／错误。原两SDK在POSIX另行2通过；不能与关联范围重复累加为全仓或Windows业务结论。
仅workflow路径与其合同身份变化，其他17件原18输入及预算／角色／selector保持，生产材料源码不变。
文档静态、精确Secret／自检和三个Python文件Ruff检查／格式通过；第16节三幅图实际渲染及视觉复核。
设计前置摘要、RED／GREEN日志XML、源码／身份差分、图示和后继原生元数据归档于
私有windows-sdk-symbol-root-20261003-v1，不覆盖原目录兼容38成员封存包。

## 6. SDK符号根修复的固定原生通过

[Run37134072312](https://github.com/carrie1988/Harnessix/actions/runs/37134072312)、attempt1、Job111234899713，
head0583b53306f3ab869fb35c5b9eece80fbe2251a4，终态success；创建2026-10-03T15:40:28Z，
更新2026-10-03T15:41:28Z。全部原身份、四格、同源、三个真实Windows控制及原安全保护步骤success，
原两个最低SHA256 Commit SDK加诊断侧车步骤success。

工作流仍只选择原两个业务case，并执行原五分钟、Trace2模式和插件。
[原诊断完整门](../../../tests/product_config/git_trace2_projection.py#L377)要求A／B两case、每个13接点、正常teardown、
未截断与完整已知写入Trace2；原sessionfinish不完整失败门保留。没有通过删除侧车或更改失败门获得成功。
本专项完成原材料／CAS写入、独立批准回读及目录共享保护的固定原生验收。

dispatch调用一次；首次列表尚未索引新Run，原宿主观察断言失败保留，后续核对同一Run而非重发或重跑。
仅保存Run／Job／step元数据，不获取原始或业务日志。旧两个failure候选及此前Root UNKNOWN保持，不改写旧结果。

后继必须完成完整对象容量矩阵、默认Git产品交付／Backup v2、消费者Windows11、真实R3及独立Beta。
该专项success不等于R4或R1～R6商用完成；不再为该已闭合路径增加新的诊断采集设施。

独立窄源码复核在固定0583b53未发现可确认P0／P1／P2；20组定向离线校验覆盖同一父根、fresh child清理、
角色绑定失败及原完整性退出路由，不计作完整pytest或独立Windows执行。单条新路径测试只断言身份表达式前缀，
完整后缀一致性另由精确workflow摘要及独立表达式复核证明；该单条用例覆盖边界保留，不宣称全关系覆盖。
同候选常规CI Run37134036729的Windows Job111234779832中，NTFS写链和Git读取／取消步骤成功，
认证raw回执与Git基准聚合步骤failure，后续步骤未据此验收。该聚合包含完整材料矩阵但不能拆造各case结果，
不读取原生业务日志或以最低专项success改写此失败；完整Windows矩阵仍需闭环。
