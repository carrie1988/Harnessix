---
doc_type: validation-evidence
status: current
version: 1
code_revision: 9723b58688890672ec17ffd8d37d78507ed81ece
owners: [core]
modules: [tools, trusted_actions, product_config]
related_adrs:
  - docs/adr/0050-model-correctable-tool-validation.md
  - docs/adr/0069-unified-coding-action-risk-route.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/tools/test_schema_argument_feedback.py
  - tests/tools/test_argument_feedback.py
  - tests/trusted_actions/test_argument_feedback.py
  - tests/trusted_actions/test_plan_error_boundaries.py
  - tests/product_config/test_profile_argument_feedback.py
  - tests/product_config/test_profile_argument_feedback_sdk.py
  - tests/product_config/test_process_action.py
supersedes: []
---

# 固定Profile与Trusted安全输入反馈验证材料

## 1. 摘要与完成边界

生产源码候选`9723b58688890672ec17ffd8d37d78507ed81ece`，比较基线`152e7a86`。
本切片仅验收安全字段投影、正确固定Profile广告、严格拒绝、独立批准、取消及持久回放。
**没有新的真实20 Trial成绩，不关闭R3或R1～R6商用门禁。**
[原真实中断](../bounded-provider-suite-interruption-2026-09-30-v1/README.md)保持：8次模型Profile调用遗漏必填字段，
没有真实测试结果；Token超限后的未知工具效果不能由新反馈转换为完成。

完整总体及详细设计、流程/时序/数据图、字段、核心伪代码、失败恢复、安全、兼容及取舍见
[整改设计](../../changes/m09-r3-trusted-input-feedback.md)。

## 2. 已实施行为与源码映射

| 行为 | 源码与验证 |
|---|---|
| 唯一有界字段辅助 | [`argument_feedback.py`](../../../src/harnessix/tools/argument_feedback.py)：最多64字段、64字符标识符、2000字符消息；不遍历参数值 |
| 普通工具兼容 | [`runtime.py`](../../../src/harnessix/tools/runtime.py)：原错误码、字段顺序、分页特殊提示保持 |
| Trusted拒绝 | [`planning.py`](../../../src/harnessix/trusted_actions/planning.py)：冻结显式Schema优先；敏感键首先拒绝；未知回调正文沿原白名单清理 |
| 固定Profile广告 | [`process_action.py`](../../../src/harnessix/product_config/process_action.py)：显式必填profile及精确值，none策略maxItems0；严格Decoder原AST不变 |
| SDK真实业务链 | [`SDK专项`](../../../tests/product_config/test_profile_argument_feedback_sdk.py)：只替换网络Provider及Container终态；Router、审批、Executor、Artifact、协议及SQLite实际运行 |

SDK专项先完成八次非法调用的安全响应，按正式call_id对协议Item多版本去重，
证明没有计划/审批或进程；随后同Thread新Turn显式修正参数。审批前进程次数为0，
批准后执行恰好一次；独立取消场景执行0次；Session重开与纯Reducer回放相等。
该确定性输入不能证明真实模型会修正，更不代表真实编码能力。

## 3. 原失败、修正与测试结果

| 测试集 | 结果 | 边界 |
|---|---|---|
| 原关联基线 | 52通过 | 原候选，无新反馈 |
| 修正后的独立原负对照 | 5失败 | 从原152提交导出源码，用相同新断言复现字段反馈/广告不足；实际导入路径已核对 |
| 最终焦点 | 104通过 | Schema投影、Callback防泄漏、原严格Profile链及新增SDK专项 |
| 关联模块 | 3226通过、63跳过 | 9个实际测试目录；辅助改为固定键查询的后继差异由最终焦点及完整回归覆盖 |
| 治理后继 | 302通过 | Git2.53，未放宽原CRLF负对照 |
| 源码外安装 | 70通过 | 实际新Wheel、锁定全部产品Extras及精确pytest工具；导入来自独立site-packages |
| 同候选完整离线 | 6085通过、111跳过、0失败/错误 | 6196项、574.751秒；固定9723源码；默认真实开关未启用 |

