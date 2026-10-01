---
doc_type: validation-evidence
status: current
version: 1
code_revision: 310874c19c1af502bec22b4fe86966d230d52fb9
owners: [core]
modules: [delivery]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/delivery/test_git_material_cas.py
  - tests/delivery/test_cas_write_authority.py
  - tests/product_config/test_git_material_cas_integration.py
  - tests/product_config/test_git_material_input.py
  - tests/benchmarks/test_soak_restart_child.py
  - tests/benchmarks/test_soak_restart.py
supersedes: []
---

# Git 完整对象原 CAS 持久化与关联失败整改验证包

## 1. 结论与证据范围

本包记录完整单对象原 CAS 类型适配，以及只读文件副作用、重启 ACK 发布竞态和 Windows 夹具创建顺序整改。
实际实现与[总体及详细设计](../../changes/m09-r4-git-material-cas.md)对应；
[完整 Git 业务设计](../../changes/m09-r4-git-delivery-business-backup-closure.md)仍为业务闭包规划。

基线提交不是新增实现提交；实际代码、测试、脚本及配置以1237件完整输入目录及最终 Wheel 字节绑定。
早期不完整目录漏一件已有 C0 退出测试，原目录已保留；完整目录包含该文件，共同1236项字节全部不变。
不把缺项清单或前候选结果套为完整最终候选。

最终本机关联、源码外双 Python、独立审查和治理结果以
[Facts](facts.json)、[Verification](verification.json)、[Review Packet](review-packet.json)为准。
公开资产由[Manifest](manifest.json)逐字节校验；包含本包的提交树必须与实际代码输入目录相同。
任何单机跳过、静态审查或安装成功都不等于 Windows 消费者或完整商业产品通过。

## 2. 已实现内容与未实现业务

- 原 CAS 新入口完整持久化正文，文件及原目录同步、完整回读后返回七字段引用。
- 类型引用核对 blob／tree／commit、sha1／sha256、Git OID、正文 SHA、长度及唯一 CAS 地址。
- 原每对象8MiB保持；实际 CAS → 原批准 Owner 写 → 新批准 Git 完整回读 → 只读 CAS 重开覆盖全部组合。
- Store 全部写入口先拒绝只读／closed，避免 SQL 拒绝前已落正文。
- 原完整回读及耐久确认提取到唯一 IO 模块，Store 原门面与原326逻辑行护栏保持，不放宽600／100／20策略。
- 重启夹具只在完整写入／同步／关闭后原子不覆盖发布，读侧严格单读、硬退出和阈值不变。
- Windows 材料夹具在原 Runner 前按正式合同创建三个私有目录，不改生产 DACL 或权限检查。

未实现对象图、对象角色／历史范围、总量容量、MAC 业务目录、GitDB／Backup v2、
默认 Commit／Checkpoint、双工作树与新根重绑。引用不是来源认证、Owner 回执或批准。
R3真实质量、Windows消费者、Beta及R1～R6仍开放。

## 3. 同一最终候选的实际结果

| 范围 | 总数 | 通过 | 失败／错误 | 跳过 | 秒 |
|---|---:|---:|---:|---:|---:|
| Delivery／Process／产品 Git／备份恢复／重启关联 | 1332 | 1274 | 0 | 58 | 234.698 |
| 全部 Benchmark 回归 | 268 | 267 | 0 | 1 | 50.990 |
| 同一 Wheel 源码外 Python3.12 | 498 | 496 | 0 | 2 | 104.176 |
| 同一 Wheel 源码外 Python3.13 | 498 | 496 | 0 | 2 | 85.073 |
| 治理回归 | 302 | 302 | 0 | 0 | 28.534 |

