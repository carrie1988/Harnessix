---
doc_type: validation-evidence
status: current
version: 1
code_revision: 1e2253b2dd304f8de4a516a40c5919c5d05b68b4
owners: [core]
modules: [product_config, delivery, agent, trusted_actions]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/product_config/test_git_checkpoint_preparation.py
  - tests/product_config/test_git_checkpoint_scope.py
  - tests/agent/test_cancel_settlement.py
  - tests/trusted_actions/test_agent_preplanning_settlement.py
supersedes: []
---

# Checkpoint 实际可信准备组件验证

## 1. 需求与范围

[完整详细设计](../../changes/m09-r4-git-checkpoint-preparation.md)定义原认证pending调用、成功Patch
用户观察、原受控全对象读取、完整Scope/Diff、Core2耐久CAS、原首次Route及正式Review的实际链。
只读命令授权来自合成夹具的原正式ExecutionPlan，不是默认Policy或Git业务批准；不执行A/T2/D。

## 2. 测试验证与证据等级

独立安装专项141项、关联2008项和原治理1413项全部通过，零失败、错误及跳过。最终统计见[结果](result.json)。
纯数据范围组装、实际离线认证SDK和模拟结算故障分别计数；网络Provider为离线脚本，未调用付费模型。
独立安装只隔离项目包；测试依赖仍复用受管环境，不声称全新依赖环境或消费者三平台验收。

## 3. 失败闭环与安全

独立审查发现取消排空吞结算失败与原UUID缺失事实仅首查。两个显式托管入口保留非取消结算异常；
原gather自己的finally复核已完成子任务，排除领域取消；A/D首末使用同一原no-follow观察。
符号链接按原workspace_path_denied拒绝，文件/目录触发固定规划拒绝。原FAIL原件保留在专用私有验证包。

## 4. 源码与后续边界

[源输入](source-inputs.json)、[审查包](review-packet.md)和[摘要](SHA256SUMS)绑定正式候选。
默认工具未注册；完整ProductLink/NativeBridge、受控A/T2/D、独立Commit、Backup2、真实R3、
三平台消费者、有限Beta和同候选R1—R6继续开放，不把组件测试通过标记为商业1.0完成。

## 5. 同候选安装与完整门禁

新增141项分为66项原CAS范围声明、13项真实离线认证SDK及62项模拟结算（59项托管/Gateway与3项实际Planner注入）。
Git业务写次数为0。全部496个生产模块在主仓库、独立普通Git副本、Wheel及隔离安装目录逐字节一致。
原281份Schema及五项政策/Native门禁原字节不变，包依赖151条未增加。
Mypy、Ruff、1765文件格式、564份文档/12931条链接及5040输入Secret检查通过。
本机系统旧Git的初次治理失败原件保留；使用原要求的Git2.53复验1413项通过，没有修改用例或阈值。

完整失败日志、XML、安装输入、5幅渲染图及完整验证Manifest存于专用私有验证目录；公开包不复制临时Owner状态或凭据。
