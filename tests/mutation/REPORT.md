# htmldate 装饰变形测试报告

目的：回答「页脚版权年、侧栏热点、评论时间会不会带偏 htmldate 的日期提取」。
方法：离线变形测试 —— 从含明确文章日期（2023-05-14）的页面出发，只增加或移动
无关装饰，观察 `find_date()` 在 default / `original_date=True` /
`extensive_search=False` 三种模式下结果是否变化。不修改任何源码。

## 文件

- `run_experiments.py` — 变形实验脚本（3 基线 × 12 变换 × 3 模式）
- `results_matrix.txt` — 完整实验矩阵输出
- `test_mutation.py` — pytest 固化（15 通过 + 5 个 strict xfail）
- `pages/` — 3 个基线页面 + 6 个最小对照页面

## 基线（决定成败的关键变量：文章日期所在的标记层级）

| 基线 | 文章日期的表达方式 | 到达的提取阶段 |
|---|---|---|
| `base.html` | `<p class="byline"><time datetime>` | 候选元素扫描（高精度线索） |
| `base_time_bare.html` | 裸 `<time datetime>`，无 class 线索 | `<time>` 扫描阶段 |
| `base_text.html` | 纯文本「14. Mai 2023」 | 自由文本阶段（fast 模式为 None） |

同一个装饰，加在不同基线上结果完全不同 —— 是否被带偏首先取决于文章日期
自己的标记方式，其次才是装饰本身。

## 每种变换为何不应改变文章日期

- `footer_copyright_year` / `footer_copyright_range` / `footer_moved_top`：
  版权只有年份（© 2024），没有月日，不可能成为文章日期；移动位置也不应
  让一个无法解析的片段产生影响力。
- `nav_menu_dates`：导航里的「Archiv 2024」是站点框架，且只是裸年份。
- `sidebar_hot_text` / `sidebar_hot_time`：侧栏热点日期描述的是**别的**
  文章，`<article>` 内唯一的日期才是本文日期。
- `comments_text` / `comments_time` / `comments_text_nocue` /
  `comments_time_older`：评论时间戳描述评论，不描述文章。
- `footer_last_updated` / `footer_full_date_top`：页脚「最后更新」是页面
  框架的修改时间；对 `original_date=True` 而言它恰是所求之反。default
  模式的语义是「页面上最近的日期」，取到它属于模式语义而非缺陷（见下）。

## 稳定行为（实测不变，测试已断言）

- 三种基线上，纯年份版权页脚（含移到顶部）均被忽略 —— 裸年份无法通过
  `examine_text()` 的完整日期解析。
- 导航归档年份被忽略。
- 文章日期在 `<time>` 或线索元素内时，无线索 class 的纯文本评论日期
  不干扰（`comments_text_nocue` 对 base_cued / base_time_bare 全模式稳定）。
- 带线索的署名行（`class="byline"`）压制所有**追加在后方**的装饰
  （候选扫描按文档顺序先到先得，byline 在前面）。
- 页脚版权年完全不构成威胁：三种基线 × 三种模式全部不变。

## 可复现问题（xfail 固化，附最小对照页与判断来源）

1. **评论 `class="meta"` 劫持** —— `pages/min_comment_meta_off.html`（正确）
   vs `pages/min_comment_meta_on.html`（全模式输出评论日期 2024-01-03），
   两页只差一个 class 属性。判断来源：`ID_CLASS_CUES` 含 "meta"
   （`htmldate/core.py:244`）使评论成为候选，`examine_elements`
   （`htmldate/core.py:285`）按文档顺序取第一个可解析候选，不看 newest/oldest，
   连 `original_date=True` 也被带偏。
2. **侧栏 `class="date"` 劫持** —— 同一机制，"date" 子串命中
   `is_date_candidate`（`htmldate/core.py:261`）。侧栏热点日期全模式胜出。
3. **页脚位置敏感** —— `pages/min_footer_bottom.html`（正确）vs
   `pages/min_footer_top.html`（全模式输出页脚日期 2024-03-02）。
   `<footer>` 属于 `ALWAYS_TAGS`（`htmldate/core.py:243`），移到文章前即
   成为第一个候选，连 byline 署名行也输。同一页脚在底部则无害。
4. **`original_date=True` 仍返回「最后更新」** —— 页脚
   「Zuletzt aktualisiert: 02.03.2024」在 original 模式下照样返回；
   候选扫描没有「原始/修改」之分（`htmldate/core.py:285`）。
5. **纯文本文章日期输给任何装饰性 `<time>`** ——
   `pages/min_text_plus_sidebar_time.html`：`original_date=True` 返回侧栏的
   2023-11-05 而非文章的 2023-05-14。`<time>` 扫描阶段
   （`htmldate/core.py:562`）先于自由文本阶段返回，纯文本文章日期根本没有
   参与竞争的机会。

## 依赖站点结构才能判断的情况（不断言为缺陷）

- **default 模式取最新 `<time>`**：`pages/min_sidebar_time.html` 中 default
  模式返回侧栏的 2024-03-12。这符合 find_date 文档的「most recent」语义；
  若站点把热点列表也包成 `<time>`，default 模式必然被带偏，应改用
  `original_date=True`（该模式下本例正确）。
- **default 模式取最新自由文本日期**：纯文本文章日期 + 纯文本评论时，
  自由文本阶段（`htmldate/core.py:961` 的 `compare_reference` 循环）按
  newest/oldest 取舍，default 取评论日期、original 取文章日期。是否合理
  取决于调用方要「发布日期」还是「页面最后活动」。
- **fast 模式读不到纯文本日期**：`extensive_search=False` 时自由文本阶段
  不运行，base_text 基线本身就是 None，与装饰无关，属已知能力边界。

## 结论

- 页脚版权年（裸年份）：完全不构成威胁。
- 侧栏热点 / 评论时间：是否带偏取决于**文章日期的标记层级**与**装饰的
  class 命名**。文章日期有线索（byline/date 类 class、meta 标签）时安全；
  一旦文章日期只是裸 `<time>` 或纯文本，装饰里的 `class="meta"`、
  `class="date"`、`<footer>`、`<time>` 都会按「文档顺序先到先得」或
  「 newest/oldest 全局比较」胜出。
- 最稳健的使用方式：给文章日期加语义化标记（meta/JSON-LD/线索 class），
  或对提取目标明确时使用 `original_date=True`。
