---
doc_type: validation-evidence
status: current
version: 1
code_revision: e0c47ca2f96c4e87a055b3135ac6c3be883275d5
owners: [core]
modules: [evals]
related_adrs:
  - docs/adr/0044-coding-eval-contract-and-grader.md
related_tests:
  - tests/evals/test_grader_final_feedback.py
  - tests/evals/test_grader.py
  - tests/evals/test_report.py
supersedes: []
---

# R3 最后成功修改后的验证反馈整改报告

## 1. 范围与判定

`FINAL_PATCH_FEEDBACK_BUGFIX_VERIFIED_NOT_REAL_QUALITY_ACCEPTANCE`。
[完整设计](../../changes/m09-r3-final-patch-feedback.md)修复原 Grader 沿用早期通过结果覆盖后续成功修改的误接受。
两个反馈检查共用最终修改后的每个必需 Profile 最后有效检查位置；Git 核对和回答须随后完成。
单修改合法闭环保持，未完成或失败 Patch 不形成新成功修改。
此为既有 v1 闭环的实现纠错，14 项检查/分类、Schema 和原 Task Pack/历史报告不变，不扩展评分规则或降低门槛。

## 2. 实际候选身份

研究父提交 `e0c47ca2f96c4e87a055b3135ac6c3be883275d5` 不是新增修复已提交证明；[全部输入](source-inputs.json)固定 5096 件来源，后续提交证明另行关联。
Wheel SHA256 `fd56221c5973ce2da12e3e9a5dfeb3ebc6836a7ed0a3c6e1b60b055c76e66dcd`；548 包成员、507 Python 源在克隆、Wheel 及源码外独立安装逐字节一致。
安装验证使用本机 CPython 3.12.7 和原锁 openai/tui/anthropic/dev 输入，离线哈希安装、`python -I` 无源码 fallback。
这是评分验证环境，不是新的终端用户先导环境或三平台发行物验收；已交付先导仍固定原候选。

## 3. RED 与实际回归

新矩阵原实现为 **12 FAIL / 17 PASS**，29 个节点，原 XML/日志保留；阳性隐藏检查与回答声明不能掩盖缺少重测。

| 验证范围 | 结果 | 秒数 |
|---|---:|---:|
| source-focused | 48 PASS | 0.480 |
| source-evals-domain | 705 PASS | 113.142 |
| source-governance | 1413 PASS | 43.291 |
| installed-focused-final | 48 PASS | 0.349 |
| final-documentation | 29 PASS | 8.786 |

集合包含交集，不累加为唯一通过数。专项覆盖第二次成功修改后缺重测、多 Profile 只重测一项、最后检查失败、缺失/非终态检查、失败/拒绝 Patch、Git/回答逆序及单修改兼容。
全部为确定性合同/夹具验证，不是实际用户任务、真实模型质量或认证生产者成绩。

## 4. 诊断与失败原件

首次 Ruff 有两项测试签名 E501；仅作签名换行，位置以外 AST 逐项相等，原失败日志保留。
Evals 域运行含排版前同名/同参数节点；最终安装专项使用排版后确切字节再次执行 48 项，生产评分器字节未变化。
三幅详设图实际渲染并逐图检查；静态审查未发现该有界修补的明确 P0/P1/P2，不替代运行或验收。
新源 Ruff、Mypy、可读性、原治理和变化文档校验分别记录；282份公共Schema、策略/CI与原构建锁等517件受保护输入字节保持。

## 5. 不变的质量、费用与发布边界

真实 R3 最近完整结果仍是严格 **0/20**、必需测试 **1/20**；后继中断没有新质量分数，不重写冻结成绩。
本轮真实模型请求 0，不读取真实凭据；两笔未决预留和暂停规则保持，不释放预留或自动重试。
默认完整编码/Git 交付、同步调度、三平台消费者、独立 Beta 及同候选 R1～R6仍开放；不能宣布商用 1.0。
同一固定安装候选原SDK负控1项通过，但50ms心跳的83次观测中最大调度间隔为 **21.594秒**；无后台观测线程、不采集局部变量。它复现响应性P1，不是SLA或性能通过，也不能与旧高开销插桩直接比较。
本结果也不证明 Runtime 会强制补测；Agent 的最终完成策略和实际模型行为仍需独立改进与验证。

## 6. 复核与交付

[结构化结果](result.json)、[输入全集](source-inputs.json)、[Review Packet](review-packet.md)和[摘要](SHA256SUMS)是公开低敏索引。
专用本地交付目录保留完整报告、RED/GREEN XML、锁定输入、Wheel/安装证明、图像、静态日志、提交/推送证明及完整 Manifest。
普通开发提交仅读取一次新 CI 元数据，不等待或将排队状态当成功。
