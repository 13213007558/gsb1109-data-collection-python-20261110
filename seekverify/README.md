# seekverify

离线校验 warcio 索引偏移能否把随机读取**精确定位**到目标 WARC 记录，重点防止
“偏移单位理解错误后读到相邻记录”。

校验同时使用两条相互独立的读取路径，只有二者与索引三者一致才判通过：

1. **原始字节 oracle**（`rawreader.py`）：只用 `zlib`/`hashlib` 解析 gzip 成员边界
   与 WARC 帧，不依赖 warcio，用于判断**文件本身**是否损坏。
2. **warcio 重定位**（`verifier.py`）：`fh.seek(offset)` 后用
   `ArchiveIterator` 读取，与生产后端的做法完全一致，用于判断 **warcio 实际
   落在哪条记录上**，并对块/载荷做字节级 sha256 交叉比对。

输入索引**只读、永不重写**，不会通过修改索引让结果“看起来通过”。

## 各格式的定位方式（偏移单位）

| 格式 | `offset` 指向 | `length` 覆盖范围 | 可否随机定位 |
|------|---------------|-------------------|--------------|
| 未压缩 WARC | `WARC/` 状态行首字节 | WARC 头块 + Content-Length 内容块，**不含**记录间的 CRLF 空行（相邻记录间隔通常 4 字节 `\r\n\r\n`） | 可以 |
| 多成员（chunked）gzip WARC | 单个 gzip 成员的魔数 `1f 8b` 处，**一条记录一个成员** | 整个 gzip 成员：10 字节头 + deflate 流 + 8 字节尾（CRC32/ISIZE）；记录结尾的 `\r\n\r\n` 位于成员**内部** | 可以 |
| 单成员（non-chunked）gzip WARC | 仅文件头一个边界，多条记录压在同一成员内 | —— | **不可以**，工具与 warcio 均拒绝（`non-chunked` 错误），需先 `warcio recompress` |
| ARC / ARC.gz | `filedesc://` 等不同帧结构 | —— | 本工具**不覆盖**，仅支持 WARC |

关键事实（已用字节核对）：

- 未压缩 WARC 中 `next_offset - offset = length + 4`（两条 CRLF 空行）。
- chunked gzip WARC 中 `next_offset - offset = length`，成员首尾相接、无间隔。
- gzip 的 `offset/length` 量的是**磁盘上的压缩字节**，不是解压后的字节；解压
  长度只用于解析 WARC 头与计算摘要。

## 错误分类

两类错误严格区分，退出码可机读：

- `index_inconsistent`（退出码 `1`，`IndexInconsistentError`）：文件完好，但索引
  与该偏移处的实际记录不符。典型情形：偏移 ±1 落到 gzip 成员内部、`length` 越界
  伸入相邻记录、Record-ID/URL 对不上、索引自带摘要写错。判定依据是**全文件顺序
  扫描健康** + 目标偏移不是记录边界。
- `corruption`（退出码 `2`，`CorruptionError`）：归档字节本身无法按帧读取，或内容
  与其自带 WARC 摘要不符。包含三种子情形：
  - gzip CRC/deflate 失败（`corrupt-crc`）；
  - 成员被截断、未到流结束（`corrupt-truncated`）；
  - 帧完好但载荷被改写、违背自身 `WARC-Payload/Block-Digest`
    （`corrupt-payload`，内容损坏）。
- 两者同时存在时退出码为 `3`。全部通过为 `0`。

## 摘要口径

- **block 摘要**：对整个 WARC 内容块（HTTP 头 + HTTP 载荷）计算。
- **payload 摘要**：对 HTTP 头结束（`\r\n\r\n`）之后的字节计算；非 HTTP 记录类型
  的 payload 即整个块。
- 算法与编码跟随 WARC 头里的 `algo:base32` 写法（与 warcio 的 `Digester` 一致）。
- `revisit` 记录没有载荷，其 `WARC-Payload-Digest` 是对被引用记录的**引用值**，
  不会拿空块去重算（与 warcio `NO_PAYLOAD_DIGEST_TYPES` 语义一致）；block 摘要仍
  正常校验。

