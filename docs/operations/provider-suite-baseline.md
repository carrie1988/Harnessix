---
doc_type: deployment-design
status: reviewing
version: 2
code_revision: pending
owners:
  - core
modules:
  - evals
  - models
  - sandbox
related_adrs:
  - docs/adr/0048-controlled-real-eval-campaign-execution.md
  - docs/adr/0088-controlled-real-provider-suite-baseline.md
related_tests:
  - tests/evals/test_provider_suite_cli.py
  - tests/evals/test_provider_suite_execution.py
  - tests/evals/test_provider_suite_evidence.py
  - tests/evals/test_task_pack_execution.py
supersedes: []
---

# 真实Provider完整Suite运维手册

## 1. 适用范围

本文用于执行、恢复和发布`harnessix-engineering/v2`受控真实Provider完整Suite。该流程会产生真实模型费用，只允许在固定代码Revision、固定模型、固定价格快照和独立私有目录中运行。

本能力不是常驻服务，不开放端口，不需要额外数据库。默认CI不运行真实Provider Suite。

## 2. 前置条件

1. 源码树已同步到目标Revision，工作区干净；
2. `uv sync --locked --all-extras --dev`已经完成；
3. Git和Docker Engine可执行，固定检查镜像已经可拉取或存在；
4. 宿主可通过HTTPS访问北京百炼OpenAI兼容端点；
5. API Key由Secret Manager或当前受控Shell临时注入，不写入仓库、配置、命令历史或日志；
6. 私有配置、私有运行根和公开证据根位于三个独立目录；
7. 执行前已核对官方模型价格，配置生成时间处于新价格快照窗口。

## 3. 目录规划

以下变量只表示目录角色，应替换为运行宿主的受限目录：

```bash
export HX_PROVIDER_CONFIG=/secure/harnessix/provider-suite.json
export HX_PROVIDER_WORK=/secure/harnessix/provider-suite-work
export HX_PROVIDER_EVIDENCE=/secure/harnessix/provider-suite-evidence
```

要求：

- 配置文件的父目录已经存在且仅当前运行身份可访问；
- Work Root在首次运行前可以不存在；
- Evidence Root必须是新的独立目录，不能位于Work Root内部或包含Work Root；
- 不要把上述私有目录放入Git工作树、共享临时目录或自动上传目录。

## 4. 执行前检查

```bash
git status --short
git rev-parse HEAD
uv run python scripts/generate_specs.py --check
uv run pytest -q \
  tests/evals/test_provider_suite_contracts.py \
  tests/evals/test_provider_suite_execution.py \
  tests/evals/test_provider_suite_cli.py \
  tests/evals/test_provider_suite_evidence.py
docker version
```

`git status --short`必须为空。任何失败都应先处理，不能通过修改配置跳过宿主身份校验。

## 5. 生成私有配置

```bash
umask 077
uv run python scripts/create_engineering_provider_suite_config.py \
  --output "$HX_PROVIDER_CONFIG" \
  --work-root "$HX_PROVIDER_WORK" \
  --fee-stop-amount 40
```

脚本固定工程Pack v2、北京精确模型、串行Tool Call、无自动重试、单请求4096输出Token和当前24小时价格窗口。输出只包含Suite ID、摘要、计划数量和停止线，不包含路径或凭据。

检查权限：

```bash
test "$(stat -f '%Lp' "$HX_PROVIDER_CONFIG" 2>/dev/null || stat -c '%a' "$HX_PROVIDER_CONFIG")" = 600
```

配置目标已存在时脚本会拒绝覆盖。恢复必须复用原文件，不能重新生成同名配置。

## 6. 注入凭据并首次运行

从Secret Manager把值导入当前进程环境，环境变量名默认是`DASHSCOPE_API_KEY`。不要在文档、脚本参数、JSON或Shell命令行中写明文值。

```bash
export DASHSCOPE_API_KEY="$(secret-manager read harnessix/bailian-beijing)"
uv run harnessix coding-eval-suite \
  --config "$HX_PROVIDER_CONFIG" \
  --allow-network
unset DASHSCOPE_API_KEY
```

CLI输出是单行JSON，只包含稳定原因、Suite身份、计数和已知成本。退出码：

| 退出码 | 含义 |
|---|---|
| `0` | 全部Case完成且Suite Report已发布 |
| `1` | 已识别运行失败或稳定停止 |
| `2` | 参数、网络授权或配置错误 |
| `130` | 操作者中断 |

缺少`--allow-network`时CLI不会读取配置或环境Key，并返回`network_not_enabled`。

## 7. 恢复运行

只有在确认停止原因允许继续、代码Revision和全部宿主绑定未变化时，才使用原配置显式恢复：

```bash
export DASHSCOPE_API_KEY="$(secret-manager read harnessix/bailian-beijing)"
uv run harnessix coding-eval-suite \
  --config "$HX_PROVIDER_CONFIG" \
  --allow-network \
  --resume
unset DASHSCOPE_API_KEY
```

