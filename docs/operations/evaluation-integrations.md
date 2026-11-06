---
doc_type: deployment-design
status: current
version: 3
code_revision: de1ab2722f91d6109e807617aa1e07c8a502c9e5
owners: [core]
modules: [evals, observability, sdk]
related_adrs:
  - docs/adr/0110-evaluation-integrations.md
  - docs/adr/0120-system-neutral-evaluation-platform.md
related_tests:
  - tests/evalkit/test_langfuse.py
  - tests/evalkit/test_harbor.py
  - tests/evalkit/test_grading.py
  - tests/integrations/test_harnessix_eval.py
  - tests/integrations/test_harbor_protocol.py
  - tests/integrations/test_harbor_adapter.py
supersedes: []
---

# Langfuse与Harbor评测接入

评测平台核心是独立包[evalkit](../../packages/evalkit/)（[ADR 0120](../adr/0120-system-neutral-evaluation-platform.md)），
不进入产品wheel、uv.lock和SBOM。Harnessix一侧的适配位于[integrations/harnessix_eval](../../integrations/harnessix_eval/)。
所有evalkit证据都绑定执行前冻结的运行计划；由已有报告推出的计划标记为backfilled，不能作为预注册证据引用。
本地报告及账本保留权威性。逐调用实时Trace、全量费用账单和生产无人值守权限策略仍单独验收。
迁出产品包前的五场景容器与Langfuse读回证据见
[第二阶段验收报告](../validation/langfuse-harbor-2026-11-05-v2/README.md)（historical）。

以下命令在仓库根目录执行，先让evalkit与适配器可导入：

```bash
export PYTHONPATH="$PWD:$PWD/packages/evalkit/src"
```

## 1. 离线准备Langfuse投影

Suite报告必须满足既有Reader的0600私有文件要求；版本库中的历史报告可能为0644，
需要时制作私有副本并保留原件摘要。父目录需预先存在，输出不能覆盖源文件。

```bash
uv run --frozen python -m integrations.harnessix_eval from-suite \
  --suite-report /path/to/private-suite-report.json \
  --output /path/to/run-report.json

uv run --frozen python -m evalkit export \
  --report /path/to/run-report.json \
  --output /path/to/langfuse-export.json
```

回填逐试次核对evalkit判定与Suite原结论一致，计划时间取Suite计划时间，源报告摘要记入source_refs。
prepared_offline表示本地投影已生成；不会执行Agent、读取Langfuse凭据或访问服务。
全部槽位都生成Trace，失败、缺失和无效试次保留在分母中。

## 2. 显式发送

预先通过本地密钥管理方式注入LANGFUSE_PUBLIC_KEY和LANGFUSE_SECRET_KEY，命令中只使用环境引用。

```bash
uv run --frozen python -m evalkit export \
  --report /path/to/run-report.json \
  --output /path/to/langfuse-export.json \
  --allow-network \
  --langfuse-base-url http://127.0.0.1:3300 \
  --verify \
  --readback-report /path/to/langfuse-readback.json
```

远端地址要求HTTPS，本机允许HTTP。自定义部署前缀保留；客户端不跟随重定向、不读取环境代理。
Trace发送到api/public/otel/v1/traces；评分以score-create批次发送到api/public/ingestion。
后者保留源证据的时间戳，适用于仍支持score-create的Langfuse版本。
每批必须逐项确认；错误、部分接收或超时返回非零，本地投影保留。
省略--verify时，submitted仅表示传输响应已确认。
使用--verify时通过v4公开Experiments和Experiment Items API读取真实根Observation及评分，
核对全部试次、输入输出摘要、身份、原币种、数值、评分归属及重复项后才返回verified。
不使用已经移除的旧Trace GET。默认读回预算120秒，可用--verify-timeout-seconds调整，最大600秒。
API时间精度为毫秒，核验允许不足1毫秒的截断；整数元数据仅接受原整数或规范十进制字符串，
布尔元数据仅接受布尔值或JSON规范的true/false字符串，不按数字或任意字符串推断真值。
核验收据以0600原子写入，绑定source_report_digest；这项验收使用真实API，不依赖浏览器截图。

同一报告重发使用相同实验、Trace、Span、评分ID与时间；不会重跑原任务。
每个组别是一个实验；报告改变（例如补充判分）产生新的实验身份。
同一数据集条目的同一重复序号在各组别、各报告间共享条目ID，可直接对照。
Suite报告未包含预算明细，不能仅凭此投影证明预算公平；对照实验仍需核对冻结的原始计划。

导出只有真实试次的起止时间与结构化摘要。聚合Token不生成伪造的逐调用generation，
不会由平台再次按模型价格推算成本。已知成本以原币种和原始金额字符串放入元数据，
partial/unknown沿用原值。评分名称为“判分器/分数”，另有主指标结果outcome与耗时。

## 3. Harbor独立环境

Harbor的依赖独立安装，产品uv.lock和Provider SDK约束保持不变。

