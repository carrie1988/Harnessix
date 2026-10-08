---
doc_type: validation-evidence
status: current
version: 1
code_revision: bef1ab088d271bec205f07d9a6ec942514b0efa7
owners: [core]
modules: [agent, evals, models]
related_adrs:
  - docs/adr/0088-controlled-real-provider-suite-baseline.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/agent/test_runtime_thread_lock.py
  - tests/evals/test_provider_reverification_v2.py
supersedes: []
---

# R3 预算合同 v2 与 R4 原 Runtime 锁归属交付报告

## 1. 交付结论与范围

本交付完成两个必要基础增量：封闭预算计划 v2、原 Runtime Thread 锁的实际 Task 归属。
前者复用原预算 Owner／Guard，后者复用原异步锁集合，不新增独立服务或执行入口。
**基础增量验收通过，R3／R4／商用发布仍为 OPEN／NO-GO。**
未登记实际复验授权，新增真实模型请求零；实际账本、原未知预留及历史成绩未修改。
未读取或修改客户原工程及副本，没有新增真实 Beta 接受。

## 2. 需求背景、设计与源码入口

- [预算合同总体与详细设计](../../changes/m09-r3-reverification-budget-v2.md)：原 v1 固定70／40，
  v2 固定60／38；封闭解析和管理快照拒绝混搭及模型绕过。
- [原锁归属总体与详细设计](../../changes/m09-r4-runtime-thread-lock-ownership.md)：
  实际 acquire／release 记录原 Task，当前 Runtime 检查不接受 `locked()`布尔证明。
- [预算合同源码](../../../scripts/provider_reverification_plan.py)及
  [锁原语源码](../../../src/harnessix/agent/runtime_thread_lock.py)为阅读起点，详设给出调用链、字段、失败语义及伪代码。

## 3. 最终候选及安装输入

源码提交为 `bef1ab088d271bec205f07d9a6ec942514b0efa7`。
Wheel版本为 `1.0.0rc1`，SHA-256 为
`0ac0725bb1e9c28d7164cc8918663e060475d86137e88fc453c31caeee445d26`。
555个生产成员与当前源码及非editable安装目录逐字节相同；1559个已跟踪验证输入单独投影。
仅版本号不足以识别候选；旧Wheel、中间依赖解析及旧测试不得替代上述候选。

最终安装使用 CPython3.12.7、pytest8.4.2及原 `uv.lock`导出的全extras／dev固定依赖。
安装入口实际位于独立 site-packages；测试目录中的源码副本仅供静态验证，不作为生产导入来源。
隔离验证投影不是付费Suite的干净源码Checkout，不据此绕过正式源准入。

## 4. 终态测试结果

| 集合 | 终态结果 | 证据边界 |
|---|---|---|
| 最终Wheel七完整目录：agent／context／session／models／protocol／app_server／product_ui | 2791 PASS、1原生Windows SKIP、0 FAIL，107.291秒 | 正式非editable安装组件回归，不是三平台认证 |
| 最终Wheel预算计划／绑定／候选链／Owner／宿主／周期隔离七文件 | 516 PASS、0 SKIP、0 FAIL，4.327秒 | 测试专属临时账本及离线Provider，不是实际授权 |
| 新预算文件 | 225 PASS | 已包含于预算集合，不重复累加 |
| 新锁文件 | 28 PASS | 已包含于七目录，不重复累加 |
| 源码完整Evals | 终态原件及准确统计见 `facts.json` | 包含新预算及旧预算子集，不与上述统计累加 |

来源为真实JUnit终态，不将pytest进度、工具启动或子代理报告直接提升为验收。
全部集合、时长、原件SHA和候选身份见[facts.json](facts.json)及[verification.json](verification.json)。

## 5. 旧合同兼容与负控

固定原提交 `15f8e145cf85e859910ddc5f87a21e7844cbc118`的实际 v1 模块与当前 v1 比较：
固定计划序列化完全相同；validation／serialization完整Schema均相同，规范SHA均为
`f2e357166711e36c3ab530784ecc9f471e58c50b8c00eef849df1598bd39bd76`。
旧版本Owner也按其原Git对象执行，不用伪造的“兼容”替身。

