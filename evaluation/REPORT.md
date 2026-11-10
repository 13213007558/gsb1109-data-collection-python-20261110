# Courlan 宽松 → 严格过滤离线对照评估

- 对象：仓库内 Courlan `1.4.0`（`courlan/` 源码未做任何修改）
- 对照配置：`check_url` 的四组参数
  - `L`：`strict=False`，无语言（当前宽松基线）
  - `S`：`strict=True`，无语言
  - `L-en`：`strict=False, language="en"`
  - `S-en`：`strict=True, language="en"`
  - 其余参数固定 `with_nav=False, trailing_slash=True, with_redirects=False`
- 样本：`samples.tsv` 共 79 条，覆盖查询重复值、编码路径、导航页、附件、国际化主机/PSL、规范化六类
- 结论性质：仅评估，不修改默认过滤政策

## 1. 方法与可信度

- `strict_audit.py` 用与 `core._check_url` 相同的 courlan 原语（`basic_filter → scrub_url → validate_url → domain_filter → normalize_path → extension_filter → type_filter → clean_query → path_filter → rebuild_url → lang_filter → BLACKLIST`）复刻一条带记录的判定链，逐门记录 PASS/REJECT 与命中依据。
- 每条样本 × 每组配置，复刻链的保留/拒绝结论都与真实 `check_url` 断言对齐：316/316 行自检通过（脚本结尾 `self-check OK`）。
- 另用 CLI 端到端实跑（`evaluation/cli_run/`）：四种配置留存数 58/45/55/41 与审计完全一致，输出 URL 集合与审计规范化结果逐条相等。
- 全量单测 `397 passed`；仅 2 个失败（`test_redirection`、`test_cli_broken_pipe`），均因沙盒禁止本地 socket 绑定，与过滤逻辑无关。

## 2. 总体留存

| 类别 | n | L | S | L-en | S-en |
|---|---:|---:|---:|---:|---:|
| query_dedup | 12 | 11 | 9 | 11 | 9 |
| encoding | 9 | 9 | 9 | 9 | 9 |
| navigation | 11 | 1 | 1 | 1 | 1 |
| attachment | 14 | 8 | 3 | 8 | 3 |
| i18n_host | 22 | 19 | 15 | 16 | 11 |
| normalization | 11 | 10 | 8 | 10 | 8 |
| **合计** | **79** | **58** | **45** | **55** | **41** |

- 无语言时严格化新增拒绝 13 条，拒绝门分布：`type_filter` 6、`path_filter` 4、`blacklist` 3。
- 叠加 `language=en` 后再新增 4 条（语言子域 2、路径语言 1、lang 参数 1）。
- 导航类在两套模式下结果完全相同（仅 `?p=数字` 的 C4 保留），strict 不改变导航判定。

## 3. 分维度发现与判定依据

### 3.1 保留（retention）

严格模式独有的三道门（宽松模式完全不执行）：

1. `path_filter`（`core.py:150`，仅 strict 调用）
   - 无查询的 `index/default/home` 页拒绝：A11 `/index?utm_source=x`、A12 `/index.html?utm_source=x` 在宽松模式 tracker 剥离后仍保留，严格模式经 `INDEX_PAGE_FILTER` 拒绝。带存活白名单参数可救援：`/index.html?id=1`、`/index?page=2` 严格模式保留（实测）。
   - `NOTCRAWLABLE` 拒绝联系/法务/登录页：F10 `/kontakt/`、E6 `/de/impressum` 在严格模式拒绝（正则还覆盖百分号编码的 `Datenschutzerkl%C3%A4rung`）。
2. `type_filter` 中的 strict 分支（`filters.py:245`）
   - `FILE_TYPE` 把附件/图片标记的检查范围扩到整条 URL（含查询串与 fragment）：D4 `?file=movie.mp4`、D5 `#movie.mp4`、D14 `/download?file=report.pdf`、D7 `/feed.xml`、D8 `song.mp3` 均由宽松保留变为严格拒绝。
   - `ADULT_AND_VIDEOS` 关键词：F7 `/livecams/stream` 严格拒绝；F9 `?x=porno` 因参数先被白名单剥离，重建后不再命中而保留（说明判定顺序会改变结果）。
3. `BLACKLIST`（`core.py:180`，仅 strict 生效，比对 eTLD+1 的标签和完整域）
   - E15a `facebook.com`、E15b `mail.google.com`（标签 google）、E15c `bit.ly`（标签 bit）严格拒绝、宽松保留。
   - 实测真实门户误伤：`web.de`、`www.mail.de`、`zoom.us`、`office.com` 严格模式整域丢弃，宽松模式保留。黑名单含 `web/mail/bit/last/http/office` 等通用词标签，同名正常站点会被连带拒绝。

