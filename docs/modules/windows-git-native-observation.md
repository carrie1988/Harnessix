---
doc_type: module-design
status: current
version: 1
code_revision: b8123324908d1a27dee9a49dd52a0a23de49e6a3
owners: [core]
modules: [windows-git-native-observation]
related_adrs: []
related_tests:
  - tests/governance/test_windows_git_native_failure_projection.py
supersedes: []
---

# Windows Git 定点观察模块

## 模块摘要：需求背景与设计目标

`scripts/windows_git_native_branch_observation` 是显式定点诊断脚本，不属于产品默认
Git operation 生命周期。原契约固定原两例、16 项源输入、13 hook、官方 PE/PDB
匹配与实际 PATH 身份，预算为 20/45/300/240 秒。原默认只预检，非授权原生运行不会执行。

## 总体架构、接口与源码映射

| 源文件 | 接口与职责 |
|---|---|
| `contract.py / contract.json` | 原固定合同、重复键拒绝、16 输入摘要 |
| `preflight.py / identity.py` | 原 PATH、PE/PDB、解释器与 CDB 绑定和重检 |
| `diagnostics.py` | 原有限前置失败原因 |
| `projection.py` | 原九字段 `project_case` 与实际 marker 分支见证 |
| `failure_projection.py` | 新三个原有限字段的精确校验、固定对象重建、sibling 隔离 |
| `run_cases.py` | 原 CaseSink 接帧与 pytest 原两例；添加独立 sibling 报告 |
| `observe.py` | 原完整 gate 与 v3 result；独立未认证失败观察 |

原案例与认证门不消费新增数据。当前 result v3 在
`unverified_execution_observation.failure_observation` 下包含独立
`harnessix.git-native-failure-observation/v1`，assurance 固定为
`UNAUTHENTICATED_DIAGNOSTIC_ONLY`。`FINITE_AB` 只是有限结构，并不是认证或效果结论。

三个字段逐项区分 `FINITE / NOT_AVAILABLE / REJECTED`：原九项 bool 不扩展，
失败状态仅允许 `not_observed / invalid / valid`，Trace2 仅保留原七字段固定投影。
缺失与坏值不降格成 false；未知额外字段不通过宽松 parser 透传。

## 数据、异常与安全

只读取原观察器既有两个文件，原大小门不变；helper 仅处理已接收的内存 JSON，
不增加探针 hook 或业务 raw 读取。原异常类、write 边界、完整 gate、状态和非零退出保持。
不输出正文、操作路径、SID、PID、errno 或 MAC。合法失败观察不改变 SDK false 和 Root UNKNOWN。

## 开发与测试

环境按当前锁文件离线装配：`uv sync --offline --frozen --no-install-project --group dev`。
执行时设置 `PYTHONPATH=$PWD/src`、`PYTHONDONTWRITEBYTECODE=1`，显式选择专项测试文件，
不要运行宽泛的 `tests` 或安全验证目录。离线夹具使用系统临时目录：
原 preflight 会拒绝含空格路径，原 observer 会拒绝仓库内报告路径。
不得为迁就夹具放宽生产前置；验证环境须登记实际Git路径、版本与能力，不将宿主前置失败解释为Windows根因。

专项命令：

```bash
PYTHONPATH="$PWD/src" PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
  -p no:cacheprovider tests/governance/test_windows_git_native_failure_projection.py
.venv/bin/ruff check scripts/windows_git_native_branch_observation \
  tests/governance/test_windows_git_native_failure_projection.py
```

部署和维护采用独立 helper 与两个最小脚本增量，没有业务持久化迁移。
增量实现必须以逐文件 SHA256 而不是兼容基线或旧原生 revision 标识。
旧冻结包保持原字节；原结果没有这三个字段时不补查 raw，也不回填根因。

完整合同、有限枚举、负例、取舍与源码导航见
[有限失败投影详设](../changes/m09-r4-windows-material-fault-projection.md)。
既有执行/预检架构见 [v3 封装设计](../changes/m09-r4-git-native-execution-envelope-v3.md)，
其中历史候选SHA不能作为当前增量脚本的身份。

## 核心流程与数据流

原v5 Probe一次发布 → 原CaseSink边界 → 原九字段案例与独立三字段投影 →
固定cases.json → 原observe严格隔离/复验 → result.json的未认证观察。
完整gate只消费原案例和原marker；新投影不参与身份、效果或SDK放行。
接口与领域字段逐项解释见[详细设计](../changes/m09-r4-windows-material-fault-projection.md)。

## 失败恢复、取消与超时

字段缺失保留NOT_AVAILABLE，类型/键/枚举冲突为REJECTED；坏载荷不透传。
原执行期限、日志上限、异常类型和退出状态保持；该纯内存接缝无重试或恢复执行。
历史结果没有保存的新字段不得回填，有限观察不能制造完整分支见证。

## 当前限制与风险

code_revision仅为兼容基线，增量身份以发布验证包的完整源码摘要为准。
当前没有新Windows原生执行证据，Git128和Root UNKNOWN保持；离线验证不替代
消费者Windows环境、SDK成功或完整Git业务交付。该脚本不进入默认产品运行时。
