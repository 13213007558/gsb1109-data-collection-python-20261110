# 诊断：同样的采集值跑两遍，第二遍少一项

离线最小案例，围绕当前 `ItemLoader` 的真实调用路径，分别复现三类根因：

| 类别 | 根因 | 场景 |
| --- | --- | --- |
| A | processor 改了输入（原地修改入参对象） | `case_a1` / `case_a2` |
| B | 共享 context 里的可变对象被修改 | `case_b` / `case_b_nested` |
| C | 一次性迭代器（生成器）已被消费 | `case_c1` / `case_c2` / `case_c3` |

## 运行

```bash
.venv/bin/python diagnostics/diagnose_double_run_missing_item.py
```

脚本自带 `sys.path` 处理，不依赖可编辑安装；每个场景末尾有断言，全部通过时输出
`all 7 scenarios reproduced and assertions passed.`。

## 观测方式（不干扰被诊断行为）

- 用 `id()` / `is` 记录对象关系，用 `inspect.getgeneratorstate()` 读取迭代器状态；
  这两种操作都不会推进迭代器（脚本中有专门验证）。
- 绝不为打日志而 `list(generator)`；只对可重复迭代的 list 取 `len()` / 快照。
- 日志只打在值的自然交接点（处理器内部、单次管线调用前后），不靠重跑业务函数补日志。
- 需要保留“第一遍当时的结果”时，在第一遍结束的瞬间做浅快照（`list(...)`），
  因为 `load_item()` 每次返回的是同一个 item 对象，第二遍会原地改写它（见 C3）。

## 调用路径上的四个关键事实

代码引用基于本仓库 `itemloaders/`：

1. `arg_to_iter` 对 `Iterator`（含生成器）原样透传，不复制
   （`itemloaders/utils.py:67`）；list/tuple/set 同样原样返回。
2. `_add_value` 把 `arg_to_iter(value)` 直接交给输入处理器，再用
   `self._values[field] += arg_to_iter(processed_value)` 累积
   （`itemloaders/__init__.py:266-270`）。`+=` 会耗尽迭代器，且处理器拿到的
   list 就是调用方传入的那一个对象。
3. `get_output_value` 把内部累积列表 `self._values.get(...)` 本身交给输出处理器，
   无防御性拷贝（`itemloaders/__init__.py:341-343`）；处理器返回什么，
   `load_item` 就写回 item（`:330`），返回同一个 list 会形成
   item ↔ 内部累积列表 ↔ 处理器入参三者别名。
4. `wrap_loader_context` 用 `partial(..., loader_context=context)` 绑定的是
   context 映射本体（`itemloaders/common.py:30`）；嵌套 loader 的 context
   字典是新建的，但值按引用浅拷贝（`itemloaders/__init__.py:192-195`）。
   另外 `load_item()` 始终返回 `self.item` 同一对象（`:332`）。

## 三类问题的责任边界

库的既定行为是“按引用传递、不做防御性拷贝、迭代器透传、context 显式共享”，
本次诊断不修改库或处理器的默认行为。责任划分如下：

- **A（processor 改输入）**：责任在处理器实现。库把调用方对象本体传进来是
  既定契约；原地 `pop/remove/clear` 入参属于处理器越界。
- **B（共享 context 被改）**：责任在业务组装方式。context 本就是供处理器共享的
  可变映射；把同一可变容器跨两个独立采集周期复用，状态必然累积。
- **C（迭代器被消费）**：责任在调用方数据形态。库无法在不物化的前提下让一次性
  迭代器可重放；传入裸生成器又期望跑两遍，违反迭代器一次性语义。

## 各自修正建议（业务侧，不动库默认行为）

**A. 处理器原地修改入参**

- 处理器改为“返回新对象”，不修改入参：
  `return [v for v in values if v is not None]`，而不是 `values.remove(...)`。
- 输出处理器需要收敛时同样返回新列表；不要对入参 `pop()`。
- 无法立刻改处理器时，在调用边界传副本：`loader.add_value(field, list(source))`。
- 需要多次 `load_item()` 且输出处理器有状态时，每次基于
  `get_collected_values()` 的副本计算，或在两次加载之间深拷贝 item。

**B. 共享 context 可变状态累积**

- 每个独立采集周期使用独立容器：每次 `ItemLoader(seen=set())` 新建，不复用。
- 去重/缓存等可变状态归属单次运行，禁止挂在跨 run 复用的 context 对象上。
- 跨 loader 只需共享配置时，传不可变对象（`tuple`/`frozenset`），或处理器内
  先 `copy.copy` 再改。
- 嵌套 loader 中父级不要假设子级不修改 context 值；必要时在
  `nested_xpath/css` 前对可变值做拷贝。

**C. 生成器被消费**

- 在调用边界物化为可重放容器：`loader.add_value(field, list(gen))`。
- 两遍采集需要两份数据时，重建生成器（调用工厂函数两次），不要复用同一对象；
  `itertools.tee` 的分流本身也是一次性的，重新调用工厂最稳妥。
- 不要把生成器包进 list 传入（`[gen]`）：它会被当作普通元素存进累积列表，
  直到输出处理器才被消费，第二遍静默变空（C3），更难排查。
- 线上排查时用 `isinstance(value, collections.abc.Iterator)` 识别、
  `inspect.getgeneratorstate()` 看状态，不要用 `list(value)` 探测（那会直接消费它）。

## 场景与现象速查

- A1：输入处理器 `pop` 调用方 list → 两个新 loader 复用同一 source，逐遍变短。
- A2：输出处理器 `pop` 内部 list → 累积列表和 item 字段是同一对象，两遍持续缩水。
- B：两个 loader 共用 `context['seen']` → 第二遍全部命中去重，字段整体消失；
  输入数据本身完好。
- B-nested：嵌套 loader 的 context 字典不同，但其中的值是别名，子级修改父级可见。
- C1：同一生成器在一个 loader 上 `add_value` 两次 → 第二次静默无新增。
- C2：同一生成器喂给两个 loader → 第二个得到空字段（耗尽的生成器仍为真值，
  字段键存在但值为空列表，不是缺键）。
- C3：生成器作为 list 元素存入 → 第一遍 `load_item` 消费它，第二遍输出空列表；
  且两次 `load_item` 返回同一 item，第一份结果被原地覆盖。
