---
doc_type: adr
status: current
version: 4
code_revision: de1ab2722f91d6109e807617aa1e07c8a502c9e5
owners: [core]
modules: [evals, observability, sdk]
related_adrs:
  - docs/adr/0120-system-neutral-evaluation-platform.md
  - docs/adr/0081-single-coding-agent-product-boundary.md
  - docs/adr/0082-multi-repository-eval-suite-and-transcript-evidence.md
  - docs/adr/0107-authenticated-eval-host-and-history-read.md
related_tests:
  - tests/evalkit/test_langfuse.py
  - tests/evalkit/test_harbor.py
  - tests/integrations/test_harnessix_eval.py
  - tests/integrations/test_harbor_protocol.py
  - tests/integrations/test_harbor_adapter.py
supersedes:
  - docs/adr/0109-evaluation-integrations.md
---

# ADR 0110 Langfuse只读导出与Harbor协议适配

## 1. 决策

本地Session、动作、用量及价格账本继续负责执行事实。Langfuse接收原生评测报告的只读投影，
Harbor通过既有Agent Protocol启动产品App Server。评测、导出和第三方评分不反向控制执行状态。

在Suite导出和Harbor协议适配器基础上，加入原生Harbor报告、真实服务读回与完整容器验收矩阵。
逐模型调用实时Trace、全量账单、生产自动审批及公开基准质量实测由后续阶段分别验收。

## 2. 源码与合同边界

[ADR 0120](0120-system-neutral-evaluation-platform.md)把下列实现迁出产品wheel，报告合同改为
evalkit的运行报告；本节的边界与第3、4节的约束保持不变。

- [投影](../../packages/evalkit/src/evalkit/langfuse/projection.py)只消费严格重算的运行报告；
  Suite报告经[回填](../../integrations/harnessix_eval/suite.py)转换，逐试次与原结论一致。
- [文件读写](../../packages/evalkit/src/evalkit/files.py)采用0600原子私有写入并拒绝符号链接。
- [传输](../../packages/evalkit/src/evalkit/langfuse/transport.py)发送OTLP/HTTP JSON与score-create批次。
  Trace不使用旧Ingestion API；评分采用其仍受支持的事件形式以保留原事件时间。
- [Harbor适配器](../../integrations/harbor/harnessix_agent.py)依赖单独锁定的Harbor 0.25.0，
  [协议驱动](../../integrations/harbor/protocol_runner.py)仅消费公共SDK，不实现模型循环。
- [Harbor导入](../../packages/evalkit/src/evalkit/harbor.py)按计划槽位逐个处理原始结果，
  拒绝身份冲突及隐藏自动重试；Harnessix原生字段由
  [协议摘要解析器](../../integrations/harnessix_eval/harbor_parser.py)映射，只进入system_refs。
  它不补造原有CodingEvalReport的14项检查，不把单仓库结果转换成完整多仓库Suite。
- [读回](../../packages/evalkit/src/evalkit/langfuse/readback.py)采用v4公开Experiments及
  Experiment Items API，核对根Observation、评分值、归属、原币种和计数，并保存绑定源摘要的私有收据。

## 3. 适用条件与副作用

导出显式读取操作者指定的完整报告，只选择身份、版本、结果及汇总用量字段；
默认只写0600文件。只有明确指定联网开关和服务地址后才读取导出凭据并发出HTTP请求。
Prompt、Diff、错误正文、仓库URL和原始Session不进入投影。
Harbor v1映射固定标准reward==1，输入锁和观测的Harbor自动重试次数必须明确为0。
缺失结果保留为invalid；槽位只关联计划，物理执行以Harbor试次及原生Turn身份关联。
Job无时区时间必须显式解释，夏令时歧义拒绝猜测。

Harbor的任务镜像需预装Harnessix、原生配置及运行依赖；配置和状态位于工作区外。
当前驱动遇到审批或提问会请求取消，不自动扩展权限。Turn终态只表示协议观察，
不能证明任务通过或全部外部进程已经清理；验收由独立Verifier完成。

## 4. 幂等与失败恢复

试次Trace、实验及评分身份由原报告指纹和相应执行或槽位身份确定。重发保留评分名称、ID及原时间；
传输不跟随重定向、不自动重试，拒收或部分接收返回固定原因码并保留本地文件。
恢复导出不重跑Agent。已有产品状态需由原恢复流程处理，Harbor驱动不自动重启旧试次；
结果文件在启动App Server前独占占位，重复执行不能先调用模型再发现文件冲突。

Suite聚合用量作为元数据，保持输入含缓存、输出含推理的原口径，不伪造generation。
已知CNY成本不填入USD字段，未知成本不补零。Harbor公共Turn缺少全量Attempt完整性信息，
当前只记录观测用量元数据，不填写其标准总Token和美元费用。
公共协议完整性只在确认Turn终态并完成全部重放后成立，不等同于全量账单。
读回支持空游标页、异步可见性和毫秒时间精度；错误绑定、重复项及数据不一致不能返回verified。

## 5. 验证与范围

验证覆盖失败分母、固定身份、字段白名单、私有文件、网络开关、部分接收、超时及协议取消。
Oracle/Nop夹具检查Docker环境与独立验收的正负路径，不计作Harnessix编码能力或公开榜单成绩。
真实产品矩阵覆盖只读review、错误答案、审批中止、超时和Provider失败；本地确定性HTTPS Provider
仅用于协议、工具、容器和验收链路验证。验收任务通过率1/5与五类场景测试通过是不同口径。
夹具显式使用Docker network_mode:none，逐实例观测实际镜像、网络和清理，不把Harbor的
sidecar控制模式误称为内核none模式。本机Langfuse镜像及持久化凭据分别固定，不进入产品uv.lock。
使用方法见[接入操作说明](../operations/evaluation-integrations.md)。
