---
doc_type: validation-evidence
status: current
version: 1
code_revision: 018a4afabf270bbd94deb4cbb728794615d85225
owners: [core]
modules: [product_config, tools, processes, delivery]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0080-capability-proven-product-action-composition.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_baseline.py
  - tests/tools/test_git_delivery_reader.py
supersedes: []
---

# 产品Git只读基准验证报告

## 1. 结论与验收范围

固定源码候选为`018a4afabf270bbd94deb4cbb728794615d85225`，比较版本为`c4b1177d359b2ff4c81cf0c7ae85bdc9ea0e1539`。
本专项验证原认证Patch来源到Git HEAD的只读基准绑定，不是Commit/Checkpoint产品交付，也不是商用发布验收。
实现、契约、失败语义及部署边界见[完整总体与详细设计](../../changes/m09-r4-product-git-baseline.md)。

| 能力 | 当前事实 |
|---|---|
| 来源与根绑定 | 沿原认证Thread成功Call/Result、Route/Transaction及连续版本链；原生根与Git端口根必须一致，非法来源在任何Git查询前拒绝 |
| 原始版本 | 从固定Commit派生Tree；普通blob完整SHA-256、长度和模式匹配首before，不使用前缀或Git OID代替完整文件摘要 |
| 选中Index保护 | stage0、OID、模式、H标签和debug flags均核对，intent-to-add、assume-unchanged、skip-worktree及冲突拒绝 |
| 无关修改 | 用户无关暂存/未暂存内容不加入交付集合；真实Git专项直接比对用户Index字节与HEAD，确认读取不写入 |
| 漂移、取消与期限 | Git前后观察、最终Snapshot及返回前checkpoint；终态取消/超时不签发对象，无自动重试/修复 |
| Windows输出保护 | 原Owner回执认证脱敏后流，不是原始blob；带输出保护源时固定拒绝，不关闭脱敏，原始统计与安全发布分离的正式契约待实现 |
| 默认兼容 | 普通Git默认参数、有效环境、绑定及8MiB停流线保留；交付目的独立绑定，前缀仍1MiB、单命令仍5秒 |

本专项没有创建Git业务账本、受管Worktree、生产工具广告或新的预算周期；R4及R1～R6保持开放。

## 1.1 固定候选结果

- 完整回归：6287项收集，6176通过、111跳过、0失败/错误，耗时592.473秒。
- 新增专项：91项，全部纳入上述完整候选；不是额外相加的总数。
- 源码外真实Wheel安装专项：132项通过、0跳过/失败/错误。
- 闭合文档之后治理302项通过；415份资料、10380条链接及888个Mermaid块检查零问题。
- Ruff、全405个生产Python模块类型校验、Schema一致性及可读性原策略检查通过。
- Windows/其他原生条件相关跳过仍需对应平台实际验证，不能计作完成。

## 2. 真实运行与故障注入区分

[原产品Patch及真实Git测试](../../../tests/product_config/test_git_baseline.py)使用真实Agent、Gateway、审批、SQLite和原生文件执行，
仅Provider采用ScriptedProvider。覆盖SHA-1/SHA-256、创建/替换/删除、连续修改、错误根、无关暂存保护、
用户首before与HEAD不符、冲突/flags、detached HEAD和Ref末尾合法非ASCII空白。
真实Git构造intent-to-add且其空blob恰与HEAD匹配；仅检查`-v`的H标签不足以排除此情况。

1MiB以上和原8MiB边界使用原合同允许的二进制before、小型文本after及正式二进制Diff摘要。
完整观察SHA与捕获前缀SHA不同；8MiB交付用途到达EOF，普通用途仍按原8MiB线停止并拒绝。
未提高原镜像、512KiB Patch输入或1MiB完整Review容量；大文本替换的原Review拒绝不计为可交付。

[端口契约测试](../../../tests/tools/test_git_delivery_reader.py)独立重建默认绑定载荷，验证宿主环境、参数、
原Capture精确停止线及专用目的。Windows配置/Plan/Owner均为离线模拟；拒绝保护源时执行端、Owner、Spec、Plan、Store零调用。
该证据不能替代原生Windows Job/NTFS或默认消费者安装验收。

端口IO/输出、同步终态取消与期限采用明确故障注入；不计为真实OS故障重现。
静态helper配置及首检后新增helper键由真实Git配置准备；不宣称已经复现了真实clean filter执行或网络越界。
本机Apple Git2.24.3真实trace证明`core.fsmonitor=false`会调用false helper，空值负对照未调用；
此结果不是网络效果，交付目的覆盖为空值，普通目的兼容参数未改。

## 3. 受控负对照与历史失败

四份独立源码导出分别恢复Ref strip、遗漏终态checkpoint、不复验helper键、仅依赖H标签；
对应5个测试均确实失败，导入路径位于各变异导出目录。
这些是**受控变异**，不是历史已发布仓库版本；对应SHA和原结果见[verification.json](verification.json)。

保留初始失败：大文本before超出原完整Review路径，JSON字典被误当作已构造嵌套模型，
Index准备命令缺少系统PATH，以及可读性最终统计未同步。验证资料装配期YAML头或Manifest缺失造成的门禁失败也保留，闭合后重新验证。
整改不提高Review容量、不松动跨字段校验、不更改治理阈值；大对象验证改为正式二进制before，
索引准备仍使用真实Git，治理最终统计从固定源码重新生成。
中间焦点/关联执行不计为最终固定候选证据，重叠测试集合不相加。

## 4. 安装、兼容与持久化

实际Wheel在源码外新建环境离线安装，全部446个包成员与固定源码及安装后文件逐字节相等。
Wheel SHA、执行结果和日志摘要见[facts.json](facts.json)与[verification.json](verification.json)。
只读基准对象没有业务持久化，不能作为MAC、Session权限或新Approval；每次宿主必须重新读取原认证来源。
Windows查询仍沿原既有Execution/Process布局；不引入另一个Git业务数据库或未覆盖备份目录。

原干净来源Git组件、Checkpoint、归属Reader、来源合同、正式备份合同、预算保护实现、锁定依赖、CI及治理策略
共15个关键文件与比较版本字节相同，清单见[facts.json](facts.json)。
本专项没有启动新的真实模型请求，不重置70元原周期、不释放旧未知预留，也不迁移单Suite授权范围。

## 5. 可视化与后续必要工作

- [总体流程](diagrams/1.png)、[数据流](diagrams/2.png)、[时序](diagrams/3.png)均由正式设计Mermaid渲染。
- 后继先完成Windows原始统计与安全发布分离的认证契约及原生验收。
- 随后完成Git业务状态/对象/Worktree的备份闭合，完整Diff、新批准、写入恢复与Ref CAS。
- 原始字节、Windows执行位/大小写/换行转换须在完整三平台产品方案中明确，不能以本专项绕过。
- 完整真实20 Trial、至少12个严格成功、每仓成功、零越界、三平台消费者及独立Beta均未在本专项验收。

## 6. 材料目录

- [facts.json](facts.json)：候选、包字节、边界及不变契约；
- [verification.json](verification.json)：原执行结果、失败及日志SHA；
- [review-packet.json](review-packet.json)：评审范围、检查项与未验收项；
- [manifest.json](manifest.json)：本目录闭合文件清单，大小和SHA-256，不包含自身。
