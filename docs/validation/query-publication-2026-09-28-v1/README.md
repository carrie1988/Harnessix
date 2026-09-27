---
doc_type: validation-evidence
status: current
version: 1
code_revision: 2ae7caf3e2a6b53431ef352ff3520f3cf1358282
owners: [core]
modules: [app_server, agent, protocol, secrets, session, product_config]
related_adrs:
  - docs/adr/0101-query-publication-and-session-host-binding.md
related_tests:
  - tests/app_server/test_query_publication.py
  - tests/product_config/test_query_publication_root.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# 导出查询原DTO与Session宿主版本绑定验证报告

## 1. 结论与完成边界

固定实现`2ae7caf3e2a6b53431ef352ff3520f3cf1358282`完成六查询的原Params准入、完整原DTO与稳定错误保护、恢复调度后置和构造同一Session对象绑定。
原方法签名、模块导出、DTO/Hash/Protocol Schema、一级依赖及环不变；无数据库迁移或新网络服务。
直接Python查询的底层稳定异常统一为`AgentServiceError`；未知异常采用固定子类型，JSON-RPC保持原`-32603`分类。

完整回归**5097 passed / 32 skipped / 405.0秒**；专项**181项 / 5.18秒**，
其中62项新增功能/观察与119项既有回归。新增61项查询测试含1项未登记旧历史开放观察，另1项实际默认Root直调验证、2项证据治理。
相关矩阵2302项通过；完整矩阵包含这些测试，不叠加计算独立总数。双quiet相关输出未记录耗时，不推算数值。
真实模型/网络调用0次，不消耗模型预算。

本切片本地验证完成，**0.9.4a与整个0.9未完成，产品发布仍阻塞**。当前Scope材料检查不授予旧历史或跨重启授权，
Session对象绑定不证明Tenant身份、SQLite物理归属或自定义Request Store身份；当前成功不能替代后续发布矩阵。

## 2. Manifest与固定运行输入

- 固定源码`2ae7caf3e2a6b53431ef352ff3520f3cf1358282`；完整测试运行提交`05cadd2dba69a9f02679d9f72ae07bdb6c1bd5ff`，树`190fd94b5f6a6eee05ad61b4508b6fdaaeca9d5d`。
- 运行期间2401个跟踪文件逐项Hash和Git状态不变，没有边执行完整矩阵边修改代码或资料。
- 1328个源码、测试、合同、脚本、治理和构建输入与固定实现逐字节一致；最终文档冻结不改变执行输入。
- 37个关键输入绑定固定Git对象；六文件Manifest不递归Hash自身，另外五文件逐字节核对。
- 695个既有验证文件原字节不变，排除允许更新的总索引。
- 未跟踪安全草稿不读取、不修改、不暂存，不计入pytest、Ruff或清洁Git发行物。

## 3. 独立真实旧基线与同脚本整改

探针分别在清洁`d105d6212b374c20ffef5b20d95cc2de6a88d48f`与固定实现Git归档执行，校验实际导入模块来源与同一脚本Hash。
真实Runtime/SQLite/正式导出Service产生原DTO，没有Transport Guard代理；仅保存状态、布尔结果和Hash，不保存材料、旧正文或原请求。

| 路径 | 旧基线 | 固定实现 |
|---|---|---|
| get/list/resume/replay/next | 五个原DTO均公开当前已登记材料 | 五个均在返回前public_output_secret_leak，当前Provider请求为0，原历史不变。 |
| Runtime/Store不同对象 | 接受装配 | 固定ValueError先于订阅拒绝。 |
| 同一路径另一Store对象 | 接受装配 | 拒绝；路径相等不推导同一生命周期。 |
| 换版本未登记旧历史 | 原旧材料可读 | **仍开放**，不能识别未登记值，不计历史授权成功。 |

原私有历史故意包含合成材料。拒绝公开不会清洗、删除、改写或重新授权这些事实。
单元矩阵另覆盖Reader绑定错配，但独立对比探针仅对上述两个Runtime/Store装配出具结果，不混淆证据规模。

## 4. 查询、错误与恢复测试矩阵

