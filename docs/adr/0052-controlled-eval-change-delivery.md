# ADR 0052：Coding Eval受控变更包与显式合入

- 状态：已接受并实现
- 日期：2026-09-07
- 决策范围：0.5.5d

## 背景

0.5.5c已经证明真实模型可以在固定历史缺陷上通过Agent Runtime、审批、外部Worker、测试和Git反馈完成严格任务，但通过结果仍停留在私有受管副本。直接复制文件无法证明结果来自通过的报告，也无法阻止目标来源漂移或覆盖用户脏工作区；自动`git apply --3way`又会把未评分的三方合并结果冒充原Eval结论。

## 决策

### D1：交付只接受严格通过的单文件Eval

新增`harnessix.coding-eval-change-package/v1`。生成器必须重算运行、报告、Git和前后镜像事实；当前只接受一个允许的已有UTF-8普通文件及0644/0755权限。包保存完整镜像但使用0600私有文件，不进入脱敏报告。

### D2：目标必须等于任务固定来源

无副作用预检同时绑定origin、source commit、tree OID、规范`ls-tree`摘要、Workspace scope、路径、权限和前镜像。目标的staged、unstaged或untracked任一非空均返回`eval_delivery_target_dirty`。不做三方匹配或自动清理。

### D3：批准精确绑定计划

新增`harnessix.coding-eval-delivery-plan/v1`。计划不保存绝对路径，完整字段形成批准指纹。只有匹配该指纹的`ApprovalRecord(APPROVED)`可进入执行；拒绝、错指纹和不同重复决定分别持久终止或冲突，不触碰目标。

### D4：写工作树，不写index或commit

执行前再次核对来源和干净状态。后镜像在目标同目录写入唯一临时普通文件，完成权限设置与文件`fsync`后，先持久化临时inode和`applying`意图，再最终核对前镜像并原子替换。替换后完成文件/目录`fsync`、后镜像摘要和inode归因，最后记录`applied`。

### D5：恢复只观察，不猜测重放

新增`harnessix.coding-eval-delivery-record/v1`和完整状态转换历史。`applying`重开时：

- 目标仍是前镜像：安全清理本次临时inode，回到`approved`，允许同一批准显式重试；
- 目标是后镜像且inode等于持久意图：记录`applied`；
- 目标是第三镜像：记录`conflicted`；
- 目标虽等于后镜像但inode不同，或仓库无法核对：记录`unknown`。

`conflicted/unknown`不自动覆盖、回滚或重放。

### D6：私有持久化和单交付互斥

状态根和交付目录要求当前用户0700，锁、包和状态要求0600与`O_NOFOLLOW`。状态使用同目录临时文件、文件`fsync`、`os.replace`和目录`fsync`原子发布。`flock`确保同一交付同时只有一个Harnessix进程处理。

## 失败语义

| 类别 | 稳定代码示例 | 是否写目标 |
|---|---|---|
| 运行/报告不合格 | `eval_change_package_not_passed`、`eval_change_package_workspace_drift` | 否 |
| 来源不符 | `eval_delivery_origin_mismatch`、`eval_delivery_source_drift` | 否 |
| 工作区不干净 | `eval_delivery_target_dirty` | 否 |
| 路径/前镜像变化 | `eval_delivery_target_changed`、`eval_delivery_file_invalid` | 否 |
| 批准错误 | `eval_delivery_approval_mismatch`、`eval_delivery_approval_conflict` | 否 |
| 效果无法证明 | `eval_delivery_postimage_unattributed`、`eval_delivery_reconciliation_unavailable` | 不继续写，转`unknown` |
| 发现第三镜像 | `eval_delivery_target_diverged` | 不覆盖，转`conflicted` |

## 测试门禁

- 私有包严格读写、摘要/正文篡改和权限校验；
- origin、HEAD、tree、前镜像、权限、符号链接以及staged/unstaged/untracked拒绝；
- 错批准、拒绝、重复决定、单进程锁和重复执行；
- 意图后、替换前、替换后和目录落盘后的退出恢复；
- 外部第三镜像及非本次inode后镜像不得归因；
- 真实历史通过run生成包，精确历史checkout显式合入后，Diff摘要、后镜像及两项隐藏检查与原报告一致；
- Ruff、Mypy、完整测试、异步调试严格模式、Schema、构建、仓库外wheel和CI全部通过。

## 后果与后续

0.5.5至此形成“真实模型候选→严格评分→私有变更包→目标预检→显式批准→可恢复写入”的纵向闭环。当前不支持多文件原子提交、创建/删除、rename、二进制、三方合并、自动commit/push和跨主机仓库锁；这些能力必须在后续版本分别扩展契约，不能放宽v1读取器。