集合重叠，不相加。58、1或2项跳过不是 Windows 原生通过。
早期1330关联、496安装选择器及修复前 Wheel 均保留为前候选，不套最终字节。
最终1237输入身份为 `977cd8f0bd6e66481498f09ed7a8aa1db922ecb47d0be44f723e6feb4d512d84`。
实际最终 Wheel SHA256 为 `c46930f48c2473d2245c4d7cab22713d2800283605307f3a4a608a5298b48468`；
462个包成员、421个 Python 模块与源码及两次实际安装逐字节匹配，原 RECORD 同时核验。
源码外 import 审计确认没有从仓库 src 导入，运行后1237输入仍全部相同。
该安装范围不等于消费者依赖锁定、Windows11真实编码或完整R4验收。

独立审查发现的 CAS 临时清理权缺陷已按最终三个源文件 SHA 确认关闭，
审查者未运行测试，只核验既有实际2 FAIL／130 PASS原件，不冒充独立OS验收。
Ruff、421模块 Mypy、原可读性策略、合同漂移及文档门禁通过；
实际最终 Wheel 和仓库的 Secret 扫描与自检分别执行，结果在结构化 Verification 中记录。

## 4. 真实失败与修复依据

| 原失败 | 实际证据及处置 |
|---|---|
| 只读 save／_put_blob 仍能创建正文 | 原生产源码两项确定性 FAIL；写准入前置后原案例通过；四写入口与 closed 均覆盖 |
| CAS 排他临时创建失败却删除原文件 | 原公开 Store／类型适配两项实际 FAIL；仅取得排他创建所有权后清理，陌生 inode／正文保持 |
| ACK 最终名在 os.write 前可见为空 | 原源码真实写屏障1 FAIL；新发布器不提前暴露最终名，短写、同步、关闭、链接及坏 ACK 单读均验证 |
| Windows 私有材料入口23项失败 | 原固定310874c作业23 failed／653 passed／6 skipped；日志共用Runner私有目录拒绝路径；测试先正式创建、后原 Runner |
| Store 既有热点长度增长 | 原静态检查354大于326；提取唯一原 CAS IO，策略不修改，最终报告同步 |
| 文档链接开发检查 | 两项缺失链接原日志保留，后继恢复实际文件名，正式文档门禁重新验证 |

[原 CI](https://github.com/carrie1988/Harnessix/actions/runs/36794675786)终态为失败：
macOS重启专项1 failed／242 passed／1 skipped；Windows材料步骤上述失败；
Python3.12／3.13在许可证检查阶段失败，文档及Container成功。
Linux许可证失败不被改为功能测试失败，也不通过降低门禁掩盖；本包不整改或关闭R2。
原失败保留，新原生结果必须绑定新的实现提交。

## 5. 输入、测试与制品的关联方法

1. 按路径、字节数、SHA冻结源码／测试／脚本／配置，回归及打包后复读；不能只绑定三个新文件。
2. 关联回归保留原 Delivery、Process、产品 Git、完整备份恢复及两个重启测试文件。
3. 原 C0 六文件与新增 CAS、写准入及真实链在源码外双 Python 执行，同一 Wheel 逐成员匹配源码及安装文件。
4. 新测试集合和关联集合有重叠，不相加；skip不计适用平台通过；私有原日志与JUnit只发布低敏摘要。
5. 完整设计含架构、流程、时序、数据流、字段、伪代码、失败恢复、安全、兼容及源码／测试映射。
6. 五份 Mermaid 已实际渲染为 PNG 并逐图查看，不以静态语法检查代替图示可读性。

## 6. 剩余验收边界

- 新候选 Windows／macOS／Linux原生结果尚未由本包取得，不继承旧候选绿色作业。
- Windows 文件同步和普通重开测试不证明断电后的目录耐久；原非POSIX目录同步边界保持。
- 原 CAS 与内容引用不提供业务登记／阶段 Journal／孤儿 GC；硬崩溃恢复由后继业务闭包实现。
- 此切片新增模型请求为0，不修改真实费用账本，不生成新的R3成绩。
- 未完成完整 Git 产品、消费者环境、独立Beta及同候选总发布门禁之前，内部rc版本不得称正式商用发布。
