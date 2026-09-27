---
doc_type: validation-evidence
status: current
version: 1
code_revision: 6b5f5591d4dbe8cb31f99752b3e8950ae62a8bd0
owners: [core]
modules: [agent, app_server, protocol, secrets]
related_adrs:
  - docs/adr/0100-protocol-frame-and-handshake-publication.md
related_tests:
  - tests/app_server/test_frame_publication.py
  - tests/product_config/test_protocol_publication_cli.py
  - tests/governance/test_security_governance_evidence.py
supersedes: []
---

# 原协议帧与握手提交版本绑定验证报告

## 1. 结论与范围

固定实现`6b5f5591d4dbe8cb31f99752b3e8950ae62a8bd0`完成原封套准入、完整原响应UTF8协商限额与当前材料保护、纯握手候选提交及关闭通知。
原Schema、公共导出和一级依赖关系不变；没有数据库迁移，不恢复独立Action Plane服务。
全量**5033 passed / 32 skipped / 403.86秒**；专项**161项 / 4.77秒**（62新增＋99既有）。
新增中1项专门记录未登记旧历史开放，不能计作历史安全验收；另有2项证据治理回归。
相关矩阵2169项 / 85.91秒通过，26项历史/新证据治理通过。各组有重叠，不相加当作独立总数。
真实模型调用0次，未消耗模型API预算。

本切片本地验证完成，**整个0.9及0.9.4a仍未完成，发布仍阻塞**。旧Session授权、跨重启Seal、
直接Service查询、全部Provider材料、Scope失效的SDK相关ID和12项Archive权利仍未关闭。
其他修订CI成功或本地测试通过均不能替代本修订发布矩阵。

## 2. Manifest与固定运行输入

- 固定源码：`6b5f5591d4dbe8cb31f99752b3e8950ae62a8bd0`；完整运行提交：`213790e8cc085715dfd722d611b231867469bd68`，树：`b69731d7304d33315c1e57111f9930305b014aa1`。
- 运行期间2390个跟踪文件逐项Hash与Git状态不变，没有边跑边改资料。
- 1325个执行、测试、合同、脚本、治理和构建输入与固定实现逐字节一致；最终只冻结文档事实。
- 37个关键源输入绑定Git对象；六文件目录中Manifest不递归Hash自身，另外五文件逐字节校验。
- 689个既有验证文件原字节不变，排除允许更新的总索引。
- 未跟踪安全草稿不读取、不修改、不暂存、不计入pytest、Ruff或清洁Git发行构建。

## 3. 独立旧基线与同脚本固定修复观察

探针在干净`735f2e9dcafbe863e36a2890fac1364c4283bca8`归档与固定源码执行，验证实际模块来源；不是关闭新保护器模拟旧版。
使用真实Runtime/SQLite/Protocol Server与合成材料，只持久化布尔事实、版本和脚本Hash。

| 原路径 | 旧基线 | 固定实现 |
|---|---|---|
| JSON-RPC id | 原材料回显，握手进入PENDING_ACK | 固定public_input_secret_leak，id为null，仍NEW。 |
| 未知参数键 | 原键进入invalid_params.path | 分派前public_input_secret_leak，安全原id保留，仍NEW。 |
| 当前材料的旧Replay | 原已登记材料被公开 | 原帧public_output_secret_leak，不发原文，历史不变。 |
| 换版本未登记旧历史 | 旧材料被公开 | **仍开放**；没有历史授权或Seal，不算整改成功。 |

第二个固定实现独立探针直接调用导出的Application Service，确认get/list/resume/replay/next五个DTO仍能含当前已登记材料。
没有通过Server传输门禁，原历史不变、Provider为0；这是下一项明确开放证据，不是产品传输回归失败或安全通过。
详见[contract-facts](contract-facts.json)。

## 4. 测试矩阵与判据

