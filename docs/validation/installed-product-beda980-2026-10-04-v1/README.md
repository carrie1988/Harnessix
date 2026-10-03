---
doc_type: validation-evidence
status: current
version: 1
code_revision: beda980fbeee90a36487b04eac5f1b493539b91f
owners: [core]
modules: [product_config, documentation]
related_adrs:
  - docs/adr/0062-local-first-v1-commercial-boundary.md
  - docs/adr/0063-windows-v1-platform-support.md
  - docs/adr/0106-v1-release-scope-and-risk-based-gates.md
related_tests:
  - tests/governance/test_installed_product_acceptance.py
  - tests/governance/test_installed_product_upgrade_acceptance.py
supersedes: []
---

# 固定beda980候选三平台源码外安装、升级与恢复验证报告

## 1. 验证目标、范围与结论

固定`beda980fbeee90a36487b04eac5f1b493539b91f`的[Run37140188341](https://github.com/carrie1988/Harnessix/actions/runs/37140188341)、attempt1、workflow_dispatch已终态success。
唯一规范Wheel构建与扫描，以及Linux、macOS、Windows三个实际安装Job均success。每个平台完成源码外安装、完整状态备份恢复、卸载重装，及原0.1.0发行件到1.0.0rc1候选的不同版本升级和匹配备份回退。

这关闭本固定候选的上述安装生命周期专项，不关闭R4整体、消费者目标OS、完整默认Git／Backup v2、真实编码质量、独立Beta或1.0商用发布。全部六份原结果明确`provider_turn_requests=0`、`commercial_release=false`。

## 2. 执行环境与原生Job

| 平台 | 环境 | Python | Job | 结果 |
|---|---|---|---|---|
| Linux | x86_64 | 3.12.3 | [111252906353](https://github.com/carrie1988/Harnessix/actions/runs/37140188341/job/111252906353) | success |
| macOS | Darwin arm64 | 3.12.10 | [111252906352](https://github.com/carrie1988/Harnessix/actions/runs/37140188341/job/111252906352) | success |
| Windows | AMD64 | 3.12.10 | [111252906320](https://github.com/carrie1988/Harnessix/actions/runs/37140188341/job/111252906320) | success |
| 唯一构建 | Linux构建与实际制品扫描 | 工作流锁定3.12 | [111252831457](https://github.com/carrie1988/Harnessix/actions/runs/37140188341/job/111252831457) | success |

上述结果证明CI原生runner与明确架构，不将其写成全部消费者系统版本或企业策略验收。

## 3. 唯一发行物与来源身份

- 当前唯一Wheel：`harnessix-1.0.0rc1-py3-none-any.whl`，1211078字节。
- SHA-256：`5bf2b54c21afa115ce46b3d860d67243243a740db474841a4cfd42842b946e7d`；三个安装和三个升级结果均绑定该原字节身份。
- 原版本：`0.1.0`，来源`a4f7f33449bb897d84fe3a8e8262307943233fb4`；原Wheel摘要`5a6e144487dd79679f609b709743db176ee28bdca077084ac8901efdc9f2fe30`。
- 每个平台对安装包与候选源码实际核验479个包成员；此数包含非Python成员，不是479个源码文件。
- 七个GitHub Artifact容器摘要均实测匹配；仅提取六份`result.json`与规范Wheel，不读取或保存其余业务日志。

规范Wheel和七份Artifact元数据保留于私有验证包。公共结果文件保持原字节，校验见[SHA256SUMS](SHA256SUMS)。

## 4. 核心流程、接口与源码对应

流程复用[原工作流](../../../.github/workflows/installed-product-acceptance.yml)，没有新增安装器或诊断协议。
[`installed_product_acceptance.py`](../../../scripts/installed_product_acceptance.py)使用独立虚拟环境、Python -I及源码外目录，校验实际包成员、Doctor和真实持久状态，执行备份、恢复、卸载与同Wheel重装。
[`installed_product_upgrade_acceptance.py`](../../../scripts/installed_product_upgrade_acceptance.py)读取两份实际Wheel元数据和原字节摘要，以新隔离进程完成旧状态创建、升级读取与新建Thread、匹配备份恢复和版本回退。

## 5. 关键字段、数据与安全边界

| 字段 | 实际意义 |
|---|---|
| source_revision／wheel_sha256或candidate_wheel_sha256 | 固定源码和同一构建件，不使用各平台独立重建的近似Wheel |
| isolated／source_checkout_in_sys_path | Python隔离开启、源码目录不进入导入路径 |
| original_key_retained／prior_root_retained | 备份、恢复、卸载和升级回退后原Key及原认证根保持 |
| active_owner_backup_refused | 活跃Owner存在时拒绝备份，不截取不一致状态 |
| same_restore_id_does_not_rewind_new_state | 同恢复ID不再次回退后续状态 |
| upgrade_install_preserves_state_bytes／rollback_install_preserves_state_bytes | 单独替换版本不改写当前匹配状态；回退须配合原备份 |
| workspace_unchanged | 安装和维护不修改用户Workspace夹具 |
| provider_turn_requests／commercial_release | 模型请求为0，发布声明为false；不能充当R3质量或商用通过 |

## 6. 测试、失败与证明边界

工作流先运行原安装、升级验收器的正反例，再运行实际产品。隔离导入、来源漂移、同版本伪升级、错误阶段、错误备份及输入不完整均按原契约拒绝。
该专项全部Job success，但同候选常规[CI Run37140137789](https://github.com/carrie1988/Harnessix/actions/runs/37140137789)并未因此通过：macOS前置控制台回归失败，Windows后继Session认证回归失败，其他Job结果应按其权威终态核对。
原材料三个Windows分组均通过仍不替代Session回归，也不覆盖未执行的后继广泛范围。

## 7. 证据清单、审查与后继

- [installed-linux-result.json](installed-linux-result.json)：原结果，SHA-256 `70af7444e45bc5a66f190c44e184b3251a67958a2b97b43d3ec1f8885e3eb1d7`。
- [installed-macos-result.json](installed-macos-result.json)：原结果，SHA-256 `d5416428f8f8187dfaa27f27b4d37abe4ab454372e10ee13e034501f43157240`。
- [installed-windows-result.json](installed-windows-result.json)：原结果，SHA-256 `b6fc434da0f34b7f3f4063fcb76f43b71109e934bb672f3d5eba3dd3d9a2cb79`。
- [upgrade-linux-result.json](upgrade-linux-result.json)：原结果，SHA-256 `3ec4e5629c571e4f6cbad4594a0065cae62da5a694bb7d2a8a43f1a9da3adc60`。
- [upgrade-macos-result.json](upgrade-macos-result.json)：原结果，SHA-256 `93a2e19bc414df99ec1d7ea0d4f2f8a0bb7040ed5577808bee5d93b917ae8d67`。
- [upgrade-windows-result.json](upgrade-windows-result.json)：原结果，SHA-256 `e903fabd4208ee520f75a20c9497b032e6701889cad226221716193fecab4ccb`。

实际模型任务、消费者系统及独立Beta未执行，不使用离线Provider或安装成功替代。原失败保留；后继源码变化须重新核验影响边界，不将此固定版本的成功自动转移到其他候选。
