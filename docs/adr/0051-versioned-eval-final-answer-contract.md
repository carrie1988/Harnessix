# ADR 0051：版本化公开Coding Eval最终回答契约

- 状态：已接受；任务v3实现与离线验收完成，真实Campaign待执行
- 日期：2026-09-06
- 范围：历史任务Prompt版本、严格最终回答协议、旧Campaign兼容

## 1. 背景

任务v2分页纠正后真实Campaign证明三个run都能采用`tool_expected_revision_required`并继续分页。其中两个run完成正确Patch、测试与Git核对，却因最终文本带Markdown围栏且字段结构不符而失败。

[实现求证](../research/eval-final-answer-contract-applicability.md)确认：评分器要求严格裸JSON，但v1/v2 Prompt只写“按约定输出JSON”，当前模型请求也没有携带该约定的系统指令。因此该失败是评测输入与评分契约不闭合，不是可归因的模型协议遵循失败。

## 2. 决策

1. 保留任务v1/v2全部字段、Prompt和指纹；
2. 新增任务v3，继承v2全部执行边界，只在Prompt中追加完整最终回答契约；
3. 最终正文必须是裸JSON对象，不得包含Markdown围栏或其他文字；
4. Prompt明确`summary`、`changed_paths`和`tests[{profile, passed}]`，示例绑定当前允许路径及`focused` Profile；
5. 不修改评分器，不做围栏剥离、字段猜测或语义提取；
6. 不增加Eval专用系统消息，不提前改变0.6指令层设计；
7. Catalog默认最新版本升级到v3，恢复始终按计划中的精确版本与指纹执行。

## 3. 失败与恢复语义

v3回答缺失、非法JSON、额外字段、路径或测试声明不一致时，仍由`final_answer_consistent`确定性失败。报告继续只保存回答摘要、字节数和已解析机器声明，不保存语义摘要。

旧v1/v2计划、运行状态和报告不重写。Campaign恢复若把持久v2替换为默认v3，任务版本或指纹核对必须在Provider创建前失败。v3必须使用新Campaign与run ID。

## 4. 兼容与安全

本决策不修改Agent Event v9、Provider Event v3、Session migration11、工具Schema/指纹、Action/Patch/Process/Artifact协议、数据库或费用算法。Prompt中的路径和Profile均来自已冻结任务定义，不包含私有工作区路径、凭据或隐藏检查实现。

严格评分器不信任模型摘要；Git、测试、隐藏检查和Session顺序仍是结果权威。显式Schema只关闭输入缺失，不降低修改边界或审批要求。

## 5. 验收门禁

- Catalog可精确读取v1、v2、v3，默认返回v3；
- v1/v2 Prompt保持一致，v3指纹唯一且预算仍为100000；
- v3 Prompt包含裸JSON、禁止围栏、允许路径和必跑Profile；
- 历史物化、正式Runtime、Campaign准入/恢复和全量回归通过；
- 新Campaign在固定模型、单Provider尝试、独立run与总费用预算内形成严格可比较结果。

未完成真实v3 Campaign前，不关闭0.5.5c，不进入变更包合入完成状态。
