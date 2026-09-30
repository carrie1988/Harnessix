---
doc_type: validation-evidence
status: current
version: 2
code_revision: 89b3cd1356ed3e6b1fdfa6b957a876c0d30d2964
owners: [core]
modules: [sandbox, evals, deployment]
related_adrs:
  - docs/adr/0066-sandbox-network-and-secret-boundaries.md
  - docs/adr/0088-controlled-real-provider-suite-baseline.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/integration/test_container_sandbox.py
  - tests/integration/test_task_pack_execution.py
  - tests/evals/test_provider_verification_host.py
supersedes: []
---

# 固定镜像与显式Engine评测宿主前置验证

## 1. 背景、目标与结论边界

工程Task Pack要求两个原始RepoDigest的检查镜像。本机Docker Desktop保存HTTP/HTTPS代理及镜像源
绕过规则后，实际日志已证明绕过生效，但Daemon认证令牌请求仍返回`EOF`。与此同时，默认Desktop API
启动新容器停在注册阶段。不能将保存设置、`docker info`或既有容器健康推导为编码评测可执行。

目标是恢复固定公开镜像、验证实际Workspace挂载和原安全约束，并恢复原宿主状态；不修改Agent、
Task Pack、Grader、模型、评分阈值或产品默认入口。生产源码基线为上述Revision；新增验收断言以
[测试源文件摘要](facts/test-results.json)绑定。

**固定镜像及显式连接同一Engine的评测宿主前置专项GO；默认Desktop启动路径NO_GO。**
5项真实容器/录制Provider集成测试及151项关联离线回归通过。真实模型请求为0；没有新的20 Trial成绩，
不关闭R3质量、R4消费者平台或其他商用门禁。原失败保持，不能用显式入口的成功替代默认入口结果。

[验证断言](verification.json)、[Review Packet](review-packet.json)和[字节清单](manifest.json)
提供有限结论、开放项及发布文件身份。公开内容是有限字段投影，原私有日志、Workspace、状态库与路径不复制。

## 2. 架构、数据流与宿主边界

```text
Docker Hub公开HTTPS对象 → 固定Index/平台Manifest/Blob逐件SHA校验
    → 本机只读白名单缓存 → VM回环只读缓存 → Engine按原RepoDigest原生拉取
    → 移除缓存、恢复原Daemon配置及容器运行/重启策略

原Agent/Action/Process链 → 固定CLI宿主绑定 → 同一Docker Engine
    ├─ 默认Desktop API：注册阶段Created，25秒观察超限后清理
    └─ 显式Engine API：原Workspace只读挂载、固定资源及network=none实际执行
```

缓存只包含Python/Node固定Linux ARM64平台的14条公开对象路由，共74,021,204字节。
外部下载保持HTTPS校验；内部HTTP只监听宿主及VM回环，拒绝未登记路径，没有推送、私有镜像、
凭据或自动上游代理。镜像仍由Engine按原引用原生拉取；没有`docker tag`、替换摘要或修改评分器。
原Index到平台Manifest及所有Blob的摘要链均校验，[传输事实](facts/image-transport.json)记录失败尝试和最终路径。

显式宿主包装仅给原Docker CLI固定同一Engine端点，不重写Profile参数、Mount路径、命令或输出。
真实Marker证明两入口指向同一受控Workspace；[挂载对照](facts/mount-controls.json)保留Created失败和成功。
[CLI绑定摘要](facts/engine-binding.json)区分验证宿主与原厂CLI。包装不进入产品默认安装或自动降级路径。
这不是默认Desktop修复，也不新增远程执行能力。

## 3. 网络验收缺陷归因与断言设计

原容器边界测试使用`ls /sys/class/net == lo`。本机无网络命名空间还包含DOWN的GRE等默认隧道接口，
以及非目录条目`bonding_masters`；所有非回环接口均无`IFF_UP`，IPv4没有非回环路由，IPv6地址及路由只关联`lo`。
用户、零有效Capability、CPU/内存/PIDs检查符合原限额。原4项集成测试因此出现3通过/1失败，失败并非
Task Pack成绩，也不能仅凭接口名字认定网络可用。

