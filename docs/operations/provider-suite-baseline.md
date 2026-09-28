---
doc_type: deployment-design
status: current
version: 5
code_revision: 90de93f565ea88679e54242ee6f1771e9be721b7
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
  - tests/evals/test_provider_verification_budget.py
  - tests/evals/test_provider_verification_host.py
  - tests/evals/test_provider_suite_evidence.py
  - tests/evals/test_suite.py
  - tests/evals/test_task_pack_execution.py
  - tests/integration/test_task_pack_execution.py
supersedes: []
---

# 真实Provider完整Suite运维手册

## 1. 适用范围

本文用于执行、恢复和发布`harnessix-engineering/v2`受控真实Provider完整Suite。该流程会产生真实模型费用，只允许在固定代码Revision、固定模型、固定价格快照和独立私有目录中运行。

本能力不是常驻服务，不开放端口，不需要额外数据库。默认CI不运行真实Provider Suite。

## 2. 前置条件

1. 源码树已同步到目标Revision，工作区干净；
2. `uv sync --locked --all-extras --dev`已经完成；
3. Git和Docker Engine可执行，原Pack固定检查镜像全部已存在且RepoDigest一致；验证宿主不自动拉取；
4. 宿主可通过HTTPS访问北京百炼OpenAI兼容端点；
5. API Key由宿主Secret Manager、macOS launchctl或显式钥匙串配置读取，不写入仓库、配置、命令历史或日志；
6. 私有配置、私有运行根和公开证据根位于三个独立目录；
7. 执行前已核对官方模型价格，配置生成时间处于新价格快照窗口。
8. 已有私有验证预算账本及唯一active周期，已知费用加占用不超过原额度，没有未决请求；不能自动新建或重置预算。

## 3. 目录规划

以下变量只表示目录角色，应替换为运行宿主的受限目录：

```bash
export HX_PROVIDER_CONFIG=/secure/harnessix/provider-suite.json
export HX_PROVIDER_WORK=/secure/harnessix/provider-suite-work
export HX_PROVIDER_EVIDENCE=/secure/harnessix/provider-suite-evidence
export HX_PROVIDER_BUDGET=/secure/harnessix/provider-budget.json
export HX_PROVIDER_PERIOD=00000000-0000-0000-0000-000000000001
```

要求：

- 配置文件的父目录已经存在且仅当前运行身份可访问；
- Work Root在首次运行前可以不存在；
- Evidence Root必须是新的独立目录，不能位于Work Root内部或包含Work Root；
- 不要把上述私有目录放入Git工作树、共享临时目录或自动上传目录。
- Budget父目录须为原700私有目录，文件为600、当前Owner、单硬链接；Period值替换为原唯一active周期UUID。
- 原预算由授权宿主提供；本工具不提供创建、加额、清零或未决请求自动退款操作。

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

## 6. 预检凭据并首次运行

受控验证使用[持久请求预算宿主](../changes/m09-r3-verification-request-budget.md)，
仍调用原正式Suite、Case、Agent和Grader，不增加旁路执行或模型重试。
macOS无钥匙串参数时优先读取launchctl中的配置环境引用，随后才读取父进程环境；
Linux由已有Secret Manager向受控进程提供环境引用。不得全局修改、清理或回显用户凭据。

```bash
uv run python -m scripts.run_engineering_provider_suite_budgeted \
  --config "$HX_PROVIDER_CONFIG" \
  --budget-ledger "$HX_PROVIDER_BUDGET" \
  --period-id "$HX_PROVIDER_PERIOD" \
  --allow-network
```

macOS也可显式增加`--keychain-service com.example.bailian --keychain-account agent-eval`，
用真实服务名和账户替换示例；只传名称，不把Key值放入参数。显式钥匙串读取失败不回退到其他Key。
实际SDK通过`api_key=`注入短生命周期凭据，Key不写入Suite配置或预算账本。

宿主输出是单行JSON，只包含有限原因或原Suite低敏结果，不输出配置、路径、第三方错误或正文。退出码：

| 退出码 | 含义 |
|---|---|
| `0` | 全部Case完成且Suite Report已发布 |
| `1` | 已识别运行失败或稳定停止 |
| `2` | 参数或网络授权缺失；其他预检失败返回1和有限原因 |
| `130` | 操作者中断 |

