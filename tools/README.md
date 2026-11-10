# XPath 规则升级离线审核工具

升级采集 XPath 时，仅比较总命中数无法发现错位（值对但节点错了、节点对但
文本变了、顺序变了）。`xpath_audit.py` 用 Parsel 对一组本地 HTML 页面分别
执行旧、新两套规则，逐规则比较**值、多重性、顺序和节点位置**，并生成可
人工复查的报告。提取完全由 Parsel 完成，节点位置只是只读元数据，不改变
Parsel 的提取语义。

## 用法

```bash
python3 tools/xpath_audit.py rules.json pages/ -o report.md
python3 tools/xpath_audit.py rules.json page1.html page2.html --format json
```

* `rules.json`：规则定义，见下。
* `pages`：一个或多个 HTML 文件或目录（目录取其中 `.html`/`.htm` 文件）。
* `-o`：报告输出路径，缺省打印到标准输出。
* `--format md|json`：报告格式，默认 Markdown。

## 规则定义

```json
{
  "rules": [
    {
      "name": "price",
      "old": "//span[@class='price']/text()",
      "new": "//div[@class='price-tag']/text()",
      "ordered": true
    }
  ]
}
```

* `ordered`（默认 `true`）：声明该规则的命中顺序是否重要。为 `false`
  时按集合（含重复计数）比较，不会因顺序差异误报 `order_changed`；
  为 `true` 时值序列不同即会被区分，集合相等但序列不同报
  `order_changed`，不会把集合相等误当序列相等。

## 差异分类

| 状态 | 含义 |
| --- | --- |
| `unchanged` | 值与节点位置均一致（无序规则按集合比较） |
| `node_moved` | 值完全一致，但来自不同的节点（页面结构调整） |
| `text_changed` | 命中节点相同，但提取的值变了（内容变化） |
| `order_changed` | 值集合相同但序列不同（仅 `ordered: true` 时报出） |
| `changed` | 值集合发生变化，含多重性（命中数）变化，报告列出增/减的值 |
| `rule_error` | 规则本身无法执行（如 XPath 语法错误） |

解析失败的页面（如空文档）不参与比较，在报告的“解析失败的页面”一节
单独列出。

报告中每条差异都附旧/新 XPath、命中数变化、节点路径（lxml getpath）
以及截断后的最小命中片段，便于人工复查。

## 示例与测试

`tests/xpath_audit_samples/` 提供人工构造的改版样本（节点移动、文本变
化、顺序变化、多重性变化、解析失败），`tests/test_xpath_audit.py` 验证
每类变化都能被发现：

```bash
python3 -m pytest tests/test_xpath_audit.py
```
