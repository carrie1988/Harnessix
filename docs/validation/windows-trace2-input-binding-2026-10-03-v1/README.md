---
doc_type: validation-evidence
status: current
version: 1
code_revision: 5265fdf2d1755b15491f41b770b2d718bab98d2c
owners: [core]
modules: [delivery, product_config, governance]
related_adrs:
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/governance/test_windows_git_trace2_input_binding.py
  - tests/governance/test_git_trace2_projection.py
  - tests/governance/test_git_minimum_commit_probe.py
  - tests/governance/test_windows_git_native_branch_preflight_v2.py
  - tests/governance/test_windows_git_native_branch_observation.py
  - tests/governance/test_windows_git_native_failure_projection.py
supersedes: []
---

# Windows Trace2角色与固定输入验证

## 1. 结论

六件相关治理测试文件实际767项通过，0失败、错误、跳过，没有排除原整体字节冻结检查。
7件变更Python的Ruff check/format通过。507件有限实际输入前后零漂移，
包括479件受版本管理的生产成员，其中438件Python源码；不是全仓测试或507件Python源码。
实现、字段、源码位置、四图、异常、安全与部署见
[总体与详细设计](../../changes/m09-r4-windows-trace2-role-input-binding.md)。

原Run37089114490保持Git128/Worker2、input proof缺失、SDK未通过和Root未知。
离线角色修复及新的源码身份不证明Windows业务根因已经修复。
R1～R6、消费者Windows、完整Git交付/Backup v2、R3真实质量与独立Beta继续开放。
独立身份审查精确复算17项允许差分；发现的数值类型测试缺口已补8项负例并采用类型敏感检查。
审查方法与限定复核状态见[验证记录](verification.json)，不把离线审查当作原生放行。

## 2. 身份与测试范围

[八件源/测试锁](source-lock.json)逐件绑定实际写集；生产Python字节与5265发布基线保持相同。
原16件声明输入只更新四行和base_revision，其他12行、资产、PE/PDB、selectors、历史结果及预算不变。
原九件整体字节门禁保留，18件原hook/raw/stream/公开输出片段逐字相同。
旧合同继续真实拒绝新输入，新增16项独立单字节反例全部拒绝。

测试命令从项目根目录执行，需锁定开发依赖：

```bash
python -B -m pytest -q -p no:cacheprovider \
  tests/governance/test_git_trace2_projection.py \
  tests/governance/test_git_minimum_commit_probe.py \
  tests/governance/test_windows_git_native_branch_observation.py \
  tests/governance/test_windows_git_native_failure_projection.py \
  tests/governance/test_windows_git_native_branch_preflight_v2.py \
  tests/governance/test_windows_git_trace2_input_binding.py
```

这些是离线合同、合成角色和真实API拒绝测试，不运行Windows、CDB、Docker或模型。
不能以当前操作系统上的通过替代Windows正式PE/PDB及原SDK两个案例验收。

## 3. 保留失败与复算

角色候选完整611项中610通过、1旧冻结身份失败，旧包原样保留。
身份接合RED实际4失败；新夹具初次集合742通过、17失败来自实际API返回字段期望和既有目录处理，
只修正测试夹具，未改变产品或评分；未将其称为产品根因。
格式检查初次三件未格式化，布局修正后新507输入冻结并重新执行全767项，不继承格式化前成绩。
独立审查发现Python深相等会混同整数、浮点数与布尔值；新增8项类型负例先全部失败，
修复仅涉及测试差分逻辑，metadata、parser、诊断和整体guard字节不变。
原759阶段保留，最后仅一个测试变化后重新冻结507输入并完整复验767项。
原始日志/XML和输入快照保存在私有交付目录，公开只保留有限事实及原件摘要，不发布正文或个人路径。
[结构化事实](facts.json)、[验证及原件摘要](verification.json)和[公开清单](manifest.json)用于核对覆盖范围。

## 4. 原生执行条件与后继验收

新原生观察必须先完成有限独立身份审查，正常提交发布全部合同/探针字节，
固定实际40位提交及expected_revision，仅attempt1；观察超时只查询原Run，不能自行重启。
原5分钟步骤、240秒watchdog、20/45秒预算、两个selectors及仅两个有限artifact保持。
原生SDK成功、完整Git业务和真实质量都需要各自的直接证据。