禁止操作：

- 修改原配置中的Endpoint、模型、价格、预算、程序路径或Key环境变量名；
- 删除Case状态后继续同一Suite；
- 复制已完成Case报告到另一Suite；
- 通过增加重试次数“提高通过率”；
- 在`cost_unknown`时未核对原因便继续产生请求。

若配置摘要、Pack、源码Revision或程序绑定漂移，运行必须失败关闭。需要采用新范围时创建新的Suite ID、配置和Work Root。

## 8. 结果判定

完成结果必须同时满足：

1. CLI `reason=completed`；
2. `scheduled_cases=completed_cases=10`；
3. `report_published=true`；
4. Suite Report严格重读通过；
5. `cost_completeness=complete`且币种为CNY；
6. 私有配置中的Pack/模型/Revision/价格快照与预审一致；
7. 已知费用和供应商账单均在授权范围内；
8. 没有通过修改Task Pack、评分器或安全策略换取通过。

真实模型可以出现任务失败。任务失败是质量证据，不应通过重跑挑选最佳结果或改变评分标准来隐藏。
模型未调用固定Profile同样属于质量证据：报告应保留空Baseline/Final并由严格Grader判定失败，不允许人工补造测试结果或
在Agent终态后旁路执行检查。

## 9. 发布低敏证据

完成后，在不导出Session、Artifact或Workspace的前提下发布：

```bash
uv run python scripts/publish_provider_suite_evidence.py \
  --config "$HX_PROVIDER_CONFIG" \
  --evidence-root "$HX_PROVIDER_EVIDENCE"
```

发布器会重新读取私有Suite Plan/Report、核对完整成本，并递归拒绝正文型字段、绝对路径、Secret和私有运行标识。成功目录只能包含：

```text
suite-plan.json
suite-report.json
evidence-manifest.json
```

在提交仓库前再次运行：

```bash
find "$HX_PROVIDER_EVIDENCE" -maxdepth 1 -type f -print
rg -n -i 'api[_-]?key|secret|prompt|response|arguments|tool_output|workspace|diff' \
  "$HX_PROVIDER_EVIDENCE" && exit 1 || true
```

还应通过正式Python合同严格重读三份JSON并重算Report SHA-256。仅凭文本搜索不能替代合同校验。

## 10. 故障处置

| 公开原因/现象 | 处置 |
|---|---|
| `network_not_enabled` | 确认确需收费验证后增加显式开关 |
| `configuration_invalid` | 检查0600、JSON、价格窗口和固定合同；不要打印完整配置 |
| `dependency_missing` | 在同Revision安装锁定依赖后恢复 |
| `runtime_failed` | 在私有目录查看受限诊断，按Pack/Revision/程序/Provider/Action分类定位；终态Turn仅缺少Profile调用时不应返回该原因 |
| `cancelled` | 确认当前Action终态；使用原配置显式恢复 |
| `cost_unknown` | 核对Usage、单请求输入是否超过32K、Provider响应是否完整；未确认前不得继续 |
| `fee_limit_reached` | 停止；核对实际账单和已完成证据，不通过修改原配置提高上限 |
| Evidence发布拒绝 | 不移动私有文件；定位违规字段或路径，修正发布合同或报告后重新创建空Evidence Root |

Provider错误正文、Session和Artifact可能包含第三方内容，只能在受限宿主本地查看，不复制到Issue、CI日志或公开文档。
若根因需要修改Harnessix源码，原配置绑定的Revision不得继续恢复；修正实现通过CI后必须生成新配置和新Suite ID，旧私有事实
仅用于费用与故障审计，禁止跨Revision拼接为完成证据。

## 11. 回滚与清理

真实Suite没有数据库迁移或常驻进程。回滚是停止继续请求并保留私有事实以供核对。完成证据冻结和账单核对后：

1. 取消当前Shell中的Key环境变量；
2. 确认没有后台Harnessix或Container进程；
3. 保留公开证据和必要审计摘要；
4. 按项目保留策略安全删除私有配置、Session、Artifact和Workspace；
5. 若Key曾暴露到日志、命令历史或文件，立即在供应商侧轮换；
6. 不把删除私有事实描述为撤销已经发生的模型费用。

## 12. 安全检查清单

- [ ] 工作树干净且Revision已冻结；
- [ ] 配置与Work/Evidence目录相互独立；
- [ ] 配置为0600且不含Key值；
- [ ] 模型、地域、价格来源和有效窗口已复核；
- [ ] `--allow-network`由操作者显式提供；
- [ ] Provider自动重试关闭，Tool Call串行；
- [ ] 费用停止线不超过本次授权；
- [ ] 停止/恢复使用同一配置和Suite ID；
- [ ] 公开目录只有三份低敏JSON；
- [ ] 公开JSON通过严格合同、摘要和敏感字段检查；
- [ ] 实际账单完成核对；
- [ ] 私有数据按保留策略清理。