```bash
uv venv integrations/harbor/.venv --python 3.12
uv pip install --python integrations/harbor/.venv/bin/python -r integrations/harbor/requirements.txt
```

适配器基于Harbor 0.25.0已安装接口验证。其依赖与当前官网主干文档可能不同；
升级时先验证install/run/populate_context_post_run和结果合同。

任务镜像内需预装产品wheel及其锁定依赖，提供原生产品配置；
主机只加载Harbor适配器，不复制宿主凭据或状态。原生配置负责选择模型，
适配器拒绝会被静默忽略的Harbor模型覆盖。

```bash
HARBOR_TELEMETRY=0 PYTHONPATH="$PWD" integrations/harbor/.venv/bin/harbor run \
  --path /path/to/task \
  --agent integrations.harbor.harnessix_agent:HarnessixAgent \
  --n-attempts 1 --n-concurrent 1 --max-retries 0 \
  --ak server_config=/opt/harnessix/product.json \
  --ak action_config=/opt/harnessix/actions.json \
  --ak python_executable=/opt/harnessix/bin/python \
  --ak workspace=/app \
  --ak state_directory=/tmp/harnessix-state \
  --ak max_steps=16 --ak max_tokens=50000 --ak timeout_seconds=300
```

运行配置显式固定n_attempts=1、n_concurrent_trials=1和retry.max_retries=0。
action_config可选；省略时采用产品内建Action配置，其中没有Process Profile。
需要执行测试时，预先在镜像内提供已冻结的原生Process Profile，不能由适配器自动补权限。
任务的Harbor agent.timeout_sec需大于原生timeout_seconds并预留取消和进程退出时间；
上例建议至少330秒。不要直接使用30秒环境控制夹具运行300秒的原生预算。
完整模型调用、审批与工具执行仍由App Server负责。等待输入或审批时驱动请求取消并记录
intervention_required；当前不自动批准、不自动恢复已有状态，不宣称支持任意终端任务。
原始Prompt只通过临时输入文件传给SDK，不拼进Shell命令或写入协议摘要。

turn_terminal_confirmed仅说明已观察到协议终态。全量Attempt证据、美元费用、
原生进程清理与跨进程恢复仍需独立验证。Harbor标准Token和cost_usd在当前适配器中保持未知；
公共Turn的观测值仅放入harnessix元数据，不将completed转换成任务通过。

## 4. Harbor运行导入与判分

运行Harbor前先冻结运行计划：数据集条目ID为Harbor任务名，条目摘要为Harbor对任务目录的内容哈希
（与Job锁中的task.digest相同）。组别的system必须等于Harbor结果中的智能体名称。

```bash
uv run --frozen python -m evalkit import-harbor \
  --plan /path/to/plan.json --arm harnessix \
  --job-directory /path/to/evidence/jobs/completed-job \
  --evidence-root /path/to/evidence \
  --parser integrations.harnessix_eval.harbor_parser:HarnessixResultParser \
  --source-timezone UTC \
  --output /path/to/trials-harnessix.json

uv run --frozen python -m evalkit report \
  --plan /path/to/plan.json \
  --trials /path/to/trials-harnessix.json \
  --evidence-root /path/to/evidence \
  --grader harbor-reward=evalkit.harbor:HarborRewardGrader \
  --output /path/to/run-report.json
```

原始Harbor文件保留不变，输出必须位于源Job目录外；试次以证据库相对路径和摘要登记冻结的result.json，
判分时内容改变即拒绝。导入不判分：奖励由计划声明的harbor-reward判分器读取，不补造14项原生检查。
两个判分器都在计划内时，`python -m evalkit consistency`逐个列出判定不同或只有一方有判定的试次。
只接收Harbor 0.25.0、Job Lock v3和已结束的运行，标准reward==1判定通过。
Harbor级配置及实际自动重试次数必须明确为0；有重试的Job需要另行展开尝试，不能只导入最后一次结果。
原生Provider内部重试仍按产品配置执行，公共Turn用量不能证明其完整账单。

导入按计划槽位重算分母：Job须恰好覆盖计划的全部条目和重复。缺失结果保留为missing（invalid）；
额外结果、重复Harbor身份、重复原生Turn、错配Job/任务/执行配置均拒绝发布。
基础设施异常为infra_error，超时为timeout，审批中止、Provider和预算失败为system_error，
Harnessix自报未完成却没有智能体异常时判为协议证据不完整（infra_error）。
只选择结构化字段，异常正文、Prompt、响应正文和仓库URL不进入报告或投影。
槽位只关联计划；executor_refs中的harbor_trial_id与system_refs中的thread_id、turn_id才是执行身份。
相同输入锁的重复槽位按结果目录名稳定匹配，不把重复序号解释为随机种子或原始调度顺序。

Harbor的Job时间可能不带时区；此时--source-timezone必须由运行配置明确给出。
推荐在启动Harbor时设置TZ=UTC并按UTC导入；夏令时歧义拒绝猜测。
观测Token记为partial，reported_cost_usd只作执行引用，成本完整性保持unknown，不补零、不重新估价。
缺失试次的投影使用标明time_source的报告时间窗口，不计入单次耗时评分。

