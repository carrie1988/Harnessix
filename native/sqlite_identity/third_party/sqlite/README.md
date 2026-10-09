# SQLite 3.45.3 官方头文件

从[官方 amalgamation 压缩包](https://www.sqlite.org/2024/sqlite-amalgamation-3450300.zip)
仅提取 `sqlite3.h`、`sqlite3ext.h`，未修改任何字节。原冻结材料不可用，故从官方源重新获取。
版本、source ID、压缩包 SHA-256 和逐头文件 SHA-256 固定在 `SOURCE.json`。

同时对压缩包内 `sqlite3.c` 的 SHA3-256 与
[官方 3.45.3 发布记录](https://www.sqlite.org/releaselog/3_45_3.html)核对一致；
该 C 文件仅用于来源核对，不提取到发行目录，也不编译或链接。
构建仅使用本目录头文件，不在构建期间联网获取 SQLite。

保留头文件原始声明；SQLite 为 [public domain](https://www.sqlite.org/copyright.html)。
本包代码许可证见 companion 根目录 `LICENSE`，与仓库根许可证一致。