缺少`--allow-network`时宿主不会读取配置、预算或Key，返回`network_not_enabled`。
镜像不在原Digest范围时先返回`verification_image_unavailable`，不读Key、不发送请求、不自动拉取。
价格窗口失效、原预算未决或原身份不符均失败关闭。不能确认费用时不输出零费用或自动退款。

通用`harnessix coding-eval-suite`保留既有默认禁网及CLI进度合同，原Factory恢复指纹不变；
它本身不提供此处的累计请求预留保护。受有限验证预算约束的运行不得改用通用CLI绕过Guard，
通用CLI也不能恢复采用Guard指纹的原Suite。

## 7. 恢复运行

只有在确认停止原因允许继续、代码Revision和全部宿主绑定未变化时，才使用原配置显式恢复：

```bash
uv run python -m scripts.run_engineering_provider_suite_budgeted \
  --config "$HX_PROVIDER_CONFIG" \
  --budget-ledger "$HX_PROVIDER_BUDGET" \
  --period-id "$HX_PROVIDER_PERIOD" \
  --allow-network \
  --resume
```

禁止操作：

- 修改原配置中的Endpoint、模型、价格、预算、程序路径或Key环境变量名；
- 删除Case状态后继续同一Suite；
- 复制已完成Case报告到另一Suite；
- 通过增加重试次数“提高通过率”；
- 在`cost_unknown`时未核对原因便继续产生请求。
- 清除预算中的`reserved/unknown`、改周期或原额度，或用账本副本增加可用预算。

请求前持久预留完整最高档费用；完整原模型用量结算后才发布成功。Suite取消、父Task退出及费用未知会关闭源并停止后续Case。
未知金额保留全部占用，重启不会自动退款；通过外部受控费用核对处理。过期窗口也不能重新定价后恢复旧身份。

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
R3首发质量还要求原完整20 Trial的严格任务及必需测试均至少12/20、每个仓库有严格成功，
并满足预注册安全限制；`reason=completed`只表示Suite执行和报告完成，不等于质量达标。
模型未调用固定Profile同样属于质量证据：报告应保留空Baseline/Final并由严格Grader判定失败，不允许人工补造测试结果或
在Agent终态后旁路执行检查。工程Pack中的检查均为Task声明的必需检查，因此空Final在Suite中必须计为
`failed`且进入测试通过率分母，不能解释为`not_applicable`。

### 8.1 历史诊断运行缺口

候选Revision `dd8b997`的受控运行已经完成10 Case × 2 Trial并记录CNY 1.44998完整已知成本，但没有形成可发布Suite：
20个Trial均没有Final Profile Observation，旧投影将其错误标记为测试不适用，聚合拒绝零适用分母。该运行同时暴露
Campaign终态可能被循环前旧State覆盖、CLI Runtime失败固定回报零进度/零成本的问题。

这些事实只用于修正执行器，不是公开质量基线。旧配置与运行根绑定旧代码Revision，禁止使用`--resume`跨Revision继续。
修正通过全矩阵CI后，应按第5节生成新的Suite ID、配置和Work Root，重新执行完整20 Trial；不得复制旧Case报告或把两次
运行拼接成完成证据。

### 8.2 当前冻结基线

Revision `fb4a0ea8f7ffcd14113212fb77b2028143af9914`已经通过
[CI 35491527318](https://github.com/carrie1988/Harnessix/actions/runs/35491527318)。基于该Revision创建的全新Suite完成
10 Case × 2 Trial，公开结果为：

- `reason=completed`、`completed_cases=10`、`report_published=true`；
- 20个Turn正常终结，Provider失败和Agent运行时失败均为0；
- 任务成功0/20、测试通过0/20、人工干预0；
- 81次模型请求、318,478输入Token、12,148输出Token；
- `cost_completeness=complete`，已知成本CNY 1.46828。

[冻结证据](../validation/provider-engineering-2026-09-20-v1/README.md)已完成正式合同重读、Report摘要重算和递归敏感字段检查。
该结果是固定组合的质量基线，不是成功率达标声明；后续改进必须建立新Suite，不得恢复、覆盖或挑选本次Trial。

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
| `runtime_failed` | 先读取CLI公开的可信完成前缀与已知成本，再在私有目录查看受限诊断；终态Turn仅缺少Profile调用时不应返回该原因，缺少必需测试应形成失败Suite证据 |
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
