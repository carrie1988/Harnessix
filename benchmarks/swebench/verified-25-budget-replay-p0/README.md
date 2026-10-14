# P0：累计预算离线重放

只读、零模型请求。对 v40（6 条）与 v27（25 条）认证终态轨迹逐步重建出站 history 消息，
用 Qwen3 分词器逐条计数，与 Provider 实际 usage 对账，再按同一轨迹反事实模拟不同计费方式
下每个 Case 可容纳的请求数。不是新 Agent 轨迹、实测收益或官方成绩；开发集 v27 3/25 不变。

## 文件

| 文件 | 内容 |
|---|---|
| `extract_wire_tokens.py` | 认证读取原 Session，校验视图摘要与 Native/Case SHA 不变，只输出数字 |
| `v40-message-tokens.json` / `v27-message-tokens.json` | 每步 history Token/字节、每条消息角色/Token/字节/工具名、实际 usage；不含正文 |
| `replay_budget.py` | 纯离线标定与反事实模拟 |
| `replay-summary.json` | 汇总结果 |

## 结论

- 31 条轨迹视图摘要全部与原记录一致，Native/Case 文件 SHA 未变。
- 分词器计数对 131 个点高估中位 285 Token，但有 1 点低估 6 Token：**分词器不能作为可证明上界**，
  只能作可选加速档；准入默认改用“上次实际 usage + 变更/新增消息字节”的锚定上界（P1）。
- 每请求固定开销（指令＋工具 Schema＋上下文片段）中位 3495 Token，6 次请求即占约 42% 累计预算。
- 基线模拟在 26 条仅追加轨迹中 23 条复现实际请求数。
- 可容纳请求数中位（仅追加轨迹）：基线 6；只遮蔽 2 步前工具结果 7；只把固定开销降到 1500 为 8；
  两者结合 10；只保留上一步工具结果原文 11。单独遮蔽或单独瘦身都不够，需 P2＋P3 合并。
- 假设轨迹不变；遮蔽后模型可能重读，真实收益必须由新冻结轮次验证。

## 复现

```bash
# 认证读取（需本机钥匙串，仅内存使用）
PYTHONPATH=src .venv/bin/python -B benchmarks/swebench/verified-25-budget-replay-p0/extract_wire_tokens.py \
  "<run root>" baseline,definition 3 out.json <tokenizer.json> <tokenizers site-packages>
# 离线模拟
cd benchmarks/swebench/verified-25-budget-replay-p0
python3 -I replay_budget.py v40-message-tokens.json v27-message-tokens.json replay-summary.json
```

分词器：`Qwen/Qwen3-8B` `tokenizer.json`，SHA-256 `aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4`，
未入库、未加入项目依赖。
