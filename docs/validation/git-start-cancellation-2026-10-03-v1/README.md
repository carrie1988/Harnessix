---
doc_type: validation-evidence
status: current
version: 1
code_revision: 5fbd98d04f27661edce9b9b71d7cacc03d72d62e
owners:
  - core
modules:
  - product_config
related_adrs:
  - docs/adr/0091-action-runtime-fencing-and-bounded-reconciliation.md
related_tests:
  - tests/product_config/test_git_shared_process_pre_handoff.py
  - tests/product_config/test_git_shared_process_startup.py
  - tests/product_config/test_git_shared_process_host.py
  - tests/product_config/test_git_shared_process_capacity.py
  - tests/product_config/test_git_shared_process_digest.py
  - tests/product_config/test_git_delivery_process.py
  - tests/product_config/test_git_material_input.py
  - tests/product_config/test_git_material_trace2_binding.py
supersedes: []
---

# Git启动登记前取消与原宿主交接限定验证

## 背景与范围

原Supervisor通过后台线程创建Owner和写入启动请求。取消等待该线程的协程不会停止线程，
因此仅在异常后查找已登记句柄，不能覆盖请求已送达、句柄尚未登记的窗口。
完整总体架构、模块边界、接口、字段、流程与时序、伪代码、持久化及失败恢复见
[共享宿主总体与详细设计](../../changes/m09-r4-git-shared-process-host.md#81-启动交接窗口的失败结算)。

本次只修复原命令端口的启动协调，不加入新的父意图、执行批准、数据库或进程监督实现。
完整Git候选的A执行、T、Bridge、D、Checkpoint、独立Commit及Backup v2均不由本资料验收。

## 验证方法

真实POSIX Supervisor、Owner和普通V2计划/批准夹具，不伪造Lease、MAC或目标进程。
分别在实际Owner创建后和原启动写入完成后阻止线程返回，再取消外层Task一次或三次。
取消期间调用不得提前结束；释放有限门闩后必须完成原交接、原停止终态、Popen回收及FD关闭。
已登记运行回执的取消为第五个对照。旧回执/停止故障、原因对象、peer隔离与同ID重放测试保持；
原六种SHA1/SHA256、blob/tree/commit完整8MiB写入与独立新批准读取不减容量。

## 实际结果

终态、选择器、原件摘要及限定输入身份由[结构化验证事实](verification.json)记录。
主仓八件测试文件实际174通过、0失败/错误/跳过，81.361秒；438件生产源码与8件测试
共446件完整输入前后一致。运行使用Python 3.12、Git 2.53.0与原生POSIX Owner。
新结果不与历史169项、开发工作区回归或其他重叠运行相加。
真实登记前RED保留：旧实现取消返回时Lease为starting，句柄未登记，Owner/目标与控制FD仍存活。
测试finally单独回收自己的资源，该事后回收不改变原失败结论。

## 失败与安全边界

启动和异常结算由本次托管Task持有，外层仅shield等待并复用原排空原语；
启动返回后仅停止本次句柄，不关闭产品级Supervisor或计划库。
停止正常返回unknown仍不是验真成功，保持git_process_unknown及原异常/结算异常原因组。
Owner请求仍携带原持久deadline，原审批、能力、环境、原始流及材料合同不变，排空不续期。

本机回归不证明Windows原生、消费者环境、完整Git业务或R3真实编码质量。
静态审查不替代实际运行；发行前仍须同一候选的必要验收。R1～R6继续开放。
本次没有模型请求、凭据读取、费用修改或Docker操作。
