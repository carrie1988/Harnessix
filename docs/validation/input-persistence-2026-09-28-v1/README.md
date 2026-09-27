---
doc_type: validation-evidence
status: current
version: 1
code_revision: 246b353337fbf9c9a625a2a310159fa8d4e04f51
owners: [core]
modules: [agent, app_server, protocol, session, secrets]
related_adrs:
  - docs/adr/0099-input-persistence-and-command-publication.md
  - docs/adr/0098-model-stream-publication-boundary.md
related_tests:
  - tests/agent/test_input_publication_runtime.py
  - tests/app_server/test_command_publication.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# 用户输入持久前与命令回执保护验收报告

## 1. 范围与总体结论

[完整详细设计](../../changes/m09-4a-input-persistence-boundary.md)包含需求背景、总体架构、流程图、正常/失败时序、
数据流、接口/类/字段、核心伪代码、取消/失败/恢复及固定源码符号导航。
原用户字段在Session及批准副作用前检查，命令原参数在Claim前检查，原缓存与新结果在公开前检查。
保留原文、身份、Hash、Schema、问答五事件和原CAS；不构建第二个Agent Loop或服务。

本机验收固定macOS、Python 3.13、实际Runtime/SQLite/AgentClient；Provider为确定性替身。
该切片已完成对应本地合同与回归，不关闭0.9.4a、0.9.4、0.9或正式商用发布。

## 2. 固定源码、测试树与版本等价

- 固定实现及全部源码/测试输入：`246b353337fbf9c9a625a2a310159fa8d4e04f51`。
- 全量测试提交：`35c1c1eaff7bf81fa2291c792b9cea4c993c72b6`；树：`20bac84523b198bbdc0431d3a9e7ace91a26f924`。
- 运行前后2378个受跟踪文件及Git状态逐项保持一致，未在全量运行中改动文件。
- 1306个源码、测试、合同、脚本、治理及构建输入与固定实现完全相同；后续冻结只增加文档事实。
- 36个核心源输入在[bundle-manifest](bundle-manifest.json)绑定固定Git字节；Manifest不递归Hash自身。
- 683个既有验证文件原字节保持不变，排除允许更新的总索引`docs/validation/README.md`。
- 未跟踪的用户安全草稿不读取、不修改、不暂存、不计入pytest、Ruff或干净构建。

## 3. 独立旧版负例与整改观察

同一探针在干净`45b801a0db99764b4ddcefa5da83a6ec9a12387e`归档及固定实现执行，
验证实际模块来源，不通过关闭新保护器模拟旧版本。

| 路径 | 旧版观察 | 固定实现观察 |
|---|---|---|
| 直接prompt | Provider为0但新Turn已创建，Session/SDK Replay/SQLite含已登记值，序列＋9 | public_input_secret_leak；无Turn、无序列增加、Provider为0，原Thread不变。 |
| SDK request_id | 请求回执、Session、Replay和SQLite含值；Provider替身为1，序列＋11 | Claim前拒绝；无回执、无Turn、原Thread不变、Provider为0。 |

原始请求或合成值不写入报告，仅记录布尔结果、源码提交和探针摘要；
事实见[contract-facts](contract-facts.json)。未执行真实网络模型、完整产品Root或实际容器。

## 4. 测试矩阵和计数口径

| 组 | 检查面和判据 |
|---|---|
| 直接/延后接受及Retry | 原文/请求身份/Trace、原值/Base64、零新接受、活跃任务映射排空。 |
| 生命周期 | workspace、archive reason、fork request_id、当前登记值原Fork快照；拒绝不复制或归档。 |
| Steering与Question | 拒绝保持原等待；安全原文/幂等；回答与Tool Result恰好五事件同事务。 |
| 普通与Trusted批准 | actor/reason/fingerprint检查先于决定、Action审计和Owner，原文件与调用数不变。 |
| 资源和失败 | Scope关闭、实际UTF8超过1MiB、工作预算、宿主异常、期限、Token及父Task取消。 |
| 命令回执 | 完整参数拒绝时无Store访问；缓存拒绝不改写历史；新结果及诊断原DTO保护先于存储。 |
| 恢复 | 安全丢失响应后同身份原结果重放；跨Runtime重启；参数冲突不覆盖completed事实。 |
| 独立开放边界 | 两项原始RPC元数据观察；Scope关闭后SDK cancel拒绝而直接Runtime.cancel仍可结算。 |

专项**156 passed / 6.92秒**＝57新增功能合同＋99既有；另新增2项治理测试。
新增功能合同包含2项开放原始Protocol缺口观察，不把它们算成安全通过。
相关回归**1358 passed / 46.93秒**执行早于最后UTF8字节回归，不冒充最终验收。
全量**4969 passed / 32 skipped / 408.69秒**；专项、相关和全量互有重叠，禁止加总。
证据治理专项**24 passed**，固定历史和新目录逐字节核验。

## 5. 构建与实际发行物消费

干净固定Git归档构建Wheel/sdist；两者均不包含用户未跟踪安全草稿，原代码与Wheel对应模块逐字节一致。

| 发行物 | SHA-256 |
|---|---|
| Wheel | `1e7774ad921d500be164df71d4d53a078bb60e3eb0b734c773b9ea6772b088fe` |
| sdist | `5a08ca3e87d3286ca60cf69cfa1a84bfb03e26e564b82e286ca68cd6ded87496` |

`python -I`将Wheel优先作为唯一Harnessix代码来源，检查五个模块的实际来源，未导入项目源码或测试。
真实Runtime→SQLite→SDK链拒绝输入与敏感request_id；安全任务仍completed且原中文结果可Replay。
真实网络模型请求为0（替身安全响应消费1次），没有网络、完整产品启动或三平台安装声明。
外部依赖复用本机环境；该结果不证明干净机器安装或可复现构建。

## 6. 门禁、设计可视化与CI

Ruff、格式、345个源码文件Mypy、原可读性阈值、源码差异文档门禁、合同、任务包、SBOM、
Secret自检、干净构建与发行物扫描通过。五幅新Mermaid图真实渲染且逐幅视觉检查，不重渲染历史图。
许可证门禁仍失败12项，不宣称make check或整体CI绿色。

[ci-observation](ci-observation.json)记录冻结时本固定实现尚未作为远端HEAD验收；
历史成功不自动继承，不为每个本地提交等待CI，批量推送后另行读取准确HEAD的有界快照。

## 7. 风险、发布结论与后续治理

独立从相同Wheel实际运行Server观察：initialize响应会回显敏感JSON-RPC id，非法参数键可进入错误path。
[合同事实](contract-facts.json)保存两个已观察出口及来源Hash；这是风险证据，不是未复现猜测或安全成功。

以下仍独立开放：原始Protocol出口、旧Session授权、跨重启Seal、全部Provider凭据、Scope丢失后SDK cancel可用性、
Owner同步阻塞/Store归属、TM攻击场景、远程MCP身份、12项Archive权利、三平台真实安装/升级/Beta及真实Provider成本。
[review-packet](review-packet.json)的发布结论仍为release_blocked，整体0.9未完成。

## 8. 单一交付目录

本README与[Manifest](bundle-manifest.json)、[合同事实](contract-facts.json)、[验证记录](verification.json)、
[评审包](review-packet.json)、[CI观察](ci-observation.json)构成六文件交付，不散布正式证据。
