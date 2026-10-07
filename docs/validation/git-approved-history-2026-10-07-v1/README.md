---
doc_type: validation-evidence
status: current
version: 1
code_revision: e088b09b20de3b2898bd2d4b8479f39b84553018
owners: [core]
modules: [product_config, session, trusted_actions, execution, deployment]
related_adrs:
  - docs/adr/0068-transactional-workspace-and-git-delivery.md
related_tests:
  - tests/product_config/test_git_approval_history_projection.py
  - tests/product_config/test_git_prepared_approval_history.py
  - tests/product_config/test_git_prepared_link_ledger.py
  - tests/product_config/test_git_review_fresh_owner.py
supersedes: []
---

# 原 prepared Git 审批历史与限定先导安装验证

## 1. 结论与范围

**`READONLY_HISTORY_VERIFIED_PILOT_INSTALL_READY_NOT_RELEASE`。**
[完整详设](../../changes/m09-r4-git-prepared-approval-history.md)的私有历史 Reader 在原 Git 全 MAC、完整 Session、Route、Execution、Core/CAS/Review 及终端读集合下完成限定核验。
旧 pending-only Reader 未放宽；已决定但 Git 仍 prepared 时输出 `decision_not_linked`。
没有追加决定事件、execute/reconcile、默认 Git 写工具、DDL、Schema、Key 或授权算法变更。
本结果不关闭整个 B2、B4、R1～R6、真实编码质量或商用发布。

## 2. 源码与制品身份

研究父提交 `e088b09b20de3b2898bd2d4b8479f39b84553018` 不是新增实现提交证明；[全输入摘要](source-inputs.json)冻结 5089 件交付输入，最终提交由独立提交证明关联。
Wheel `a9f3ff16a606b07d4aadd2a817862a7bfd7ff89ab49c1143471206e3c27a16d9`；548 件包成员、507 件 Python 源码逐字节一致于独立克隆及两个独立安装。
运行安装为 macOS 27.0.1 / arm64 / CPython 3.12.7 / `1.0.0rc1`，仅 `openai+tui` 的46依赖，不含dev；安装验证环境另含dev与原双Provider夹具所需anthropic Extra。
安装以原锁 `--offline --require-hashes --no-deps` 完成，`python -I` 及包成员核验拒绝源码 fallback。

## 3. 最终验证

| 验证组 | 真实结果 | 秒数 | 证据范围 |
|---|---:|---:|---|
| 源级治理、Session、原审批恢复及纯解释 | 2020 PASS | 53.142 | 原JUnit与完整日志分别留存 |
| 源级五项历史/控制/原恢复负控 | 5 PASS | 474.171 | 原JUnit与完整日志分别留存 |
| 安装级原SDK四状态与旧pending边界 | 4 PASS | 459.788 | 原JUnit与完整日志分别留存 |
| 安装级纯解释与原Owner回归 | 148 PASS | 60.522 | 原JUnit与完整日志分别留存 |
| 最终文档、可读性及生成合同回归 | 29 PASS | 8.011 | 原JUnit与完整日志分别留存 |

不同组包含重叠测试，不能相加声称唯一覆盖数。纯模型投影测试不认证 MAC。
SDK阳性通过正式 start_turn / approval/respond / turn/cancel 和原持久化取得；夹具只禁止执行前后台启动，不替换决定与来源认证。
这些场景临时注册Git工具，不证明默认产品广告或真实Provider质量。原只读命令授权夹具不等于用户Git业务批准。
原MAC未通过前禁止解释；Router-first不一致拒绝，再由原sync_decision协调器补相同Session决定，Reader自身不恢复。
末端全行/尾锚、Source/CAS/Review和已验真审批物理行复核不进入共享Execution构造检查点；四类原控制异常实例保持。

## 4. 失败原件与处置

- 缺新模块的源级RED保留，退出2；纯解释扩充首次4 FAIL保留，最终136 PASS。
- 第一治理组25 FAIL/1995 PASS保留：旧Apple Git 2.24.3不支持SHA256，且Windows仿真夹具拒绝含空格输出路径；改用既有Git 2.53.0和独立无空格basetemp，不改产品/测试/门槛，第二完整组通过。
- 初次安装四状态4 FAIL、Owner组2 FAIL/146 PASS保留：验证环境缺原双Provider夹具所需Anthropic Extra，原preflight以product_dependency_missing正式拒绝。按原锁补两依赖后在同Wheel重新通过，不跳过预检。
- 初次Configure拒绝非独占目录；仅修复新建验证目录权限，原件保留。无Secret时Doctor以secret_unavailable拒绝；使用无效测试凭据及.test端点后离线预检通过，没有网络请求或真实账户验证。
- 初始通用Wheel目录缺40原件，未下载或重打包；固定uv私有缓存的逐文件清单与20MB离线输入包支持当前Mac重放，不冒充通用pip Wheel库。
- 初次文档组1 FAIL/28 PASS保留：新增报告类型不在既有枚举且评审包缺元数据；修复文档后29 PASS，未修改政策。
- 早期源级4状态及6项扩展属于诊断，不代替最终冻结实现及安装组。

## 5. 单人先导交付

[操作手册](../../operations/pilot-beta.md)保持五项真实需求、人工验收、审批/取消/重开/状态恢复记录。
安装输入、独立venv、实际help/code-help、离线Configure/Doctor和成员证明已经准备；测试凭据/端点仅供离线验证，不能用于真实试用。
**真实任务0，独立Beta0。** 单人先导不替代3～5名独立开发者、至少15任务及三平台实际使用。
当前UI费用unknown，不是人民币硬预算；本切片真实API请求0，未决费用预留与暂停规则不变。

## 6. 未验证与风险

完整U最后Git复核、协作锁、决定Writer和恢复屏障仍planned；A/T2/D、NativeBridge、Commit、业务Backup2及默认Git装配未闭合。
本机安装不证明当前候选Linux/Windows消费者流程；R3最近严格0/20，P1同步调度长间隔及最终同候选门禁继续开放。
四库监视只检测可观察变化，不是跨库事务、永久Owner或恶意外部ABA防护；只读历史不是执行权限。

## 7. 原件及复核

[结构化结果](result.json)、[Review Packet](review-packet.md)、[输入全集](source-inputs.json)和[摘要清单](SHA256SUMS)为公开低敏索引。
专用本地验证目录留存完整XML/日志、五幅真实渲染及视觉复核、Wheel/两安装证明、输入缓存、失败原件、完整报告、Manifest和后续提交/推送证明。
未追查旧CI；新提交仅读取一次CI元数据，不将in_progress当成功。