| 组 | 判据 |
|---|---|
| 7类原字段×3编码 | id/method/值/键/ClientName/嵌套键/列表值在Params与握手之前拒绝，无身份/能力/限额变更。 |
| Notification | 拒绝无Response，无ACK更新；关闭后的合法通知不访问已关闭Scope。 |
| 原查询与Replay | 当前已登记旧值的get/list/resume/replay/next完整帧拒绝；原Session不清洗。 |
| 错误 | Kernel/Service/意外异常及动态path不能带原登记材料；只发有限固定保护错误。 |
| 握手候选 | 构造与出口失败后NEW可安全重试；父取消不半提交；关闭不复活；两个候选只有一个胜者。 |
| 不可逆事实 | completed命令后出口拒绝保留原回执；重试不重复领域操作。 |
| 期限与预算 | 输入/输出失效、限额、超时、扩展异常和父取消可验证；不作同步硬抢占保证。 |
| UTF8限额 | 原4095/4096/4097字节含换行边界；超限稳定小错误；大Replay减少页可重试。 |
| 实际stdio | 真实Reader/Writer与产品CLI OS管道，不把InProcess假装成真实进程。 |
| 旧未登记历史 | 专门通过观察证明开放，不将测试通过当成授权通过。 |

实际`python -m harnessix agent-server`子进程装配临时真实配置、Provider、Scope、SQLite，使用OS stdin/stdout。
顺序请求验证敏感id/键拒绝、正常initialize/ACK、空列表、EOF退出；stderr为空、stdout/数据库不含合成材料。
无Thread或Turn接受；Provider仅构造关闭，没有网络模型请求，也不证明真实Provider协议兼容。

## 5. 清洁发行物与独立Wheel消费

从固定Git清洁归档构建，不打包未跟踪草稿。Wheel和sdist候选发布物Secret扫描通过，
原Runtime、帧保护、握手与Server模块和固定Git字节一致。构建物是固定源码测试候选，不是正式发布；
源归档中的文档属于全量冻结前版本，不能将该sdist当作最终完整文档交付或安装验收。

| 发行物 | SHA-256 |
|---|---|
| Wheel | `27d681cd81d5d8298270f50acb19eddc085e7c91d4b44bc1df6c930241cb8e5e` |
| sdist | `2ba5446ff7ea36efc47e608c4a27a352a9b534669134f7d730570b757b50ca88` |

隔离模式`python -I`从压缩Wheel导入新模块，断言实际模块路径；不引用项目源码或测试助手。
实际Runtime/SQLite/SDK验证敏感id/键拒绝、当前登记旧Replay拒绝、私有历史不变和安全原文往返。
确定性Provider消费1次；真实网络模型0次。复用本机依赖环境，不声明干净机器安装、可复现构建或三平台发行验收。

## 6. 文档、六幅图和质量门禁

[详细设计](../../changes/m09-4a-protocol-frame-publication.md)含需求、选型、总体架构、流程、两种时序、数据流、
类/接口/字段、伪代码、失败/取消/恢复、部署与固定源码行导航；ADR及四份模块文档、总体架构、路线图同步。
五幅新设计图＋更新后的模块流程图均真实渲染、逐幅视觉核验，源和PNG Hash保存于verification；未用语法检查冒充渲染。
Ruff、格式、Mypy347源码、结构可读性、文档、Schema、任务包、SBOM、Secret自检通过。
许可证门禁继续因**12件Archive权利**失败，不豁免、不改策略、不将单项成功声称为make check全部通过。

## 7. Review Packet、CI与剩余发布阻塞

[verification](verification.json)区分全量、专项、Wheel和各门禁；[Manifest](bundle-manifest.json)绑定输入及五文件；
[Review Packet](review-packet.json)保持release_blocked；[CI快照](ci-observation.json)在冻结时未推送该切片，
既有运行36340715417开工快照为in_progress，不冒充当前修订通过。
本地多个提交合并一次推送，CI后台运行，按最终提交取一次有界快照，不逐提交等待、循环轮询或重试许可失败。

剩余：未知旧历史/跨重启Seal、直接Service查询、全部Provider凭据、Scope不可用SDK相关ID、
Owner同步阻塞/Store归属、编号威胁测试、远程MCP身份/出口、三平台真实安装升级/Beta和真实Provider成本。
固定紧急控制帧是有限常量＋已检查id或null的明确例外；当前材料检查不是任意DLP，不改变原正文/Hash/幂等身份。
