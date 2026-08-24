# session-persistence-jsonl —— 一个目录,一行一个事件

协调器把一致性管住了;剩下的是「把字节放下」。JSONL 后端把每个
会话写成一个人工可读的 append-only 文件,并把「崩溃之后日志仍然
完好」这条底线钉死在每个写路径上。

## 物理布局

```
{root}/{projectKey(cwd)}/{encodeSegment(id)}/session.jsonl
```

- `projectKey` 把 cwd 折叠成目录名(`--` 包裹,长度截断);id 经
  `encodeSegment` 转义(`~` 十六进制、`.` 与 `..` 特判),路径穿越
  在词法层就被中和。
- 第一行是会话 header(元数据,含格式版本),之后每行一个事件。
  chunk 碎块事件按运行打包成行(裸标签 `text-chunks` 等,打包行
  通过整个 run 推进 seq 游标)。

## 原子写:双路径

「落盘即发布,读者只见到完整文件」:

- **POSIX**:临时文件写透 + fsync,`link()` 发布 + `unlink()` 清理
  (EEXIST 并发仲裁 —— rename 的静默覆盖不可用)。
- **win32**:ctypes 封装 `MoveFileExW(MOVEFILE_WRITE_THROUGH)`,
  临时对象写透重命名发布;目录同样按段 staging + 写透发布。
- 追加走 `stat 前 → 写 → fsync`,失败回滚 truncate 到写前字节数
  再重抛 —— 不变式游标会重试本批次,留下部分字节会制造重复 seq。

## 崩溃恢复:撕裂尾

读路径先按修订号(dev:ino:size:mtimeNs:ctimeNs)做稳定性循环,
把读取的字节快照钉在一份一致修订上;然后扫描事件行:不完整行
(无换行的撕裂尾)截断到最后一个完整行的字节边界,`tornMarker`
记录截断点与恢复数;open 回合由协调器在采纳/加载时合成关闭器
(`interrupted`)。已提交字节永不重写 —— 修复只截断尾部,不动
历史。

## 只读发现的纪律

`list` 是元数据级操作:逐项目目录读第一行 header(8KB 分块,长
header 不设上限),不做任何修复、不写一个字节。垃圾文件跳过,
cwd 与物理路径不符、重复 id、相反压缩档位、legacy 平铺布局都被
拒绝 —— 读不动的磁盘状态要浮出,而不是假装没看见。

## 与 DSH 的差异

zstd 压缩档位被砍掉(零新依赖决策),压缩开关保留,默认 `none`,
`zstd` 值被明确拒绝并给出说明;`tornMarker.recoveredEvents` 在
纯 JSONL 路径恒为空列表(它只存在于 DSH 的 zstd 解码回退)。win32
FFI 用标准库 ctypes 替代 koffi,非 Windows 进程永不加载 kernel32。

## 测试

```bash
cd packages && python -m pytest session/session-persistence-jsonl -q
```

含 26 个格式测试(路径编码/header 守卫/扫描器 gap 语义)、23 个
后端测试(惰性物化/逐字节 round-trip/部分写回滚/撕裂尾修复/8KB+
header/路径穿越/磁盘采纳)与 8 个 win32 测试(盘符根原生探测、
写透发布、EEXIST/ENOENT errno 映射、255 字符组件)。
