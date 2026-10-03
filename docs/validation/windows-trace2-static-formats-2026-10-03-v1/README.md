---
doc_type: validation-evidence
status: current
version: 1
code_revision: 5306c7134c1301dd10bee682be5ce1e61e120c46
owners: [core]
modules: [delivery, product_config, governance]
related_adrs:
  - docs/adr/0077-versioned-documentation-contract-and-gates.md
related_tests:
  - tests/delivery/test_git_material_trace2_static_errors.py
  - tests/delivery/test_git_material_trace2_contracts.py
  - tests/governance/test_git_trace2_projection.py
  - tests/governance/test_git_minimum_commit_probe.py
  - tests/governance/test_windows_git_native_failure_projection.py
  - tests/governance/test_windows_git_trace2_input_binding.py
  - tests/governance/test_windows_git_native_branch_observation.py
  - tests/governance/test_windows_git_native_branch_preflight_v2.py
supersedes: []
---

# Windows Trace2精确静态格式与有限发布验证

## 1. 结论与范围

同一候选八件测试文件实际944项通过，0失败、错误、跳过；811件完整执行输入前后零漂移。
三处普通error模板精确登记，原六项、事件规则、未知拒绝、branch/proof/SDK成功标准不改变。
原16件发行输入完整保留并末尾追加profile与失败发布器，共18件；原九件整体guard未排除。
详细结构、接口、数据、流程、失败与恢复见
[总体与详细设计第12节](../../changes/m09-r4-windows-trace2-role-input-binding.md#12-精确静态错误目录与失败发布接合)。

离线通过不是Windows业务修复。原Run37099316276及两个SDK失败不改写，当前验证未启动新的原生Run、
未发送模型请求、未改变费用或预留。完整Git/Backup v2、R3、Windows消费者、独立Beta与R1～R6保持开放。

## 2. 静态来源、精确输入与安全

固定官方Git for Windows commit `32c4f7689275d233577576630e1ac5b7eb354eb0`、tag
`v2.55.0.windows.5`的object-file.c三个普通error来源已经核验，模板与字段见[结构化事实](facts.json)。
`error_errno`追加动态文本，仍不匹配；前缀、大小写、尾空白、近似或未登记模板仍UNKNOWN。
有限ID只说明当前观察器分类，不证明唯一caller、根因、事件顺序、输入proof或真实对象写入成功。

[九件源/测试身份](source-lock.json)绑定当前实际写集；[验证记录](verification.json)绑定实际XML、
811输入表和三份真实渲染图的摘要。公开不包含raw、stderr正文、PID、路径、MAC或个人运行目录。
公开manifest核验本目录成员；原始执行输入和失败XML保存在私有验证包，不属于新增用户数据存储。

## 3. 失败保留与独立审查闭环

- 初始覆盖候选392通过、1失败：旧Sibling不能接受新增ID，后继只同步三个固定enum。
- 首次整合929通过、10失败：两处旧16输入数量断言及八处类型负例误用历史差分选择器；修正测试契约，生产验证不放宽。
- 第二阶段943通过，不能与历史结果累加。
- 独立审查发现P2：原catalog selector的五种有限投影正例移入新负例，单独运行原selector不再发现validator退化。
- 新反例令validator恒False，原selector确实未抛异常，实际1失败。恢复原五种正例到原selector，9项定向测试全部通过；不修改生产代码。
- 新完整944项结果独立冻结并复验，保留943阶段及所有失败，不继承旧字节成绩。

首次RED调用有一次解释器被终止导致选择器缺失的夹具错误，单独保存，不冒称产品失败。
[Review Packet](review-packet.json)区分独立静态审查与实现方实际修复验证；没有宣称第二次独立审查或原生成功。

## 4. 可复现命令与部署

从项目根目录、锁定开发依赖执行：

```bash
python -B -m pytest -q -p no:cacheprovider \
  tests/delivery/test_git_material_trace2_contracts.py \
  tests/delivery/test_git_material_trace2_static_errors.py \
  tests/governance/test_git_minimum_commit_probe.py \
  tests/governance/test_git_trace2_projection.py \
  tests/governance/test_windows_git_native_branch_observation.py \
  tests/governance/test_windows_git_native_branch_preflight_v2.py \
  tests/governance/test_windows_git_native_failure_projection.py \
  tests/governance/test_windows_git_trace2_input_binding.py
```

本增量无需服务、中间件、数据库迁移或新配置。现有原生workflow必须固定正式发布的40位候选revision、
原两个Case、原期限及attempt1；仅新的有限原生结果才能判断本次是否观察到新增ID。
观察超时查询同一Run，不自行重跑；未知、失败或证据缺失均不得升级为通过。
