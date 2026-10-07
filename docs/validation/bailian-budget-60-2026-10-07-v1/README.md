---
doc_type: validation-evidence
status: current
version: 1
code_revision: b7bb29e00469033394842dcc29cc10b1a9959c9e
owners: [core]
modules: [evals, models]
related_adrs:
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/evals/test_provider_budget_period_isolation.py
  - tests/evals/test_provider_verification_budget.py
  - tests/evals/test_provider_verification_host.py
  - tests/evals/test_provider_reverification.py
  - tests/evals/test_provider_reverification_rebinding.py
  - tests/evals/test_provider_reverification_chain.py
supersedes: []
---

# 百炼独立60元周期预算隔离验证

## 1. 结论与验收边界

预算周期隔离的离线回归通过，运行时预算代码无改动。R3、BETA-001及其他百炼验证必须显式消费同一个私有账本路径和周期
`bab39be6-7af6-4a77-9da6-fe0664e15f65`，只有一个累计60元总额，不是各用途分别60元。
关闭重开不重置累计费用；新reserved/unknown仍停止默认Owner。

旧周期141条请求、已知费用估算3.646408元、两条unknown占用41.55648元保持原字节，旧费用不纳入新额度、
不再阻塞独立新路径。旧账本内active字段没有改写；收据只将其管理用途标为历史不新增请求。
未知实际费用不是0。预算隔离通过不构成真实R3质量PASS，Beta完成数0，商业发布门禁仍开放。

完整架构、正常/失败时序、接口、持久化、安全、部署、取舍和源码映射见
[正式设计](../../changes/m09-provider-budget-period-activation.md)。

## 2. 文件和权威来源

公开交付：[facts.json](facts.json)、[verification.json](verification.json)、[review-packet.json](review-packet.json)、
[manifest.json](manifest.json)和本报告。源码候选C为私有证据根E中的`candidate/Harnessix`。
私有证据不复制到公开资料目录，实际绝对路径由操作员在登记收据中核对。

| E中的权威文件 | 证据边界 |
|---|---|
| `ACTIVATION_RECEIPT.json` | 一次性私有管理登记；新path/id、同一总60元、旧历史边界 |
| `budget.json` | 新v1账本；登记时空请求0/0，后续以实际持久事实为准 |
| `previous-budget-snapshot.json` | 旧141条、两unknown原样快照；摘要与旧原件一致 |
| `budget-tests.xml`、`budget-tests-wheel.xml` | 既存回归记录，按实际testcase重算；不是新执行记录 |
| `credential-engine-preflight.json` | 既存认证合同、固定镜像、价格预检；本验证不重读密钥、不重新联网 |
| `endpoint-auth-preflight.json` | 既存GET models HTTP 200、固定模型可见；模型生成请求0，不是质量验证 |

新周期初始账本摘要：`34a944e91870ec6fa8662301892d801e2f83fe7a3650d45a12f64b3c24e2a0de`。
旧原件及快照摘要：`d923d8e8812e9706229b290f362b472f3934086feb75a908534cd415f88b4385`。
收据和管理指针不会被运行时自动读取；消费必须显式传入收据中的同一path/id，不能使用账本副本或旧复验身份。
一次性管理登记脚本不是新增产品能力或预算重置API，不重复执行，不把初始空快照覆盖回真实账本。

## 3. 三类请求与费用事实必须分开

1. **登记快照**：收据记录登记时请求0、已知估算0、占用0；只属于登记时刻。
2. **本次离线验证**：使用合成临时账本和离线Provider/MockTransport；真实模型请求0、不读取真实凭据、
   不取得真实账本Owner、不写真实账本。夹具中模拟的Attempt不是供应商真实请求。
3. **登记后的真实R3/Beta**：必须由对应运行的预注册配置、Suite/Trial、Session及费用事实单独认定。
   本资料不执行也不评定该真实运行，不能用登记请求0或本次离线请求0覆盖其后实际请求与费用。

`facts.json`中的只读账本观察仅表示一个带时间和摘要的文件快照，不是完整真实Suite成绩。
`known_cost`是用量与价格边界得出的估算；`reserved_cost`包含保守预留与unknown，不是供应商已扣款金额。
供应商账单未确认，实际账单金额用null表示，不用0。旧两unknown的对账保持独立历史责任。

## 4. 测试范围与结果