测试集相互重叠，不能相加。所有离线网络使用ScriptedProvider或MockTransport。
初始测试及工具配置错误也保留摘要：敏感`private_key`夹具误期待普通参数错误、协议Item创建与
终态双版本未去重、公共批准字段误用Kernel字段，以及不存在测试目录造成0项收集。
产品代码不为这些测试错误放宽原行为。独立原源码负对照重新验证后仍5项真实失败。

治理原FAIL来自Apple Git2.24的CRLF对照，后继仅切换Git2.53，原断言未改写。
第一次离线安装包含无关Dev工具，因缓存缺少librt失败；后继明确锁定产品全部Extras，
仅补测试执行所需精确pytest/pytest-asyncio，不称为全部Dev锁安装通过。
每次执行的XML/原日志SHA与固定原因列于[verification.json](verification.json)，原件在本机私有验证目录保留。

## 4. 不变证明、实际发行物与兼容

[facts.json](facts.json)包含11个不变AST符号及7个不变文件证明：严格Decoder、Selector验证、
Process执行/输出、Binding算法、Resolver、Definition、原规划/规范解码/敏感键检查及分页反馈。
原预算脚本、请求Guard、单次计划、验证宿主、依赖/锁文件及结构治理策略字节不变。
正式Schema与工程Task Pack生成校验通过；没有迁移、配置、预算或评分更改。
Ruff及1378文件格式检查、403源文件Mypy、原可读性策略通过；文档413份/10330链接/885 Mermaid块
检查零发现；实际Wheel在内3708输入Secret扫描零命中，3个新增图已渲染并实际观察。

实际Wheel SHA-256：`e8dc92e32ee3ae0b6efe48dbde5e55906a85605a04a05cb8a654235d8be9aa9f`。
444个产品包成员逐字节与当前源码及独立安装一致，导入位于独立site-packages而非源码目录。
完整私有403源文件清单及安装清单的SHA保留于结构材料；没有改写旧Wheel或旧安装环境。
Profile描述与Schema更新使原Fingerprint变化；旧描述指纹在Decoder之前明确拒绝，不能复用旧批准。
只读工具目录及历史Session不重写。

## 5. 预算与真实验证限制

本切片新增真实模型请求0次。原70元周期保留旧20.77824元全额未知预留；
同一40元复验额度已用已知估算0.219136元，剩余39.780864元，不是新40元额度。
原账本前后字节一致，SHA与原真实中断材料精确相同；当前已知估算合计1.74186元。
已知Usage估算不是供应商实际账单。新完整Suite必须取得同一范围的显式重新绑定，
不重放本次已中断Suite、不自动续期、不释放未知预留；任何新增未决仍停止新请求。

## 6. 材料清单、图示与复现

- [facts.json](facts.json)：源码/不变证明、业务链及预算边界；
- [verification.json](verification.json)：每次原执行的身份、结果及日志摘要；
- [review-packet.json](review-packet.json)：专项评审与未验收范围；
- [manifest.json](manifest.json)：本目录文件长度及SHA，清单自身不纳入循环摘要；
- [流程图](diagrams/input-flow.png)、[数据流程图](diagrams/input-data.png)、[时序图](diagrams/input-sequence.png)：由设计的3个Mermaid原块独立渲染，已实际观察。

复现使用固定源码、Python3.12.7、Git2.53；本机正式测试夹具使用umask022，私有报告目录0700/文件0600。
从项目根执行正式Schema、Task Pack、Ruff、Mypy、可读性及文档门禁；
焦点执行以上7份related_tests对应文件，治理使用原`tests/governance`，完整回归使用无真实开关的默认pytest。
安装场景由独立环境安装真实Wheel，在源码外工作目录执行同一测试，显式禁止editable源码替代安装结果。

## 7. 风险与后续必要项

真实模型遵循率及完整20 Trial复验未取得；旧费用与工具未知效果分别保留，不混淆。
默认Docker Desktop路径、三平台同候选核心产品链、Windows11消费者验证、
Commit/Checkpoint产品接线、独立Beta及R1～R6仍开放。仅离线参数反馈通过不能标记商用发布。