两模式一致、与 strict 无关的拒绝（切换不引入新风险）：路径扩展名门 `extension_filter`（D1 `.pdf`、D6 `.zip`、D10 `.jpg`、D11 `.mov`、D13 大写 `.PDF` 均两模式拒绝）；feed（F8）、站点结构/导航（C1–C12 多数）、主机名图片标记（D9 `cdn-img.*`）。

### 3.2 规范化（normalization）

两模式输出相同的部分（strict 不带来新变化）：

- IDN/punycode 收敛为同一 Unicode 形式：B5 `bücher.de` 与 B6 `xn--bcher-kva.de` 都归一到 `https://www.bücher.de/katalog`，归属均为 `bücher.de`。
- 大小写主机 + 默认端口剥离（F3）、userinfo 剥离（F4）、重复斜杠折叠（F5）、根路径斜杠规范（F6a/F6b 同归一）、编码斜杠不被解码（B8 `%2F` 保留）、非 ASCII 路径百分号编码（B9）。

严格模式输出不同的部分：

- 查询白名单（`settings.ALLOWED_PARAMS` 仅 13 个键 + `lang/language`）：A7 `?foo=bar&baz=qux` → 空查询；A6 `?download=report&id=9` → `?id=9`；A8a `/search?q=…&page=1` → 仅留 `page=1`。宽松模式只删 tracker（A3/A4/A5 两模式结果已相同）。
- fragment：strict 一律丢弃（F1 `#section` 被移除）；宽松模式仅在 fragment 含 tracker 时清理（F2 两模式都清空）。
- 非法 UTF-8 百分号序列 B3 `M%FCnchen`：宽松原样保留（surrogate 往返），严格因 `q` 不在白名单而整体丢参——两种处理都不会崩溃。

查询重复值专项：`id=1&id=1`（A1）与 `id=1&id=2`（A2）在两套模式下都**不去重**，`clean_query` 只用 list 追加并按键名排序，重复键值原样保留；仅空值参数被丢弃（A9 `a=`）。若采集入口依赖重复值折叠，需要在 courlan 之外另行处理。

### 3.3 站点归属（attribution）

归属由 `get_tldinfo/hosts.get_registrable_domain` 基于内置 PSL 快照计算，**与 strict 无关，四配置结果相同**；strict 只在最后加一道黑名单。

- 多级后缀：E14c `news.bbc.co.uk` → `bbc.co.uk`，正确。
- PSL 通配符：E14a `shop.ck`、E14g `foo.kobe.jp` 本身即公共后缀，无注册域 → 两模式都在 `registrable_domain` 门拒绝；E14h `ck-shop.example.ck` 正常归属 `ck-shop.example.ck`。
- PSL 例外：E14f `city.kawasaki.jp` → 注册域 `city.kawasaki.jp`，保留。E14b `www.ck` 虽 PSL 可归属为 `www.ck`，但更早被 `validate_url` 的 www 短主机长度门槛（netloc < 8）拦截，到不了 PSL 判断——这是长度门而非后缀门的拒绝。
- IP 字面值：E14d IPv4、E14e IPv6 规范化后以 canonical 形式作为自身"域"保留，两模式一致。
- 含义：切换 strict 不会改变任何 URL 的站点归属，只会让命中黑名单标签的已有归属结果被拒绝。

### 3.4 语言判断（language）

- 路径语言线索（`PATH_LANG_FILTER`/`ALL_PATH_LANGS`）在宽松与严格都生效：E4 `/en/` 对 en 目标 +1 保留；E5 `/es/` 评分 -1，两种模式下 `L-en/S-en` 都拒绝；E7 `/en-US/` 地区码合法计 +1；E8 `/en/de/` 两线索抵消为 0 保留。
- 主机子域线索（`HOST_LANG_FILTER`）**仅 strict 计分**：E2 `de.example.org`、E3 `fr.example.org` 在 `S-en` 被拒（-1），在 `L-en` 因子域门不启用而保留。这是 strict 在语言维度唯一新增的拒绝来源。
- 查询参数线索：E9 `?lang=fr` 与 en 目标冲突时，`clean_query` 直接抛 ValueError（宽松、严格都拒绝，门为 `clean_query`）；E10 `language=en` 保留；无值的 `lang`/`lang=` 不声明语言，放行。
- 语言段超过 2 个时正则主动弃权（实测 `/en/de/fr/` 两种模式都放行）；无任何语言线索的 URL 评分恒为 0 放行，strict 不会更严。

