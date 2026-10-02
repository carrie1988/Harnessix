---
doc_type: validation-evidence
status: current
version: 2
code_revision: 730f0846641700c4c697d7cc6ba03cbf1a8364bc
owners: [core]
modules: [delivery, product_config, processes, governance]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/governance/test_windows_git_native_branch_observation.py
  - tests/governance/test_windows_git_native_branch_preflight_v2.py
supersedes: []
---

# Windows Git 有限前置拒绝诊断 v2 验证包

## 文档摘要

本包记录730发布基线后的有限诊断候选字节及实际离线验证，不是Windows/CDB报告。v1实际前置拒绝不含阶段，不能唯一解释原因；v2只提供首次失败stage、固定reason_code及两个真实presence观察，UNKNOWN和拒绝非0保持。

## 内容与边界

- [完整设计](../../changes/m09-r4-git-native-branch-preflight-v2.md)：架构、流程/时序/数据流、接口、字段、异常/预算、安全及部署回退。
- [SOURCE.json](SOURCE.json)：当前受审源码身份、原45输入保持/必要两脚本演进、六个原控制函数AST及43件零漂移。
- [diagnostic-policy.json](diagnostic-policy.json)：12阶段、38个既有literal reason及UNKNOWN；四字段类型和封闭JSON结构。
- [facts.json](facts.json)、[verification.json](verification.json)：真实RED保留、原50+新增82=132PASS、类型/格式及图形证据。
- [COMMANDS.json](COMMANDS.json)：可复现定点离线命令，不含个人路径或私有正文。
- [review-packet.json](review-packet.json)：精确写集、冻结输入和后继单次原生条件；不是已执行声明。
- [manifest.json](manifest.json)：本包/SDD/演进源码及原只读输入的真实成员SHA，排除自身递归。

原v1及集成包文档、源身份目录、FAIL与原50测试不改；两个必要脚本的新字节在本包单列，不能把旧manifest当当前v2源码身份。原16件输入、官方PE/PDB、硬件断点、两个selector、20/45/240/300秒预算、Owner/Job/RO-DOD/MAC/raw/EOF/protection及两个上传文件不变。

## 实际结果与限制

正式两组132PASS，0失败/错误/跳过；Ruff/format实际10Python文件、mypy strict新模块1件。原字段缺席1FAIL的RED原件保留，初版129PASS不与最终132累加。新回归位于tests，不以docs辅助测试冒充正常CI选择器。

presence只表示原is_file已执行的观察；未执行为null，false不提供唯一根因，true不是工具PE或可信身份通过。泛异常、多参数/子类/跨阶段码均UNKNOWN，不发布异常文本或args动态值。

本轮没有网络/PDB下载、Windows/CDB、原SDK两个selector、模型、Docker、凭据、账本、stage/commit/push/dispatch。mock与合成头只证明有限合同；没有商业或全系统验收通过。四图实际渲染/查看状态、尺寸及源码SHA以verification为准。

## 封包复核

本包与SDD实际通过两件Markdown元数据/相对链接/规范章节/图源检查，21件精确写集Secret扫描零命中；不是全仓DocGate或360治理回归。实际26组测试产生的有限result/SHA对已核字段类型、阶段白名单及真实字节SHA。

四图已由本机既有Chrome渲染并逐图查看：总体架构586×886、预检流程571×1376、调用时序1075×1109、发布数据流541×1309；文字和连线完整，无裁切。图源与SDD四个inline块相同。manifest排除自身，记录63成员：20件演进写集成员（含四PNG）与43件保持不变的原输入；manifest本身为第21件写集。