| 组 | 判据 |
|---|---|
| 五查询×raw/Base64 | 完整原DTO拒绝，Session事实不变，不新增Task或Provider消费。 |
| 六类输入拒绝 | 完整原Params在Store/Reader/Runtime.resume与信号注册前检查；失效Scope、敏感UUID/cursor无IO。 |
| 原安全DTO | 原字段、原Thread身份及私有事实保持，不以替换或过滤伪造安全结果。 |
| 稳定/意外错误 | Kernel原code/message、Service与意外ValueError均不公开登记材料；安全retryable与原分类保留。 |
| 非法构造模型/Live选项 | warnings=error固定拒绝，不打印含字段原值的Pydantic诊断；非原生bool不注册Live信号。 |
| Live Delta/Artifact | 最终完整原候选检查；不重写引用、Hash或正文，不承诺Delta无损重读。 |
| 三类装配错配 | 不同Session、同路径另一实例、Reader另一Session均先于Delta订阅拒绝。 |
| 原接受Turn恢复 | 输出拒绝、父取消、超时、关闭竞态不调度；安全结果后恰好恢复原Turn，不重复模型消费。 |
| 输入/输出五故障 | 失效、限额、超时、扩展异常与父取消；分别零IO或已读一次，不改持久事实。 |
| 实际空长轮询 | 原Session读取及Event同步，无固定sleep；关闭、取消、到期都有界终止。 |
| 已closed命令回执 | 保留既有completed重放合同，但_spawn不再创建Task，原accepted不是执行保证。 |
| 未登记旧历史 | 测试通过只证明风险开放，不作为授权或发布通过。 |

Artifact专项使用真实Scoped Reader外壳与原DTO，但页读取是单元替身；不能当作实际Artifact存储或跨重启验收。
已有实际Artifact、CLI、传输与SDK回归进入相关/完整矩阵，仍分别按其原固定场景判据解释。

## 5. 默认产品组合根与隔离Wheel消费

默认Root验证实际配置、Scope、SQLite、默认Action与只读宿主装配，仅模型工厂和stdio驱动是替身。
直接调用五个Service查询，无Transport代理，原历史不变、不接受新Turn、不创建后台Task，当前Provider消费0次；退出清零Scope持有的可变材料副本，不承诺Python不可变副本全部擦除。
私有数据库仍包含预置旧合成材料，这一不变事实不是凭据已从历史删除的证据。该用例不宣称真实OS管道或网络模型验收。

清洁固定Git归档构建Wheel与sdist；三份变化执行模块与固定Git字节一致，候选发行物Secret扫描通过。
这些是固定源码测试候选，sdist文档为全量冻结前状态，不作为最终正式发行或完整文档安装交付。

| 候选发行物 | SHA-256 |
|---|---|
| Wheel | `cce831dd491f8d351da8c168d481a6c0fc32b1d5d271fe8e0a1657ea64aef784` |
| sdist | `2d33c99bf7eca20e6380e8c357b4d9e2d5ba4984c75cfff943785582ac8fb48b` |

隔离模式`python -I`从压缩Wheel导入原Runtime、公开保护、新查询边界、Service、Server等六模块，断言所有实际导入路径。
不使用项目源码或测试助手；真实SQLite和导出Service拒绝五查询、拒绝错误Store装配并保留私有历史。
独立安全Session的五个原DTO、SDK Replay与稳定不存在Thread错误通过。确定性Provider消费1次，真实模型0次。
复用本机依赖环境，不声明干净机器安装、可复现构建、真实stdio/network或三平台发布验收。

## 6. 详细设计、六幅图与质量门禁

[详细设计](../../changes/m09-4a-query-publication-boundary.md)覆盖需求背景、源码研究、目标、选型、总体架构、核心流程、
正常与失败时序、数据流、重点类/接口/字段、伪代码、错误/取消/超时/恢复、安全/部署/回滚和固定源码行导航。
ADR、总体架构、四份模块设计及路线图同步。五幅新详设图与一幅更新的模块图真实渲染、逐幅视觉核验，记录源与PNG Hash。
Ruff、格式、Mypy348份源码、可读性、文档、Schema、任务包、SBOM、Secret自检通过；原结构门禁阈值不放宽。
许可证门禁仍因**12件Archive权利**失败，不豁免、不改判据、不称make check完整成功。

## 7. Review Packet、CI及剩余发布阻塞

[verification](verification.json)区分完整、专项、发行物和门禁；[Manifest](bundle-manifest.json)绑定固定输入及五份原文件；
[Review Packet](review-packet.json)继续release_blocked；[CI快照](ci-observation.json)在冻结时未推送当前切片。
既有36343882845运行的开工一次快照为in_progress，不冒充其最终状态或当前修订通过。
本地多个提交合并一次推送，CI后台运行；最终head取一次有界快照，不逐提交等待或循环轮询。

剩余：旧Session授权/内部Store聚合权限/跨重启Seal、全部Provider材料、Scope失效SDK相关ID、
Owner同步阻塞与Store归属、Request Store物理身份、12项Archive权利、编号威胁测试、远程MCP身份与受管出口、
三平台真实安装/升级/Beta及真实Provider能力/成本。同步恶意宿主硬抢占、任意DLP、所有编码和多租户安全均未宣称完成。
