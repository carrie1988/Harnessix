---
doc_type: validation-evidence
status: current
version: 1
code_revision: 716a72bccc63b110851650d266e073252ca58d1f
owners: [core]
modules: [product_config, session]
related_adrs:
  - docs/adr/0104-managed-session-key-and-default-root.md
related_tests:
  - tests/product_config/test_session_key_acl_binding.py
  - tests/product_config/test_session_key.py
  - tests/product_config/test_product_state_owner.py
  - tests/product_config/test_managed_session_root.py
supersedes: []
---

# Darwin静态ACL绑定复用验证

## 1. 结论与实际范围

四件相关测试文件84项通过，0失败、错误、跳过；含实际macOS对象新增扩展ACL后拒绝、Key重开与Owner生命周期。
新ABI专项9项原实现8通过、1失败；后继静态复用成功，三次调用仍查询三个实际FD而不是缓存检查结果。
新增ACL、未知errno、不同FD、首次绑定失败和非Darwin边界均覆盖。
独立静态审查未发现生产回退，指出errno清零测试覆盖缺口；新增两个静默查询反例以进程内撤去清零产生2项真实失败，
原生产清零不改，随后完整84项复验。旧82阶段保留，不声称第二次独立审查，详见[Review Packet](review-packet.json)。
原失败、源字节和原件摘要见[结构化事实](facts.json)、[源锁](source-lock.json)及[验证记录](verification.json)。

只复用系统库及两个函数的静态ABI，不缓存ACL、FD、Key、路径、Owner、Scope或权限结果。
每一个原checkpoint、stat、ACL重新查询和原期限均保留。
接口、架构、时序、伪代码、安全和失败详见
[托管Key设计4.1.2](../../changes/m09-4a-managed-session-key-and-root.md#412-darwin静态ffi绑定与持续acl复核)。

## 2. 真实Git流程与热点

完整Git未合入候选原14文件228项回归227通过、1失败：实际材料登记与同claim重入触发原60秒维护期限。
独立cProfile记录约5.10亿函数调用，`_private_acl`实际3563323次反复构造ctypes系统绑定。
该仪器化运行失败及热点保留；它不是完整生产性能基线，也不认定为全部瓶颈的唯一原因。

同一Full候选只借用本单文件优化，原失败案例重新实际执行通过，耗时93.965秒；原运行122.130秒失败。
这个时间是包含准备、Review、批准及两次材料调用的整体测试耗时，不是某单次维护调用的60秒deadline。
没有增大deadline或减少Scope/Owner复核；不从单例通过推导全部228项或完整Git效果/备份恢复通过。

独立真实Darwin FD查询5000次：原绑定加载5000次、后继1次，实际ACL查询次数仍5000。
该局部循环仅测静态绑定成本，不承诺产品整体加速比或生产SLA。

## 3. 可复现与部署

在锁定开发依赖的项目根目录执行：

```bash
python -B -m pytest -q -p no:cacheprovider \
  tests/product_config/test_session_key_acl_binding.py \
  tests/product_config/test_session_key.py \
  tests/product_config/test_product_state_owner.py \
  tests/product_config/test_managed_session_root.py
```

原真实macOS ACL反例在其他平台可能跳过，不能把mock ABI或Linux执行称macOS原生验证。
无需配置、数据库迁移、Key轮换或服务部署；首次成功绑定后只复用进程内静态函数，重启重新绑定。
原异常、ACL指针finally释放及线程errno规则不变；实际权限每次查询，后一次改变仍固定拒绝。
完整Git准备/批准owner关联与后继效果、Windows、R3、Beta和商用门禁继续分别验收。
