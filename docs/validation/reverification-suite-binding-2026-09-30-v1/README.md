---
doc_type: validation-evidence
status: current
version: 1
code_revision: a0b5df0a3c380b8b058b8e45c99a731a9022e7f0
owners: [core]
modules: [evals, models]
related_adrs:
  - docs/adr/0088-controlled-real-provider-suite-baseline.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/evals/test_provider_reverification_rebinding.py
  - tests/evals/test_provider_reverification.py
  - tests/evals/test_provider_verification_budget.py
  - tests/evals/test_provider_verification_host.py
supersedes: []
---

# 同一剩余额度的单次Suite切换验证报告

## 1. 交付范围与完成边界

固定脚本候选`a0b5df0a3c380b8b058b8e45c99a731a9022e7f0`，验证一次显式Suite切换保持
原70元周期、同一个40元授权及全部已用/预留，撤销旧Suite运行资格，且新增未决立即停止。
完整总体及详细设计、源码、字段、伪代码、流程/时序/数据图和故障矩阵见
[正式设计](../../changes/m09-r3-same-cap-suite-rebinding.md)。

本交付没有登记实际账本，没有新模型请求或费用，没有修复后的完整20 Trial成绩，
不关闭R3或R1～R6。所有预算写入及硬退出实验使用独立临时账本。

## 2. 核心合同与真实故障验证

- 原`bounded_reverification`授权原样保留；切换前全部请求按完整规范摘要冻结。
- 已用0.219136元继续计入同一个40元上限，只承接39.780864元；旧unknown全额预留保持。
- `reserve`自动追加原复验、切换及目标Suite身份，调用方不得伪造这些字段。
- 默认、旧Suite及其他范围拒绝；新unknown或reserved同进程与重开均拒绝。
- 唯一切换及原授权不能替换，Owner不能发布schema降级。
- 实际子进程在replace前、replace后和目录fsync后退出；重开只允许同计划登记/确认，
  不重复发布已出现的V2，不新增额度。同步失败及公开管理错误仍失败关闭。

实际`32e974f`旧Owner从固定Git对象未经删改载入独立命名空间。
原V1授权范围仍可读取，V2默认/原Suite/新Suite三个范围都拒绝，不能忽略新字段而继续旧授权。
这不是人工删除分支的变异体，也不证明对同UID恶意恢复整个旧原件具有不可回滚保护。

## 3. 测试结果与原失败保留

| 验证 | 结果 | 边界 |
|---|---|---|
| 固定候选专项 | 146通过 | 合同负例、双上限、范围撤销、旧Reader、同步及三个实际退出窗口 |
| 全Eval模块提交前回归 | 566通过 | 同一生产脚本，包括新专项；没有真实Provider开关 |
| 源码外产品依赖与当前验证宿主 | 241通过 | 产品从独立site-packages导入，新验证脚本从固定源码导入 |
| 邻近原接口 | 147通过 | 原授权、请求账本、宿主、Suite及缺证停止；集合与上述结果重叠 |
| 原环境失败 | 562通过、1失败 | 全模块误用umask077，原Delivery按合同拒绝夹具600文件 |
| 同选择器环境负/正对照 | 077下1失败、022下1通过 | 相同原测试与生产源码，无修改权限断言或生产校验 |

初次143项和后继146项分别保留，不累计测试计数。
全模块恢复原测试umask022后通过；私有证据目录保持0700，日志与结构化原件保持0600。
没有将原环境FAIL删除或当成新的真实任务失败。测试集合重叠，不能相加为质量分数。

## 4. 发行物与源码边界

本切片只改变可信验证宿主脚本，产品源码、公共Spec、依赖锁均未改变，验证脚本不在产品Wheel内。
源码外验证复用[上一固定候选实际Wheel](../profile-observation-stop-2026-09-30-v1/README.md)，
来源`ef582dacb5609fee90e6b3905998dea8db269c53`，SHA-256为
`41a30308b3d5f1baf45bbefe654c713ebdab0b386ba698ba7ae574a57901a939`。
447个产品包成员在当前源码、原Wheel和独立安装中逐字节一致。
这不是本轮新构建的Wheel，也不将源码中的管理脚本冒充已安装产品CLI。

Ruff、原结构治理、公共Schema一致性及四个变化脚本的显式包名Mypy检查通过。
三个图已实际渲染并检查：[架构](diagrams/architecture.png)、[时序](diagrams/sequence.png)、
[数据流](diagrams/data.png)。标准文档与Secret门禁结果以[Verification](verification.json)为准。

## 5. 实际账本只读前置

实际原件仍为V1，71条请求的原文件SHA保持
`d48ae4bc2f61f3e065ceadcc2648e460066c6328e2fb2ada32072150c362e88d`。
已知估算1.74186元，旧unknown保守占用20.77824元，原同一40元已用0.219136元。
内存合同校验确认剩余39.780864元可正确表达，没有调用登记方法，没有读取凭据或发请求。
估算和保守预留不是实际账单，不因本切片释放预留、重新分配40元或重置70元周期。

实际登记必须依据明确预算规则，绑定最终完整Suite及源码配置；准备验证不是运行授权。
模型运行仍须原范围、镜像、私有文件、计价窗口和网络开关全部通过。

## 6. 复现与审计索引

验证环境：macOS ARM64、Python3.12.7、Git2.53；测试过程umask022，预算文件权限由原Owner显式0600。
Windows上验证宿主账本按原POSIX边界跳过，这不是Windows产品编码验收。

```bash
python -m pytest tests/evals/test_provider_reverification_rebinding.py
python -m pytest tests/evals
MYPYPATH=src:. python -m mypy --explicit-package-bases \
  -m scripts.provider_reverification_binding \
  -m scripts.provider_verification_budget \
  -m scripts.provider_verification_guard \
  -m scripts.authorize_provider_reverification
python -m scripts.documentation_check
python -m scripts.generate_specs --check
```

[事实](facts.json)、[Verification](verification.json)、[Review Packet](review-packet.json)和
[Manifest](manifest.json)绑定原Reader、当前脚本、实际执行及原失败摘要；不公开私有日志或预算原件。
完整真实20 Trial、至少12严格成功、每仓成功与零越界、消费者平台及独立Beta继续开放。
