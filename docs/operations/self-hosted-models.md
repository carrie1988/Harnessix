---
doc_type: deployment-design
status: current
version: 1
code_revision: 6dca857c7d1e9c4352e37a5043f225007289692b
owners:
  - core
modules:
  - deployment
  - product_config
  - models
related_adrs:
  - docs/adr/0066-sandbox-network-and-secret-boundaries.md
related_tests:
  - tests/models/test_openai_chat.py
  - tests/product_config/test_wizard.py
  - tests/product_config/test_endpoint_probe.py
  - tests/product_ui/test_cli.py
supersedes: []
---

# 接入自托管模型

## 1. 适用范围

本页说明如何让Harnessix Code使用你自己部署的模型，包括同一台机器上的端点和局域网里的GPU服务器。
端点需要兼容OpenAI Chat Completions，支持工具调用，并能在流式响应中返回用量（`stream_options.include_usage`）。
已实测的服务端是vLLM；Ollama和LM Studio的兼容性尚未验证，见第5节。

## 2. 部署步骤

### 2.1 同一台机器

地址是`127.0.0.1`、`localhost`或`::1`时无需额外声明，向导自动应用本地预设：

```bash
export MODEL_API_KEY=$(openssl rand -hex 16)
harnessix code configure \
  --provider-kind openai_chat \
  --base-url http://127.0.0.1:8000/v1 \
  --model your-model \
  --non-interactive
```

### 2.2 局域网里的服务器

加上`--self-hosted`，声明这是你自己部署的端点。交互式运行`harnessix code configure`时，向导遇到内网地址也会询问。

```bash
export MODEL_API_KEY=$(openssl rand -hex 16)
harnessix code configure \
  --provider-kind openai_chat \
  --base-url http://192.168.1.20:8000/v1 \
  --model your-model \
  --self-hosted \
  --non-interactive
harnessix code doctor /path/to/workspace --no-tui --probe-model
```

`doctor --probe-model`会实际探测模型是否存在，以及工具调用参数能否解析。

`MODEL_API_KEY`即使服务端不校验也要设置，且至少16字节。`local`这类短值会被当作机密，一旦出现在普通提示词里，整个任务就会被拦截，所以启动预检会直接报错。上面的命令生成一个随机占位值。

可声明为自托管的地址只有：私网IPv4（`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`）、Tailscale等使用的`100.64.0.0/10`、
IPv6 ULA（`fc00::/7`）、单段主机名（如`gpu01`），以及`.local`、`.lan`、`.internal`、`.intranet`、`.home.arpa`后缀的主机名。公网地址会被拒绝。

## 3. 本地预设的效果

| 项目 | 取值 | 作用 |
|---|---|---|
| 明文HTTP | 允许 | 内网端点通常没有TLS |
| 单请求和首包时限 | 1800秒和300秒 | 排队和prefill期间不被判为传输失败 |
| 工具参数修复 | 开启 | 还原被字符串化的参数，修复常见转义错误 |
| 稳定提示前缀 | 开启 | 让服务端前缀缓存生效 |
| 费用 | 本地0元 | 状态栏显示“本地 0 元” |
| 累计Token上限 | 600万 | 缓存命中也计入累计，上限比云模型高 |

建议用`--context-window-tokens`填写模型的上下文窗口，压缩阈值会据此计算；不填时沿用产品固定上限。

思考开关经`chat_template_kwargs.enable_thinking`发送。服务端不支持时，用`--thinking-protocol none`关闭。

## 4. 回退与兼容

不声明`--self-hosted`时行为不变：内网HTTP端点被拒绝，内网HTTPS端点按普通云端点处理，不会标记为免费。
既有配置的摘要不受影响，因为默认值不写入配置文件。不想声明时，也可以用SSH隧道把远端端口映射到本机回环地址。

## 5. 风险与已知限制

- 明文HTTP下，代码和`MODEL_API_KEY`在内网中不加密传输，只应在可信网络上使用。
- 代码会发送到你配置的端点，Harnessix不验证该地址背后是谁部署的模型。
- Ollama和LM Studio尚未实测。若工具调用失败，先用`doctor --probe-model`确认，再尝试`--thinking-protocol none`。
