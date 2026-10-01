---
doc_type: validation-evidence
status: current
version: 1
code_revision: 59e129e059ebbe1aff725bbef2e6555767cbcce5
owners: [core]
modules: [delivery, workspace]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/delivery/test_git_tree_projection.py
  - tests/delivery/test_git_object_material_factory.py
  - tests/delivery/test_git_object_references.py
  - tests/delivery/test_git_tree_closure.py
  - tests/product_config/test_git_baseline.py
  - tests/product_config/test_git_delivery_source.py
  - tests/product_config/test_git_delivery_source_sdk.py
supersedes: []
---

# 完整目标文件树纯规划验证包

## 1. 结论与范围

本包对应[总体与详细设计](../../changes/m09-r4-git-tree-projection.md)，覆盖原CAS全部base与必要after
验真、首before比较、逐路径独立目录变更、完整目标tree正文/OID及显式容量合同。
研究基线59e129e不包含新增实现；最终内容由1244件输入目录与各资产摘要绑定。
[Facts](facts.json)、[Verification](verification.json)、[Review Packet](review-packet.json)及
[Manifest](manifest.json)分别说明实际内容、验证结果、独立评审和公开文件字节。

纯规划返回内容不是持久化、授权或业务成功回执；不开放默认Commit/Checkpoint，
不修改GitDB、正式备份范围或产品容量承诺，不关闭R1～R6商用门禁。

## 2. 正式实现合同

- 原材料工厂完整计算Git类型头、正文和OID，复用原8MiB、实际bytes和类型/格式合同，不执行Git或写CAS。
- 原CAS完整回读所有base普通文件与tree，包括未修改、空、二进制、可执行成员；模式与正文分别比较。
- 目录再次按路径回读展开，重复子树不共享可变目录；全部首before存在性、长度、SHA和模式与base一致。
- 必要末after仅来自原CAS完整blob；缺失、重复、额外、跨格式、坏摘要或正文不能作为目标输入。
- 先移除全部选中before，再应用全部after；无关空tree保留，仅移除删除触及的空目录。
- 目标全路径复用原POSIX/Windows比较键与安全路径合同；原32MiB镜像容量、8MiB对象及四项宿主显式限额保持。
- 规范二进制tree编码按目录尾斜线排序，自底向上计算全部目标；原base、必要after和新tree按唯一对象并集记账。
- 任一异常、超限、损坏、取消或期限整体失败；无部分返回，无CAS/SQL/Git/工作树/Ref写效果。

## 3. 输入与实际验证

最终完整输入目录SHA256：`7dacd4ba2d587b0c050b81487cf11cd9c88d20a4d2a376cddee5f643b33730dd`。
初始完整目录SHA256：`8863ee5e6446c6f5763b4bf710560122e9058de9c2eca6d19e39fb7d52124799`。
两者均为1244件；唯一后继差异为真实Git差分夹具的版本选择/记录及四项回归，生产模块字节不变。
最终关联组按新目录重新执行，不把初始测试结果改写为新目录下的执行。

| 范围 | 总数 | 通过 | 跳过 | 失败/错误 |
| --- | ---: | ---: | ---: | ---: |
| 初始冻结C3工厂/投影焦点 | 157 | 157 | 0 | 0 |
| 初始输入旧材料/CAS/解析/闭包关联 | 796 | 796 | 0 | 0 |
| 最终Delivery/Process/材料/备份恢复/重启关联 | 1868 | 1810 | 58 | 0 |
| 最终原Git基准与产品来源/SDK | 99 | 99 | 0 | 0 |
| 同一Wheel源码外Python3.12 | 1133 | 1131 | 2 | 0 |
| 同一Wheel源码外Python3.13 | 1133 | 1131 | 2 | 0 |
| 最终完整治理 | 302 | 302 | 0 | 0 |

两个最终源码关联组逐案例身份核对无交集，合并1967项、1909通过/58跳过。
其他集合有重叠，两个Python安装测试也不能相加为独立产品场景或消费者OS。
最终Ruff/Format、424模块Mypy、原可读性和Schema检查通过；治理302通过。
Secret六条规则自检与仓库/实际Wheel扫描分别执行，3947输入完整覆盖、零命中；
442文档、10874链接和933个Mermaid块检查零问题。详见Verification，不代替实际编码质量。
唯一Wheel SHA256为`8d83e63a149481d87fd2812fdf45db479094c09b3babd9368d12448fa37f58a7`；
470发行物成员中465个包成员/424个Python模块的源码、Wheel、两次安装字节一致，RECORD全量验证。
两个实际安装环境均审计到187个实际导入模块，未回退源码；未导入模块仍完成全包字节检查。

## 4. 原失败及根因处置

1. 两项差分夹具顺序错误曾触发原净Mutation排序守卫；仅改夹具顺序，原守卫不变。
2. 四项真实输入负对照证明Pydantic接受合法外观str/int子类；生产代码增加实际标量类型检查，原FAIL保留。
3. 首次完整治理301通过/1失败，文档缺少数据流程和取舍语义标题；按原文档政策补齐，未修改策略或删除用例。
4. CI 36806972653的三个矩阵各四项setup错误来自Git版本精确断言，而不是对象解析失败。
   夹具移除维护者绝对路径，使用PATH当前Git并记录实际版本；四个原真实两格式差分继续执行。
   缺失Git、命令失败、不支持SHA256或超时仍失败，不转skip，不提高期限。
5. 初次版本夹具专项314通过/4条JUnit属性警告；改为xunit2兼容testsuite属性，最终关联组重新执行。
6. Worker局部目录的原说明误写23件，实际JSON目录21件；原记录与纠正均保留，不将局部目录当成全仓冻结。

## 5. 原生CI与发布风险

[CI 36806972653](https://github.com/carrie1988/Harnessix/actions/runs/36806972653)绑定原59e129e：
文档和Container成功，macOS及两个Python任务因上述版本夹具失败，Windows首NTFS步骤
在125通过/2跳过后达到原三分钟期限，后继Git步骤未执行。
现有证据不能将最后打印案例认定为死锁根因；当前算法和版本夹具的本机通过也不能清除该超时。

当前C3新候选原生结果尚未取得；Windows11消费者、完整真实20 Trial、独立Beta及同候选发布仍开放。
实际测试过程没有模型请求、凭据读取、费用账本变更或Docker操作。

## 6. 独立评审、图示与后继接线

独立审查核对两个生产模块完整内容和21件局部输入，未发现P0/P1/P2；审查者函数测试0。
追加夹具审查独立核对版本变化，全部真实差分、隔离配置和失败检查保持；不推导商用支持范围扩大。
四幅Mermaid图均实际渲染并逐张视觉检查；核心流程/时序依据实际函数顺序重新渲染，
图示只证明语法、中文字形、源码对应和版面可读性，不是运行验收。

后继必须接通完整Diff和独立新批准、认证对象目录/角色、GitDB全前缀、双工作树及原Runtime新派生事务，
再开放默认Checkpoint/Commit，并完成Backup v2与新根重新授权。内部四限额仍非产品默认容量。