## 5. 真实Langfuse服务

[Compose](../../integrations/langfuse/compose.yaml)基于官方v4.76.0部署，
[镜像锁](../../integrations/langfuse/images.lock.json)固定六个多架构索引摘要，
并按平台记录清单摘要和镜像ID。只有Web绑定127.0.0.1:3300，数据库和对象存储不发布宿主端口；
各服务使用unless-stopped，Docker重启后自动恢复，数据卷保留。

```bash
uv run --frozen python integrations/langfuse/local_stack.py up \
  --state-directory "$HOME/.local/state/harnessix/langfuse-acceptance"
```

首次启动生成私有凭据文件credentials.env，目录0700、文件0600；不写入版本库。
登录账号为acceptance@harnessix.invalid，密码在该文件的INIT_USER_PASSWORD字段。
API使用同一文件中的LANGFUSE_PUBLIC_KEY与LANGFUSE_SECRET_KEY，由操作者只注入执行进程。
重启复用原状态；已有数据卷而凭据文件丢失时失败关闭，不生成替代密钥。
`local_stack.py stop`配合同一--state-directory停止服务并保留数据卷。

无法访问镜像仓库的评测主机不修改Docker守护进程配置，改为离线传输：

```bash
# 在可访问镜像仓库的机器上：按目标平台清单摘要拉取、核对镜像ID并打包（0600）
python3 integrations/langfuse/local_stack.py export-images \
  --platform linux/amd64 --output /path/to/langfuse-images-amd64.tar

# 在评测主机上：加载后只用本地镜像启动，逐个核对镜像ID，禁止拉取
docker load -i /path/to/langfuse-images-amd64.tar
python3 integrations/langfuse/local_stack.py up --image-source local \
  --state-directory /data/harnessix/langfuse/state
```

本机通过SSH隧道访问评测主机上的服务，地址仍为http://127.0.0.1:3300：

```bash
ssh -N -L 127.0.0.1:3300:127.0.0.1:3300 root@evaluation-host
```

## 6. 可重复的完整容器验收

[file-artifact](../../integrations/harbor/tasks/file-artifact/task.toml)使用独立Verifier，
Oracle应得到1，Nop应得到0。先使用Harbor TaskConfig校验任务，再以不同Job名称运行；
任务内容变化后不覆盖旧Job锁或旧证据。

这个夹具只证明环境、产物转移与评分正负路径。

完整产品矩阵使用[镜像准备](../../integrations/harbor/fixtures/prepare_image.py)和
[验收驱动](../../integrations/harbor/run_acceptance.py)：

```bash
uv run --frozen python integrations/harbor/fixtures/prepare_image.py \
  --build-directory /path/to/new-build-directory

# 在本进程注入本机Langfuse凭据并设置上文PYTHONPATH后执行；每轮使用新输出目录。
uv run --frozen python integrations/harbor/run_acceptance.py \
  --image-receipt /path/to/new-build-directory/image.json \
  --output-directory /path/to/new-acceptance-directory \
  --langfuse-base-url http://127.0.0.1:3300 --allow-network
```

依赖在构建阶段按产品uv.lock准备；本夹具通过任务自定义Compose显式固定Docker network_mode:none。
Harbor默认no-network通过sidecar控制TCP出口，存在其实现定义的DNS/ICMP例外；
本夹具的物理none命名空间比该默认策略更严格，监控实际内核模式并保留验收失败记录。
镜像内本地测试CA仅供确定性HTTPS SSE夹具使用，
产品仍强制HTTPS并校验证书，不修改宿主信任库。运行真实wheel、原生配置、App Server、SDK和read_file。
独立Verifier通过完成回答的摘要与成功工具证据验收只读review任务，并包含错误答案、审批中止、
超时和503失败变体。固定100/20 Token的SSE数据只用于字段流转验收，不代表实际分词或计费。

驱动先冻结运行计划，再核对五个场景、完整1/5任务分母、容器实际镜像和none网络模式、容器清理，
随后导入、判分、真实发送并读回，再以同身份补发并核对源文件没有变化。
5个场景验收通过与任务通过率1/5是不同口径；公开基准、真实模型质量、真实编码修改和测试执行
仍需独立冻结任务、权限、模型及预算验收。本矩阵未调用付费模型。

## 7. 官方接口依据

- [Langfuse OTel](https://langfuse.com/integrations/native/opentelemetry)
- [Langfuse实验身份](https://langfuse.com/integrations/native/opentelemetry/experiments)
- [Langfuse评分更新](https://langfuse.com/faq/all/tracing-data-updates)
- [Langfuse API迁移](https://langfuse.com/faq/all/deprecated-api-migration)
- [Harbor自定义Agent](https://docs.harborframework.com/agents/custom-agents)
- [Harbor独立验收](https://docs.harborframework.com/tasks/separate-verifier)