## 部分载荷消费

后端常出现“只读前 N 个载荷字节就跳到下一条”的模式。`--partial N` 会额外做一次
重定位，只读取 N 字节再推进，并断言：

- 当前记录的 `offset/length` 记账不发生变化；
- 实际读到的字节数等于 `min(N, payload_size)`，不会越界；
- 能到达下一条时，下一记录边界仍与原始 oracle 一致（`*_did_not_bleed`）。

相邻成员损坏时不会反过来污染当前健康记录的结论，相关检查标记为
`warcio_next_offset_skipped_damaged_neighbour`。

## 命令用法

```bash
# 从原始字节生成真值索引（不参考任何现有索引）
python -m seekverify truth-index valid.warc.gz

# 按索引逐条重定位并核对 ID / 长度 / URL / 块与载荷摘要
python -m seekverify verify valid.warc.gz index-gzip.jsonl

# 模拟只消费部分载荷
python -m seekverify verify valid.warc.gz index-gzip.jsonl --partial 64

# 不带索引的顺序完整性扫描
python -m seekverify scan valid.warc.gz

# 生成全套确定性反例文件
python -m seekverify gen-fixtures seekverify-examples
```

`verify` 支持 `--output-format json`（每行一个 JSON 结果 + 末尾 summary）。
索引字段支持常见别名：`id/record-id/warc-record-id`、`url/uri/warc-target-uri`、
`cl/content-length`、`digest/warc-payload-digest` 等。

## 可直接复现的反例文件

`python -m seekverify gen-fixtures <dir>` 生成的文件字节确定（固定 Record-ID、
日期，gzip `mtime=0`），两次运行逐字节相同。

| 文件 | 构造方式 | 预期结果 |
|------|----------|----------|
| `valid.warc` / `valid.warc.gz` | warcio writer 产出的 4 记录归档（大载荷、空正文、request、二进制 resource），后者为 4 个 gzip 成员 | 全通过，退出码 0 |
| `index-bad-offset.jsonl` | 某条偏移 +1，落入 gzip 成员内部 | `index_inconsistent`，退出码 1 |
| `index-bad-length.jsonl` | `length` +4，声称范围伸入相邻成员 | `index_inconsistent`，退出码 1 |
| `index-bad-id.jsonl` / `index-bad-url.jsonl` | 仅篡改索引里的 ID / URL，归档不动 | `index_inconsistent`，退出码 1 |
| `index-bad-digest.jsonl` | 仅篡改索引摘要，归档与其 WARC 头仍一致 | `index_inconsistent`，退出码 1 |
| `single-member.warc.gz` | 整个 WARC 压成一个 gzip 成员 | 不可随机定位，`corruption`，退出码 2 |
| `corrupt-crc.warc.gz` | 翻转某成员中段一个压缩字节，头尾不动 | 解压 CRC 失败，`corruption`，退出码 2 |
| `corrupt-truncated.warc.gz` | 在成员中段截断文件 | 首条可读、其后 `corruption`，退出码 2/3 |
| `corrupt-payload.warc.gz` | 重写载荷后逐成员重压缩，帧与边界仍合法 | 违背自身 WARC 摘要，内容 `corruption`，退出码 2 |

复现并核对分类：

```bash
python -m seekverify gen-fixtures seekverify-examples
python -m seekverify verify seekverify-examples/valid.warc.gz \
    seekverify-examples/index-bad-offset.jsonl            # 退出码 1
python -m seekverify verify seekverify-examples/corrupt-crc.warc.gz \
    seekverify-examples/index-gzip.jsonl                  # 退出码 2
```

## 测试

```bash
python -m pytest test/test_seekverify.py -o addopts=""
```

测试覆盖：未压缩/多成员 gzip/空正文/部分载荷消费、全部索引篡改反例、三类文件
损坏、非分块 gzip、损坏邻居不污染健康记录、索引文件不被修改，以及与
`warcio index` 输出的逐字段一致性。
