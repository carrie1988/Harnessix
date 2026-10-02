---
doc_type: validation-evidence
status: current
version: 1
code_revision: 7bbce1033925eaf758e295b3c76fc65dee446f30
owners: [core]
modules: [delivery]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/delivery/test_git_store_schema_v2.py
  - tests/delivery/test_git_store_readonly.py
  - tests/delivery/test_git.py
supersedes: []
---

# GitDB v2精确结构合同：正式验证报告

## 1. 范围与结论

[完整结构详设](../../changes/m09-r4-git-store-v2-schema.md)覆盖背景、架构、流程/时序/数据流、实际接口、
七表全部字段、18phase、组合引用、编码/事务/失败边界、源码及部署回退。
本切片新增一个生产模块和正式测试文件，原v1模块、Store、默认Runtime与原六表/SQL/metadata/payload保持。
实际实现为完整13表、23个PK/UNIQUE内部autoindex、无显式index/view/trigger；checksum仅模块常量，不写metadata。

**结构合同通过；业务迁移、原子产品Writer、认证全前缀Loader、Owner/Key、legacy全集及W1/商用验收均未完成。**
helper只在已有同连接事务中逐条建七表，不升版本、不自行BEGIN/COMMIT/ROLLBACK。
完整13表但metadata仍1是helper允许的迁移中态，不是verifier允许的正式v2结果；旧默认继续拒绝v2。

## 2. 固定源码与实际运行环境

- 生产SHA：`1036cd0295866e0198ef675fb9bf8838d8bc41586e2522ef1536bddd5f3275fe`，239行。
- 正式测试SHA：`56726dcb990e41bf8d2e62070e95ca6cbce41cd78b37c8877a23839d35575c5c`，600行。
- 历史编码诊断SHA：`b3844608647f57b67a38607d8a63232d709fb8d7ffb8d226872d2e7e3102c279`，36行。
- 唯一13DDL checksum：`85f25eab50c7e3ed2a6575e2b177faf38a312915e527b19b5cac4aa6990f7cbf`。

[source-freeze.json](source-freeze.json)固定输入；[schema-v2.ddl.sql](schema-v2.ddl.sql)直接投影模块常量。
基线HEAD为7bbce1033925eaf758e295b3c76fc65dee446f30，不把该提交当作新增未发布模块的身份。
实际运行CPython3.12.7、SQLite3.45.3，来自已有独立虚拟环境，非裸python3；
实际导入本工作树源码见[runtime.json](runtime.json)。无安装wheel或原生Windows/Linux结果。

[inputs-before.json](inputs-before.json)是初始历史输入快照，其中implementation_present=false和schema_field_decisions_pending记录当时状态，
不是当前实现结论；该快照与初始缺API RED不删除、不伪装为GREEN。
[inputs-after.json](inputs-after.json)显示所列原源码/规范/锁文件7项前后SHA全部相等。
当前实现和字段决定已完成的状态由[SUMMARY.json](SUMMARY.json)与[facts.json](facts.json)明确记录。
所选输入没有漂移不等于全仓审计；未覆盖并行非本切片变更。

## 3. 最终真实测试与静态验证

| 组 | 独立实际数目 | 结果 |
|---|---:|---|
| 新增正式结构测试（含UTF-16回归4项） | 363 | 通过 |
| 原git_store_readonly | 85 | 通过 |
| 原git | 12 | 通过 |
| 最终组合 | **460** | **零失败、零错误** |

最终正式证据为[current.log](current.log)与[current.xml](current.xml)：主组363+原97=460，未选择docs诊断。
UTF-16四例归入tests/delivery/test_git_store_schema_v2.py，纳入pyproject默认testpaths=tests；
复用原_v1及唯一模块DDL，不另抄DDL，并断言拒绝后完整serialize/total_changes/事务状态不变。
[current-encoding.xml](current-encoding.xml)为正式四项子集验证，不重复累计到460。
旧[final-all-02.xml](final-all-02.xml)仅记录359主组+4外部诊断+97；旧[final-related.xml](final-related.xml)为456项，
旧[final-all.xml](final-all.xml)为459通过/1失败。这些均属于编码回归迁入tests前的旧测试版本，不是当前test SHA结果；
其中旧final-all-02组合对应06719255，较早456/459记录不能统一归为该SHA。
[encoding-final.xml](encoding-final.xml)及docs诊断仅为历史证据，不充作正常CI回归。
[全部运行表](test-runs.json)区分历史运行及最终组合，不累加中间GREEN。
核心两个文件[Ruff/格式通过](ruff-current.log)，新增生产模块[Mypy通过](mypy-current.log)，
历史诊断文件独立[Ruff/格式通过](ruff-diagnostic-final.log)，不属于当前正式测试选择。
准确参数、环境变量、日志/XML对应及限制见[verification.json](verification.json)。

