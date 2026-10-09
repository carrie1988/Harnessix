# harnessix-sqlite-identity

`1.0.0rc1` 是“Python 主体 + 最小原生扩展”的**开发内部候选**，不是默认 Writer，
也不代表 R4 或首发 macOS 安全验收通过。它独立打包，不改变主项目的 hatchling、
`uv.lock` 或依赖，不要求安装未发布的 companion 依赖。

## 构建边界

- 仅 CPython >= 3.12；扩展名为 `harnessix_sqlite_identity._bridge`。
- 使用标准 `setuptools.Extension`，不自定义编译器；非 abi3，必须按 Python ABI、
  操作系统和架构分别构建。构建机需要 C 编译器及对应 CPython 开发头文件。
- 当前仅 POSIX 候选；Windows 或非 CPython 构建直接报错，不退化为纯 Python 成功包。
- 仅编译 `src/harnessix_sqlite_identity/bridge.c`，依赖同目录
  `file_identity_backend.h`；使用 `-std=c11 -Wall -Wextra`，Linux 为 `dladdr` 链接
  `libdl`，其他 POSIX 不额外链接库。
- wheel 包含 `.pyi`、`py.typed`，不包含桥接 C/H；sdist 保留 C/H、类型文件和 SQLite 头文件。
- SQLite 仅使用随源码包附带的两份官方头文件，不编译 `sqlite3.c`，不添加
  `-lsqlite3`、SQLite 静态库或第二套 SQLite 运行时；构建时不下载头文件。

## 独立构建

在本目录按明确文件清单准备外部临时副本，再构建；不复制无关文件，
不在源码目录生成制品，也不运行主项目的 `uv sync`：

```bash
work=$(mktemp -d)
mkdir -p "$work/source/src/harnessix_sqlite_identity" "$work/source/third_party/sqlite"
cp pyproject.toml setup.py MANIFEST.in README.md LICENSE "$work/source/"
cp src/harnessix_sqlite_identity/{__init__.py,bridge.c,file_identity_backend.h,_bridge.pyi,py.typed} \
  "$work/source/src/harnessix_sqlite_identity/"
cp third_party/sqlite/{sqlite3.h,sqlite3ext.h,SOURCE.json,README.md} \
  "$work/source/third_party/sqlite/"
python3.12 -m venv "$work/venv"
"$work/venv/bin/python" -m pip install --no-cache-dir 'setuptools>=77.0.3' build
"$work/venv/bin/python" -m build --no-isolation --outdir "$work/dist" "$work/source"
```

`python -m build` 默认先生成 sdist，再从 sdist 构建 wheel；缺少源码、头文件或编译失败
必须阻断 wheel 交付。

## 验证范围与未解除门槛

运行期资格固定为 SQLite **3.45.3**、默认 **unix VFS**，并要求 source ID 精确匹配：

```text
2024-04-15 13:34:05 8653b758870e6ef0c98d46b3ace27849054af85da891eb121e9aaa537f1e8355
```

CPython >= 3.12 只是构建条件，不等于任意 SQLite 后端通过资格检查。
其他 SQLite 版本、source ID 或 VFS 不因打包成功获得放行。

打包检查应覆盖 sdist 源码/头文件/许可证完整性、wheel 的 CPython 平台标签、
干净环境导入，以及二进制不存在第二 SQLite 链接。构建环境的 `CFLAGS`、`LDFLAGS`
等仍可能改变编译/链接结果，不能仅凭未声明 SQLite 链接库声称已验证最终二进制。

打包成功不等于文件身份、替换竞态、失败闭锁或 Writer 集成通过。
当前首发只交付macOS，按实际OS/架构核验原安全门槛。Linux/Windows交付任务取消，
未来若另行立项须独立验收；Windows构建阻断、跳过或模拟检查不算该平台PASS。

### macOS 内存检查入口

先独立验证检测器的真实正／负控，再用**安装了对应插桩 wheel**的解释器执行
`tests/run_sanitized.py`。父解释器不能预加载检测器；入口会检查实际映射，仅向原场景
子进程设置预加载。直接预加载 pytest 父进程会被 Darwin 运行库清除继承变量，不能据
子进程退出 0 推断检测有效。原测试的断言、30 秒子进程期限及失败退出码保持不变。

以下变量均为已核验的绝对路径；`new_output` 必须尚不存在：

```bash
"$installed_python" -I -B "$repo/native/sqlite_identity/tests/run_sanitized.py" \
  --sanitizer asan --runtime "$llvm/lib/clang/22/lib/darwin/libclang_rt.asan_osx_dynamic.dylib" \
  --symbolizer "$llvm/bin/llvm-symbolizer" --output "$new_output"
```

分别以 `asan`、`ubsan`、`lsan` 及匹配的 wheel／runtime 执行；不得混用三套 OPTIONS。
入口保存源码／工具哈希、Junit 和原场景日志，但不自行宣布内存或发布验收通过。
原 Python 3.12.7 宿主仍有泄漏诊断；隔离 Python 3.14.0 候选及私有驱动修复后，
四组原场景各 93 通过、1 个历史 ABA 已知反例。候选并未替换产品运行时，也不是可分发安装包；详见
[原生来源研究 §7.6](../../docs/research/git-sqlite-native-source.md#76-macos-有效检测与子进程预加载)。

## 来源与许可证

固定 SQLite 3.45.3 官方头文件；来源 URL、压缩包及逐文件 SHA-256、官方 SHA3 核对
记录见 `third_party/sqlite/SOURCE.json`。头文件保留官方原文，详情见同目录 README。
本包 `LICENSE` 原样复制仓库根许可证；SQLite 自身的 public-domain 声明保留在头文件中。

构建参数依据：[Extension 官方文档](https://setuptools.pypa.io/en/latest/userguide/ext_modules.html)、
[pyproject 配置](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html)、
[sdist 文件清单](https://setuptools.pypa.io/en/latest/userguide/miscellaneous.html)。