新增负控覆盖版本金额混搭、类型强转、额外字段、copy／construct和嵌套绕过、旧前缀及未知篡改、
38／60双上限及最小货币单位、错误范围、七类新增unknown停机、绑定链扩额、私有权限／链接／重复键。
锁负控使用真实当前Task及另一Task，覆盖未登记／外来锁／等待取消／超时／非重入／跨Task释放；
原SQLite审批关闭流程和原异常实例亦核对。

## 6. 原始失败与环境整改

首次安装预算测试的四项setup ERROR来自验证投影缺少历史Git对象，
错误明确为 `fatal: not a git repository`；不是产品断言失败。
向隔离投影补入原仓只读Git对象后，同一候选、同一七文件、无跳过完整516项通过。
保留首次错误XML／日志，不改测试、不删用例、不用最终PASS覆盖原ERROR。

早期两次定向命令因拼写不存在的测试路径而收集失败，保留原件，最终准确文件集65项通过；
不将收集失败算成产品回归。首次Mermaid默认Chrome缓存缺失，改用本机已存在Chrome渲染，未下载浏览器。
首次离线依赖解析使用未固定范围，未运行测试；随后按原lock固定依赖，最终结果仅绑定该环境。
默认扫描同时读取历史dist归档时触发既有 `scan_byte_limit`；保留该未完成结果。
最终明确选择本候选Wheel目录并完整扫描已跟踪仓库，原扫描上限不扩大，历史归档不删除；
此范围不表示旧dist或新Sdist通过。

## 7. 持久化、恢复与安全边界

所有费用测试使用临时0600账本及0700目录，阻断真实网络／凭据／Provider入口。
合同v2不结算旧unknown、不退款、不增周期；新增未知仍取消Suite并拒绝重开。
锁不持久化、不签发Token、不改变Session或原审批恢复语义。
源代码之外的外来目录未读取、运行、清理或提交；已封存历史交付保持原件。

## 8. 图示、文档及质量检查

两份详设均包含十四节、总体架构／时序／数据或状态流及完整文字说明。
七份Mermaid实际编译为SVG，预算架构PNG另经视觉检查，最终SVG保留在本目录。
Ruff检查及格式、520生产源码及五脚本Mypy、文档门禁和已跟踪资料Secret扫描按终态记录。
资料同步包含总体架构、路线图、agent／evals模块及旧预算／候选链／approved-link的版本说明。

## 9. R3剩余工作与费用边界

新合同仅提供合法表达能力；实际复验仍需有效预算所有者授权、原账本登记及新干净冻结源码候选。
保持原20 Trial、三仓、固定Profile、原Grader、原请求最坏档预留及新增未决即停。
原严格成功0/20、必需测试1/20、Beta接受0不因本切片提高，未知费用不被认定为零或实际结清。
新源码Suite和安装态产品验收分别绑定候选，不跨版本拼接成绩。

## 10. R4剩余工作与发布边界

原锁归属只是B7必要基础：仍需绑定Git的原Runtime并覆盖完整临界区／全部dispatch。
SQLite实际FD、B4末端Ref／配置保护、P1响应性、正式决定Writer、A／T2／NativeBridge／D、
默认Checkpoint／本地Commit、Backup2及三平台完整编码仍独立开放。
不因基础测试通过放宽这些门禁或启用默认Git写工具。

## 11. 资料索引及复核方法

- [Review Packet](REVIEW_PACKET.md)：独立复核要点及Go／No-Go边界。
- [facts.json](facts.json)：原件准确统计、候选与开放项。
- [verification.json](verification.json)：实际执行及质量检查。
- [manifest.json](manifest.json)：公开成员SHA及私有封存清单摘要；不含凭据、个人路径或模型正文。

复核应先验证公开manifest，再由授权维护者核验私有封存清单、候选555成员及JUnit。
不得仅凭版本号、截图或单一测试绿灯判断商用完成。
