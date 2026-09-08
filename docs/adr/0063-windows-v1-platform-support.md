# ADR 0063：Windows纳入Harnessix Code 1.0支持范围

- 状态：已接受
- 日期：2026-09-08

## 背景

ADR 0062将Harnessix Code 1.0定义为面向大量独立终端用户的本地优先商业版本。仅支持macOS/Linux会排除大量Windows开发者，但当前Workspace、Process、Git和Eval实现明确依赖POSIX路径、文件描述符、权限位、Session、Process Group和Signal语义，不能通过增加一个CI标签就宣称Windows可用。

Windows还引入盘符、UNC路径、大小写不敏感、保留文件名、Alternate Data Stream、长路径、文件共享模式、Reparse Point/Junction、Job Object和不同的原子替换语义。上述差异同时影响安全边界、取消、恢复和交付，不允许由零散条件分支处理。

## 决策

1. Windows与macOS、Linux共同进入1.0正式支持矩阵；具体最低系统版本在0.9发行物验证前冻结。
2. 当前版本仍只支持POSIX。只有Windows对应契约、实现、故障测试、安装升级和正式CI全部通过后，README才能将Windows标记为“当前支持”。
3. 0.7建立平台端口，至少分离Workspace文件安全、Process监督、Git调用、Sandbox和发行路径语义；领域模型、Agent Runtime、Provider和Session不得散布平台判断。
4. Windows原生Workspace必须覆盖盘符、UNC、保留名、ADS、大小写折叠、长路径、Reparse Point/Junction、文件锁和替换恢复。安全检查必须在实际打开、执行和提交时复核，不能只做字符串规范化。
5. Windows进程监督使用Job Object等原生能力实现进程树归属、取消、超时和宿主退出核对，不把POSIX PID/Signal假设映射为同名行为。
6. Windows原生Host模式必须准确展示其非强隔离边界；1.0的Windows强隔离优先使用受管WSL2或Docker Desktop后端。后端不可用时不静默宣称Sandbox已启用。
7. 立即增加Windows平台中立CI基线，只验证安装、导入、许可证策略和不依赖OS执行语义的核心测试；该基线不是Windows产品支持证明。0.7逐步扩大契约覆盖，0.9再启用完整Windows E2E、故障注入和发行物门禁。

## 结果

### 正向结果

- 1.0覆盖主流桌面开发环境，与本地优先C端产品定位一致；
- 平台差异被限制在正式端口内，不污染Agent领域语义；
- Windows支持拥有明确安全与恢复门禁，不以“能够import”冒充生产支持；
- 后续若引入Rust Sidecar，可以针对Process/Sandbox端口局部替换。

### 成本与风险

- Windows会显著增加路径、文件锁、进程、Sandbox、安装和签名测试矩阵；
- WSL2/Docker Desktop不是Windows内核级透明替代，宿主路径映射和Secret边界必须单独验证；
- 当前POSIX测试中的权限位、符号链接和进程组Oracle不能原样复用；
- 若0.9前无法满足完整门禁，必须延后1.0，而不是降低Windows支持声明。

## 被否决方案

### 1.0继续只支持macOS/Linux

该方案降低实现成本，但不符合面向大量独立终端用户的产品范围。

### 仅支持WSL2并宣称Windows原生支持

WSL2可以作为强隔离和兼容后端，但不能替代Windows原生Workspace、路径、Git、CLI和安装体验。

### 直接在现有POSIX代码中增加平台条件分支

该方案会把不同安全语义混入同一实现，难以建立可审计不变量和失败恢复测试。