| 证据 | 执行属性 | cases | 失败 | 错误 | 跳过 |
|---|---|---:|---:|---:|---:|
| E/`budget-tests.xml` | 已存源码XML，重新计数但未重新执行该历史命令 | 291 | 0 | 0 | 0 |
| E/`budget-tests-wheel.xml` | 已存Wheel XML，重新计数但未重新执行该历史命令 | 291 | 0 | 0 | 0 |
| E/`recovered-budget/budget-regression.xml` | 新执行：独立已装Wheel + C中宿主scripts | 291 | 0 | 0 | 0 |

三次是独立证据，不合并成一次873-case运行。每份XML均以testcase实际数量与suite声明交叉校验。
恢复后的[周期隔离文件](../../../tests/evals/test_provider_budget_period_isolation.py)有6个函数、7 cases：
旧两unknown隔离不清账、R3/Beta跨重开共用60、reserved和unknown两参数重启拒绝、旧UUID拒绝、
用途间Owner冲突、not_sent仅释放自身预留。复用既有`ledger_file/period/PERIOD` helper及POSIX标记。

原5文件分别为：预算Guard43、宿主15、复验37、复验重绑定146、候选链43，共284 cases；连同7 cases合计291。
测试没有调用新增重置API、放宽默认unknown规则或改变原评分条件。

## 5. 独立Wheel、执行方式和复验

Wheel SHA-256：`85a8b90e367f6ab8f73235c8ec28012db5930f1f2f2bda0491c6c4deee8011ba`。
548个package成员、507个Python成员与已安装包及候选包源码逐字节匹配。候选Git基线为
`b7bb29e00469033394842dcc29cc10b1a9959c9e`；基线提交不替代Wheel摘要和运行时来源证明，不表示新增提交或产品发布。

使用E中的已安装venv、隔离模式与禁写字节码；启动器仅将C根加入scripts/tests导入路径，不加入C/src。
所有实际导入的harnessix模块逐项校验来自独立Wheel的site-packages；子进程搜索路径亦不包含源码包目录。
pytest关闭仓库缓存，临时夹具与日志/XML落到E/recovered-budget，目录0700。

```text
<E>/installed-venv/bin/python -I -B <E>/recovered-budget/run-budget-regression.py
```

启动器选择的六文件及pytest参数见私有`import-and-run-provenance.json`，包括
`--basetemp=<E>/recovered-budget/pytest-tmp`与`--junitxml=<E>/recovered-budget/budget-regression.xml`。
复验只允许重新执行离线启动器，不执行`activate-budget.py`、真实Suite、密钥读取或账本管理操作。
Ruff检查和格式检查、正式文档元数据/章节/链接/图示、交付JSON及清单一致性验证另留独立结果。

## 6. 可定位的新私有证据

| E/recovered-budget文件 | 内容 |
|---|---|
| `baseline.json` | 初始候选与权威文件摘要、旧原件核验、0700目录事实 |
| `run-budget-regression.py` | 独立Wheel离线复验启动器 |
| `budget-regression.log/xml` | 新执行的291-case输出及JUnit |
| `import-and-run-provenance.json` | Wheel成员一致性、scripts来源、全部harnessix导入来源及执行参数 |
| `xml-recount.json` | 两既存XML和新XML逐case重算及同名7-case核对 |
| `ruff-check.log`、`ruff-format.log` | 新测试文件静态检查与格式检查 |
| `documentation-check.json`、`diagrams` | 两新增Markdown的现行治理检查、14节设计及图示渲染证据 |
| `secret-scan.json`、`delivery-check.json` | 七文件Secret扫描、JSON/事实/清单一致性与摘要核验 |
| `scope-check.json` | 旧原件、权威旧文件和运行时预算源码的末次摘要对比；候选并行差异单列 |
| `evidence-manifest.json` | 私有验证文件、交付七文件及其摘要；不自哈希 |

## 7. 剩余真实门禁

- 固定完整20 Trial的真实R3质量证据：原严格任务及必需测试至少12/20、每仓库有严格成功、预注册安全约束保持。
- 同候选完整Git交付链、消费者安装与跨平台/安全最终门禁。
- BETA-001真实消费接线必须显式共享同一60元path/id；完整业务验收仍未完成，完成数0。
- 供应商权威账单及旧两unknown费用核对；账单未确认不是实际费用0。
- 每次真实消费前重新检查有效价格窗口、固定镜像、认证和配置/源码身份；遇新增未决必须停止。

路线图及运营资料由其主维护边界独立管理，不纳入本预算恢复文件改动范围。离线PASS不能关闭上述真实门禁。