[Linux v6.10源码](https://github.com/torvalds/linux/blob/v6.10/net/ipv4/ip_tunnel.c)的
`ip_tunnel_init_net`可按网络命名空间创建Fallback接口；`ip_tunnel_lookup`会检查`IFF_UP`。
该源码支持默认隧道接口产生机制，但没有证明Docker内部EOF或注册挂起的排他根因。

修正只位于[集成测试](../../../tests/integration/test_container_sandbox.py)，不改变
[生产argv构造](../../../src/harnessix/sandbox/container.py)或
[固定Pack Profile装配](../../../src/harnessix/evals/task_pack.py)。
共享测试常量`_NO_EXTERNAL_NETWORK`按下列顺序执行：

```text
要求lo启用
遍历实际接口目录，跳过非目录条目
对所有非lo接口：若IFF_UP已设置，退出41
若IPv4路由指向非lo接口，退出42
若IPv6地址或路由关联非lo接口，退出43
继续原只读根/Workspace、资源限额和Secret脱敏检查
```

真实Bridge负对照复用同一断言，必须因已启用非回环接口退出41；仅运行本地状态读取，不发外部网络请求。
不能通过跳过测试、取消`network=none`、忽略非回环路由或放宽能力/资源限制接受环境。
完整设计与源码映射见[Sandbox模块](../../modules/sandbox.md)。

## 4. 实际流程、失败语义与恢复

1. 保存原Daemon字节、8个运行容器及5个停止竞争容器的运行状态和重启策略。
2. 每次受控重启前暂时禁用这5个停止容器的自动重启，避免端口竞争；不删除容器或数据。
3. 保存规则后再次拉取，记录认证`EOF`；宿主缓存经Desktop代理失败，后续VM回环路径完成两个原引用拉取。
4. 删除自有VM缓存容器、停止宿主缓存，恢复Daemon原字节并重启验证原镜像源；恢复5个原重启策略。
5. 默认/显式入口使用相同Workspace Marker和固定资源约束。默认容器25秒仍Created，仅记观察超限，
   不把控制客户端结束当作Engine终态；经同一Engine只清理自有探针并验证返回204。
6. 原Agent、审批、Process和Grader链执行录制Task Pack及崩溃恢复；原网络断言失败保留后再运行修正及Bridge负对照。

[恢复事实](facts/restoration.json)确认原8容器、停止集合、重启策略及运行镜像源恢复。
没有安装新中间件、修改用户业务配置或清除已有数据。默认注册挂起仍需独立处理；后续真实评测须显式冻结
宿主入口，不自动尝试其他端点或把有限环境认证扩成全平台承诺。

## 5. 测试、可观测性与后续门禁

| 检查 | 实际结论 | 证据边界 |
|---|---|---|
| 原固定Python/Node镜像 | 两个原RepoDigest、Linux ARM64一致 | [镜像身份](facts/images.json)；不是签名或发行供应链认证 |
| 默认入口挂载探针 | Created观察超限 | 不标记成功；已清理自有容器 |
| 显式同Engine挂载探针 | Marker、退出0、none、只读根和Workspace、0.5 CPU/128 MiB/32 PIDs | 有限评测宿主，不是默认Desktop平台认证 |
| 初始录制Task Pack/Container | 3通过、1失败 | 保留原失效断言，不覆盖 |
| 修正后真实集成 | 5通过、0失败、0跳过 | 包含完整录制Agent链、恢复、Secret边界、MCP及Bridge负对照；不是模型质量成绩 |
| 关联离线回归 | 151通过 | 原安全和验证宿主合同；不替代真实运行 |
| 模型费用 | 本切片请求0 | 不能从零请求外推供应商认证成功 |

[测试投影](facts/test-results.json)保存原JUnit摘要和精确计数，不公开其中的私有路径。
容器参数、实际退出、清理状态和原故障按独立事实记录；没有靠健康检查或合并不同运行得出成功。
后继真实运行在`0813c58`中断，原未知费用预留仍待核对，见[原中断事实](../provider-suite-interruption-2026-09-30-v1/README.md)。
恢复真实请求前须按原预算合同处置未决事实；不得新建预算周期或用Usage字段自动追认成功终态。
费用核对完成后，仍须在干净Revision上预注册完整3仓10 Case/20 Trial并由原持久预算宿主执行真实Provider，
严格任务及必需测试均至少12/20、各仓有严格成功且无未授权修改/损坏/重复高风险效果；原0/20保持。
消费者Windows11、独立Beta及最终同候选发布门禁继续开放。

## 6. 默认Desktop启动链的后继对照与宿主回退

本节绑定`df8dc8f124a1d8f1a6fafac480785e06ab8cfa97`及
[独立后继事实](facts/default-api-followup.json)，不重写前五节的镜像传输、5项集成和151项回归身份。
探针只挂载自有非敏感Marker，保持原镜像、只读根、无网络、资源及权限限制；没有挂载或读取用户业务文件。

### 6.1 已排除的单一归因与组件证据

| 对照 | 默认入口实际观察 | 可支持的结论 |
|---|---|---|
| 无挂载、`start --attach` | 12.078秒时CLI仍活动、容器Created | 故障不只发生于Workspace挂载路径 |
| 临时目录或受管目录挂载 | 约12秒时仍Created | 更换已共享目录未使启动完成 |
| 无挂载或有挂载、`start`不带attach | 约12秒时仍Created | attach不是唯一触发条件 |
| 相同Workspace、显式同Engine | 0.512秒退出0并读回Marker | Engine直接路径可执行，不等于默认Desktop修复 |
| 暂时改为gRPC FUSE后重启 | 无挂载、有挂载均仍Created | 文件共享后端切换没有产生改善，不能保留为修复 |

默认API对应的本地Backend堆栈包含8条匹配调用链：

```text
proxyStart.RequestRewrite → grpcfuseClient.Add → volumeShareClient.Add
    → ClientConn.Invoke → ClientConn.getTransport → pickerWrapper.pick
```

这将定位范围收敛到启动请求重写中的卷注册/gRPC传输等待。原捕获文件恰为1 MiB，可能截断；
不能声称全量线程覆盖，也不能把全部8条线程逐一归属于自有探针。
现有Unix Socket可以连接，不等于gRPC服务健康。尚未证明排他的底层根因，不猜测模型、代理或资源不足导致此等待。
12秒是受控观察窗口，不是Engine失败终态；清理只针对身份与自有标签一致的探针，全部清理返回0。

### 6.2 回退、原状态与证据时效

后端实验只改VirtioFS→gRPC FUSE，失败后回退VirtioFS，共两次受控重启。
每次重启前保存28个原容器，暂时关闭其中9个原停止容器的非`no`自动重启策略，防止新增端口竞争。
回退后28个原ID、全部重启策略及运行/停止集合均与保存值一致：8运行、20停止。
未删除原容器、原数据或修改业务配置，未升级Desktop、改代理/绕过规则或调整共享目录与资源。
这些9个策略属于本次完整快照，不改写前一镜像专项只涉及5个竞争容器的历史记录。

后续独立只读观察发现Desktop进程及默认/显式Socket均不存在，Daemon不可连接；原因没有确证。
本观察没有再次启动、重启或修改设置，因此**不能继续宣称当前8容器运行或宿主仍可评测**。
再次使用前须核对当时运行集合并重新验真固定宿主；历史恢复成功和历史集成PASS不具有永久有效性。

### 6.3 正式链复验与Go/No-Go

回退后原容器状态完成核对时，正式Container及Task Pack集成取得**6通过、0跳过/失败/错误**，
JUnit耗时16.590秒。范围包含实际只读/网络/资源与Secret边界、Bridge负对照、MCP stdio、
录制Agent任务及两种Selector参数形式的持久恢复；Provider为录制实现，真实模型请求0。
不与历史5项或151项相加，不计入真实20 Trial成绩。

有限显式Engine录制链GO，默认Desktop启动仍NO_GO；后续宿主停机使当前可执行性未经验证。
不新增产品端点自动降级、不调整Grader、不放宽隔离、不以持续重启替代根因证据。
真实费用核对、完整Suite、消费者平台、独立Beta及R1～R6商用门禁均保持开放。
