---
doc_type: deployment-design
status: current
version: 1
code_revision: 1793360021099fa4dae5f9e64f48c1b4fbbf35f5
owners:
  - core
modules:
  - deployment
  - product_config
  - sandbox
related_adrs:
  - docs/adr/0066-sandbox-network-and-secret-boundaries.md
  - docs/adr/0081-single-coding-agent-product-boundary.md
related_tests:
  - tests/product_config/test_thinking_config.py
  - tests/sandbox/test_container_workspace_path.py
  - tests/test_exec_cli.py
supersedes: []
---

# 在海光K100上自托管Qwen3.8-Flash-Next并接入Harnessix

## 1. 适用范围

记录2026-11在一台8卡海光K100_AI（DCU，DTK 26.04）服务器上部署Qwen3.8-Flash-Next INT8（w8a8）、
让Harnessix在同机以无头方式使用它的做法、实测性能与已知问题。脚本在
[`deploy/local-model/`](../../deploy/local-model/)，其余Linux加速卡可参考接入方式，但补丁只适用于下述镜像。

## 2. 前置条件

- 镜像`vllm0.29.0-ubuntu22.04-dtk26.04-py3.10-…-qwen3.8flashnext`与权重`Qwen3.8-Flash-Next-Channel-INT8-w8a8`
  （约183GB，校验证据见服务器`/data/local-model-services-20261023/evidence`）；
- 该镜像需要的K100补丁（MoE对齐、路由、INT8线性层、PLE加载、跳过warmup）只读挂载进容器；
- 本仓库补充的采样补丁：[`patch-topk-topp-sampler.sh`](../../deploy/local-model/patch-topk-topp-sampler.sh)
  从镜像取出采样器并在ROCm/DCU上禁用Triton top-k/top-p内核；
- Docker、`/dev/kfd`、`/dev/dri`、`/dev/mkfd`设备与`video`/`render`组。

## 3. 部署

```bash
bash deploy/local-model/patch-topk-topp-sampler.sh      # 一次性生成采样补丁
bash deploy/local-model/run-flash-replicas.sh           # 两个副本：卡0-3 -> :18002，卡4-7 -> :18001
bash deploy/local-model/run-flash-replicas.sh b         # 只启动副本B
MTP_DRAFT_TOKENS=0 bash deploy/local-model/run-flash-replicas.sh b   # 关闭MTP对比
```

每个副本TP4、`max-model-len 262144`、前缀缓存、`qwen3_xml`工具解析、`qwen3`推理解析、MTP=1。
副本只监听`127.0.0.1`。启动约8分钟（权重加载约3分钟）；`curl http://127.0.0.1:18001/health`返回200即就绪。
诊断时可设`PROFILER_CONFIG`为vLLM `--profiler-config`的JSON，再调用`/start_profile`、`/stop_profile`；
诊断结束应去掉该变量重启（见第7节）。

## 4. 接入Harnessix

配置向导对回环地址端点（`127.0.0.1`/`localhost`/`::1`）自动应用本地预设：

| 字段 | 值 | 作用 |
|---|---|---|
| `thinking_protocol` | `vllm_chat_template` | 经`chat_template_kwargs.enable_thinking`开关思考 |
| `normalize_tool_arguments` | `true` | 还原Qwen把数组/对象参数编码成的JSON字符串 |
| `stable_prompt_prefix` | `true` | 逐请求说明放到末尾，前缀可被缓存，也满足“system只能在开头”的模板 |
| `pricing` | `free` | 状态栏显示“本地 0 元”，默认累计token上限600万 |
| Profile时限 | 首包300 s、单请求1800 s | 本地prefill慢且多会话共享副本 |

接入前可在线确认端点与工具调用：`harnessix code doctor <工作区> --config <配置> --no-tui --probe-model`（K100副本上实测：模型已列出，数组参数工具调用解析正确）。

无头运行：`harnessix exec --prompt-file task.md --config <配置> --action-config <执行配置>
--approval-mode auto --summary-file summary.json --events-file events.jsonl`。本地端点仍需`MODEL_API_KEY`
环境变量存在（值任意）。

## 5. 实测性能（单副本，TP4）

| 场景 | 结果 |
|---|---|
| 单路解码 | 约33 tok/s |
| 冷prefill | 约600 tok/s，与长度成正比（32K约52 s，128K约218 s） |
| Agent追加模式（每轮约+2K） | 第3轮起前缀缓存命中，首字约9 s |
| 并发 | 不提高总吞吐：prefill会阻塞同副本其他请求的解码 |
| SWE-bench单题（A2配置） | 3～20分钟，单步约10～15 s |

prefill剖析：73%的GPU时间在模型的QSA稀疏注意力（`_qsa_sparse_paged_gqa_splitk_kernel`约59%），MoE与INT8线性层
合计不足10%。该内核按单查询split-K组织，prefill时每个查询行单独gather，是吞吐上限的主因。

## 6. 运行约束

- 不占用其他团队已发布的端口（本机18000被占用）；只操作带`owner=harnessix`标签的容器；
- 卡温低于90°C；评测完成后删除本项目的SWE-bench评测镜像与容器；
- 评测时每个副本建议不超过2个并发Agent。

## 7. 已知问题与处置

| 现象 | 根因 | 处置 |
|---|---|---|
| 并发≥4时引擎`EngineDeadError`，执行超时300 s | Triton `_topk_topp_kernel`在logits行≥8（MTP翻倍）时在DCU上挂起 | 采样补丁 + `VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=1800` |
| 启动后warmup卡死 | 镜像warmup在本卡集合通信上自旋 | `VLLM_SKIP_WARMUP=1`等环境变量（见启动脚本） |
| 一个worker挂起约11分钟后退出 | 发生在开启profiler的副本上，根因未定 | 去掉`PROFILER_CONFIG`重启；评测执行器启动任务前做健康检查 |
| Qwen模板报“System message must be at the beginning” | 逐请求说明作为system插在开头 | `stable_prompt_prefix`（第4节） |
| 首字超过30 s被判传输失败 | 默认时限面向云端 | 本地预设时限（第4节） |

## 8. 源码与脚本映射

- 部署：[`start-flash.sh`](../../deploy/local-model/start-flash.sh)、[`run-flash-replicas.sh`](../../deploy/local-model/run-flash-replicas.sh)、
  [`patch-topk-topp-sampler.sh`](../../deploy/local-model/patch-topk-topp-sampler.sh)；
- 基准与探针：[`local_model_serving_bench.py`](../../scripts/local_model_serving_bench.py)、
  [`local_model_tool_call_probe.py`](../../scripts/local_model_tool_call_probe.py)；
- 本地预设：[`wizard.py`](../../src/harnessix/product_config/wizard.py)；
- 评测：[`swebench_local_run.py`](../../scripts/swebench_local_run.py)、[`swebench_local_report.py`](../../scripts/swebench_local_report.py)、
  [`security_acceptance.py`](../../scripts/security_acceptance.py)。