真实SQLite覆盖：精确13表/全部新列/23内部索引，STRICT/NOT NULL/UUID长度/SHA/整数/18phase/五kind，
真实PK/UNIQUE/组合FK插入负例，多Inventory及未来D意图不被误约束，Seal 1..4096B，
五个payload UTF-8多字节超64MiB及恰64MiB Anchor，body_bytes一致性，
原v1默认/拒v2，同事务幂等/中态/回滚，DDL中途失败/取消identity，
真实只读文件字节/成员不变、零业务读取及metadata VIEW拒绝前函数零调用。
SQL正确不证明Python规范UUID、actual bool/subclass、JSON canonical、认证Seal或合法状态转移。

## 4. 原RED及最小修复

| 历史档案 | 实际原结果 | 当前处理 |
|---|---|---|
| [red.log](red.log) / [red.xml](red.xml) | 缺API collection ERROR 1，退出2；0功能例执行 | 保留历史，不作为功能负例通过 |
| [implementation-01.log](implementation-01.log) / [implementation-01.xml](implementation-01.xml) | 346例：344通过、2失败 | singleton改为真实拒NULL的INT PK；FK负例隔离其他CHECK |
| [metadata-order-red.log](metadata-order-red.log) / [metadata-order-red.xml](metadata-order-red.xml) | helper/verifier各最终corrupt但VIEW函数已调用1次 | 先temp/main精确结构；变形对象拒绝，当前两路径均0次 |
| [encoding-red.log](encoding-red.log) / [encoding-red.xml](encoding-red.xml) | UTF-16两端序helper错误接受，2失败 | 只读编码门禁；当前helper/verifier×两端序共4例拒绝 |
| [final-all.log](final-all.log) / [final-all.xml](final-all.xml) | 459通过、1失败，原trace断言未允许编码PRAGMA | 断言仅接受SELECT或精确PRAGMA main.encoding，最终460通过 |

metadata负例以真实v1建库、rename metadata、同名VIEW调用注册函数复现。
[metadata-order-red.json](metadata-order-red.json)保存原source/test SHA与实际调用次数；
原冻结[source-freeze-before-order-fix.json](source-freeze-before-order-fix.json)保留；
[source-freeze-before-ci-regression.json](source-freeze-before-ci-regression.json)记录旧06719255和外部诊断阶段。
不以LIMIT或fetchmany代替结构信任顺序，原v1模块未改。

UTF-16负例使用`汉`重复`67108864//3+1`次，UTF-8超过64MiB但数据库CAST计数更小。
本修复只要求v2数据库UTF-8，不改DDL/checksum、不转换旧库；不降低旧业务64MiB/8MiB合同。
该次编码RED证明当时加载实现的缺口，不声称所有临时测试输入在执行前已完整封存。

公开日志/XML是仅移除个人环境绝对路径的投影，历史失败、异常和计数未改。
[archive-projections.json](archive-projections.json)记录原字节SHA与公开投影SHA；
不把脱敏投影宣称为原字节档案。不包含受限资料原路径/正文、模型正文、API配置或凭据。

## 5. 四图实际渲染与视觉验收

使用已有Mermaid CLI/已安装Chrome，白底、scale2、viewport1100、PingFang SC、htmlLabels=false；无依赖安装。
四图均实际渲染exit0，并逐图调用view_image验收：文字未裁切、中文可读、箭头/分支完整、边框不截字。
初版出现字面换行标记，已仅修订图源并重渲染；初版四MMD/PNG保留于render-attempts/01。
当前SDD inline与以下四个MMD逐字一致，不把渲染意图标为视觉完成。

| 图 | 冻结图源 | 实际PNG | 尺寸 | 视觉判定 |
|---|---|---|---|---|
| 架构 | [architecture.mmd](architecture.mmd) | [architecture.png](architecture.png) | 1438×1482 | 通过 |
| 流程 | [flow.mmd](flow.mmd) | [flow.png](flow.png) | 2078×4352 | 通过 |
| 时序 | [sequence.mmd](sequence.mmd) | [sequence.png](sequence.png) | 1576×2368 | 通过 |
| 数据流 | [data-flow.mmd](data-flow.mmd) | [data-flow.png](data-flow.png) | 1612×2080 | 通过 |

逐图图源/PNG SHA、渲染日志及实际视觉检查项见[visual-verification.json](visual-verification.json)。

## 6. 交付、复核与限制

[manifest.json](manifest.json)列出本公开目录全部正式文件、两个新增源码/测试及SDD的实际SHA/尺寸，排除manifest自身。
[review-packet.json](review-packet.json)提供变更范围、复现入口、测试映射、剩余产品前置与有限结论。
[文档检查](document-check.json)仅验证本SDD/README的YAML、规范章节、相对链接、图源同源及公开路径约束；不是全仓门禁。
原失败档案及脱敏身份均纳入manifest；本次临时SQLite/Git fixture、缓存与Chrome运行目录已清理，无递归chmod。

未运行全仓回归、CI、网络、模型、Docker、Keychain、真实业务迁移或真实预算账本写入；无commit/push。
新结构不授予权限、Owner、认证或恢复能力，不启用默认v2，不补签业务行。
后继完整GitDB产品仍须真实Owner/FK连接、合法领域FSM、原子Writer、认证完整Loader、legacy全集、跨库和对象材料验真。
结构通过不能据此关闭W1、R4业务备份、全Git产品或商用1.0验收。