## 4. 交付建议

### 4.1 可直接启用（行为可预期、风险低）

- 严格查询白名单与 tracker 剥离：规范化结果确定性强，且 strict 的 `path_filter` 定点修掉了"tracker 剥离后 index 页漏网"（A11/A12）。上线需配合重排抓取队列/去重键，因为 URL 身份会变化（A6/A7/A8a）。
- strict 的 fragment 全丢弃（F1/F2）：以页面实体为采集目标时合理且可预测。
- 规范化主体（IDN、端口、userinfo、斜杠、根路径）：两模式已完全一致，切换零增量风险。
- 隐藏在查询/fragment 的文件类型拦截（D4/D5/D7/D8/D14 的附件属性部分）：若采集目标明确为 HTML 文本，符合预期；真实附件路径在宽松模式本就被 `extension_filter` 拒绝，strict 只是补齐了伪装位置。

### 4.2 需要单独例外后再启用

- 黑名单整域拒绝：`web.de`、`mail.de` 这类德语区主流门户以及 `zoom.us/office.com/bit.ly` 会被严格模式整域丢弃。建议按业务目标维护豁免白名单，或裁剪 `BLACKLIST` 中的通用词标签（`web/mail/bit/last/http/office`），仅保留明确不采的平台。
- 下载落地页误判：`/download?file=*.pdf`（D14）等 HTML 下载路由会被 `FILE_TYPE` 子串匹配拒绝，而无扩展名的下载页（D2 `/download/1024`、D3 `/download.php?id=`、D12 `/downloads`）两模式都保留。建议对下载路由前缀做路径例外，不要放宽全局类型门。
- 联系/法务/登录页：`/kontakt`、`/impressum`、`/login`（F10/E6）strict 拒绝；若合规采集或实体信息需要这些页面，需路径级豁免。
- `index/default/home` 无查询页（A11/A12）：若站点以 `/index` 作为板块入口或种子，需保留入口类 URL 的例外。
- 语言子域门（仅 strict）：`de.`/`fr.` 子域在 en 目标下被拒（E2/E3），多语言站点若在非英语子域承载英语内容会误伤；建议先以路径语言门（两模式共有）为主，主机子域门灰度验证。
- 成人/视频关键词正则（F7）：基于路径子串，可能命中正常报道类 URL，启用前需用真实流量复核词表。

### 4.3 离线无法判断 / 超出本次范围

- 页面真实语言：courlan 只依据 URL 线索评分，无线索即放行；实际语言需内容级语言检测，URL 离线评估无法判定。
- 下载页真实响应类型：D2/D3/D12 返回 HTML 还是 `Content-Disposition` 附件，需 HTTP 头/内容嗅探；本次离线（未开 `with_redirects`）无法覆盖，重定向行为同样未评估。
- 语言段 >2 的弃权区间（`/en/de/fr/`）与无线索 URL：属于刻意保留的不确定区，无法从 URL 得出语言结论。
- PSL 时效：归属完全依赖 `courlan/_psl_data.py` 快照，新后缀/私有域需更新数据后重跑。
- 导航政策边界与 strict 无关但需知晓：`with_nav=True` 只救援 `is_navigation_page` 命中项（C1/C2/C3/C6/C10 可救），日期归档 C5、搜索 C7、attachment C8、gallery C12 即使开 `with_nav` 也拒绝。
- 数据外推限制：79 条为构造样本，13/79 的翻转率不能代表线上误删率；建议用真实采集 dump 直接重跑 `strict_audit.py` 量化实际影响面。

## 5. 复现方式

```bash
.venv/bin/python evaluation/strict_audit.py        # 生成 results.tsv 与 trace.md，含自检
.venv/bin/courlan --strict -i evaluation/cli_run/input.txt \
  -o evaluation/cli_run/strict.txt -d evaluation/cli_run/strict_discard.txt
```

- `evaluation/samples.tsv`：样本与意图
- `evaluation/results.tsv`：316 行（样本 × 配置）保留结论、拒绝门、规范化 URL、归属域
- `evaluation/trace.md`：每条样本逐门判定依据（含命中的正则/参数/PSL 规则）
- `evaluation/cli_run/`：四组 CLI 端到端输入输出与丢弃清单
