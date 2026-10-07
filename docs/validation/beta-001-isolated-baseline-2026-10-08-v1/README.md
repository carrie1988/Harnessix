---
doc_type: validation-evidence
status: current
version: 1
code_revision: b0b12638c5437b230147a1f027e6172cab283923
owners: [core]
modules: [sdk, product_config, evals]
related_adrs:
  - docs/adr/0071-headless-app-server-and-sdk-lifecycle.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/models/test_chat_text_tool_boundary.py
  - tests/tools/test_scoped_runtime.py
  - tests/product_config/test_agent_context.py
supersedes: []
---

# BETA-001 独立隔离输入与业务前测

## 1. 结论与验收边界

`PASS_SCOPED_OFFLINE_BASELINE_WITH_OPEN_BETA_GATES`。建立可离线验证的业务前测输入；
前端构建/类型检查/18测试及后端编译/18认证API测试通过。未生成登录整改、未执行Harnessix受控Profile、
未验证浏览器或生产HTTPS，真实Beta完成仍0；[原四次分析失败](../beta-001-readonly-analysis-2026-10-07-v4/README.md)保持。

## 2. 输入来源、隐私转换和闭包

419件合成输入来自原冻结初始选集，81处已分类Span、18文件只替换同用途测试凭据。
433件准备Manifest、419源码摘要、非Span逐字节反向复原和独立inode通过主审；未知凭据绝对不存在并非结论。
原419选集明确排除了必需源码和配置，首次后端编译失败是选集不完整，不作为原业务代码缺陷。

后继从初始冻结Git提交`cd0cf278fce916db8ea19378588490c795e5307f`只读取得2件Java和2件运行配置，
新增3处同Scope测试凭据替换，形成另一个423件私有输入。Git读取有禁网与写入限制，不执行原业务应用、
fetch、安装或构建；不使用整改参考件，不改旧419输入、外发许可、模型候选或失败记录。
运行配置原件仅私有保存；测试使用新合成签名/加密Key、空供应商凭据及新临时工作目录，不能作为模型外发输入。

## 3. 隔离契约与工具链

macOS sandbox-exec拒绝全部网络，写入仅允许新私有目录及设备节点；测试过程拒绝读取原业务目录、
整改参考件及Keychain。实际探针确认目录外写入、回环网络和原业务读取均被拒绝。
HOME/TMPDIR及日志置于新目录，使用空环境；测试配置不继承真实Key或旧业务环境。
不承诺该宿主策略等价于产品Docker Profile或三平台Sandbox，也不证明整个原目录正文长期未变。

Node22.23.2、Java17.0.6、Maven3.9.16；npm严格冻结原锁并离线安装185包，不执行安装脚本。
Maven只读复用已缓存依赖，按实际原缓存的`aliyunmaven`仓库身份使用空凭据设置、离线模式；未改缓存身份或版本。

## 4. 实际结果与原失败

| 阶段 | 原件结果 | 边界 |
|---|---|---|
| 前端离线安装 | 185包，exit0 | 原npm锁未变 |
| 前端build/typecheck | 均exit0 | 大分块警告保留；未启动dev proxy |
| 前端现有测试 | 3文件18 PASS | 无新增登录整改用例 |
| 419输入后端编译 | FAIL | 缺少隐私选集排除的必需类 |
| 423输入后端编译 | 269生产/78测试源码通过 | Java17，不是全部资源/业务回归 |
| 首次认证/API测试 | 18 ERROR | Mockito inline动态自挂载初始化不可用，原件保留 |
| 显式启动Agent复测 | 18 PASS | 同源码/测试/网络与写入策略；不改业务或Mock逻辑 |

[Byte Buddy官方说明](https://bytebuddy.net/partial/tutorial.partial.html)支持在JVM启动时使用Agent。
本次仅为隔离测试进程显式装配现有锁定1.14.19依赖，其缓存SHA1及Premain-Class核验通过；
没有启用动态挂载、扩大目录权限或升级Mockito。原包装器配置错误、离线仓库身份拒绝和读取数量上限拒绝均单独保存。

## 5. 证据与费用

私有交付目录分别封存输入清单、精确Span、源绑定、策略/探针、实际命令、退出码、日志、原失败JUnit及最终JUnit。
419/423源文件前后摘要单独核对，生成的target/dist不混入初始源清单；不公开原配置、代码、测试日志或个人路径。
[事实](facts.json)和[评审包](REVIEW_PACKET.md)仅含受审元数据。
本业务前测无模型请求、Keychain或预算Owner调用；不把测试通过计为免费模型整改或实际费用结算。

## 6. 后续必要条件

423输入仍未放行模型外发。完整业务资源/回归、真实Harnessix Patch与审批、受控Profile执行、
浏览器Console/Request Payload分项验证、取消/恢复、使用者验收和发布门禁均开放。
见[正式先导任务契约](../../operations/pilot-tasks/001-login-password-protection.md)。
