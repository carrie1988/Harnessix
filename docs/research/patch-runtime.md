# Patch：精确计划、写效果和恢复边界

- 日期：2026-09-03
- 状态：0.5.3 专项研究；0.5.3a 是只读准备，不是写能力
- 基线：Codex `a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67`、OpenCode `69c172e8a7c0086887b1f93ed5a162f14b6aa0c5`，与现有[冻结基线](baselines.md)一致

## 1. Codex：失败也可能已产生部分效果

**事实**：[apply-patch/lib.rs](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/apply-patch/src/lib.rs) 中 apply_patch_with_options 先 parse_patch，再通过 apply_hunks_with_options 进入 apply_hunks_to_files。后者按 hunk 处理文件，持续累积 AppliedPatchDelta；写失败分支将 delta.exact 置为 false，并随失败携带已有证据，不假设 write 返回错误就代表没有修改。

**事实**：[file_update.rs](https://github.com/openai/codex/blob/a0dcfe2ada3f5bbd5059a34c0fc6fac244741a67/codex-rs/apply-patch/src/file_update.rs) 和入口提供不同的换行更新模式。换行行为是明确选项，不是所有工具通用的隐式保证。

**推断**：应分离“计划不合法”“写尚未尝试”“已经写入但结果未知”“多文件部分完成”。仅捕获异常并返回 failed，无法安全指导恢复。上述源码不是 Harnessix 已有持久意图协议的证明。

## 2. OpenCode：V1/V2 的精确编辑和条件写边界

**事实**：V1 [edit.ts](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/opencode/src/tool/edit.ts) 会按路径锁读取文件、处理 BOM/行结束符、生成 Diff、请求权限，再写入并运行格式化。替换器包含精确与多种模糊修正策略；不能把 V1 的全部行为移植为本项目默认编辑语义。

**事实**：V2 [tool/edit.ts](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/tool/edit.ts) 明确以 exact-edit 为入口，非唯一匹配需要显式 replaceAll；模糊修正、格式化、快照等能力仍列为后续待办。它通过 FileMutation.writeIfUnchanged 写入。

**事实**：[file-mutation.ts](https://github.com/anomalyco/opencode/blob/69c172e8a7c0086887b1f93ed5a162f14b6aa0c5/packages/core/src/file-mutation.ts) 的 writeIfUnchanged 在目标锁内重新 readFile，对比 expected 字节，再调用 writeFile；不是内核提供的带内容前置条件写操作。

**推断**：同一协调域内的锁和复核有价值，但方法名不能证明对不协作编辑器存在跨进程 CAS。研究必须继续到实际文件操作，不能停在包装器命名。

## 3. Harnessix 的选择

1. **先计划，后效果**：0.5.3a 只读取完整前镜像，提供精确编辑与只读复核；0.5.3b 才增加持久意图、批准计划和效果核对。
2. **拒绝猜测**：v1 不做 fuzzy/replace_all。全部编辑定位于同一原文，锚点唯一、区间不重叠；不自动修改 BOM、换行或未涉及字节。
3. **完整证据**：read_file 的 revision 继续作为来源失效标志，但完整 SHA 在准备时读取全文件后重新计算；不能用可见片段计算“文件哈希”。
4. **不滥用幂等**：修改执行拟复用 NON_IDEMPOTENT_WRITE，不原地扩展冻结 Action v1。第三种内容发生时不能“再执行一次看看”。
5. **先限定工作副本**：真实写需要宿主明确管理的独占工作副本。原地 rename 原子性不能解决与外部编辑器的覆盖竞争；缺少该条件就保持只读准备，不自动对源目录降级。
6. **不复制实现**：这里只记录机制和失败语义；Python 实现独立使用现有 Workspace/严格模型/摘要约定，不将参考仓库引入运行依赖。

Python os.replace 成功时的原子替换与相对目录 FD 能力，不能被解释为预期内容校验；Linux RENAME_NOREPLACE 只检查目标不存在。**推断**：跨进程内容 CAS 需要额外的操作系统/协作前提，而非多做一次用户态哈希。[Python 文档](https://docs.python.org/3.12/library/os.html#os.replace)、[Linux rename 文档](https://man7.org/linux/man-pages/man2/rename.2.html)。

具体落地、非目标和门禁见 [ADR 0027](../adr/0027-prepared-patch-and-write-admission.md)。
