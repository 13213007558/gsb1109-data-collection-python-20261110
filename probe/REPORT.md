# mechanize 后台表单兼容性探针报告

- 对象：workspace 内 mechanize 0.4.10（Python 3.12.13，HTML 解析后端 html5lib 1.1）
- 方法：`run_probe.py` 通过进程内 `ProbeHandler` 提供最小页面并拦截全部提交（零网络、零 socket），
  同时记录客户端生成的 `Request` 转储与处理器侧收到的“线上”请求，逐条与 HTML 表单提交语义比对。
- 复现：`.venv/bin/python probe/run_probe.py`（幂等，重跑会重建 `results/` 证据）

## 结论总览

| 场景 | 判定 | 差异分类 | 证据 |
|---|---|---|---|
| 重复字段提交语义 | 可直接使用 | — | `results/01-duplicate-fields.json` |
| 重名字段赋值写法 | 需按约定调用 | 调用方式错误 | 同上，step `naive: br.form['tag'] = 'first'` |
| 同名 checkbox 控件表示 | 需业务侧留意 | 解析器差异 | 同上，断言 1（2 个 DOM 控件 → 1 个多值控件） |
| 禁用 fieldset | 需业务侧适配 | 库不支持 | `results/02-disabled-fieldset.json` |
| 外置控件（`form=` 属性） | 可直接使用 | — | `results/03-external-controls.json` |
| 提交按钮覆盖 `formmethod` | 可直接使用 | — | `results/04-submit-overrides.json` |
| 提交按钮覆盖 `formaction`（相对地址） | 需业务侧适配 | 库不支持 | 同上 |
| 提交按钮覆盖 `formenctype=text/plain` | 需业务侧适配 | 库不支持 | 同上 |
| 文件输入（multipart 上传/空文件） | 可直接使用 | — | `results/05-file-input.json` |
| 文件控件赋值写法 | 需按约定调用 | 调用方式错误 | 同上，step `naive: br.form['doc'] = ...` |
| 多选/单选提交语义 | 可直接使用 | — | `results/06-multi-select.json` |
| 列表控件赋值写法 | 需按约定调用 | 调用方式错误 | 同上，两个 convention step |

## 分场景记录

### 01 重复字段（`pages/01_duplicate_fields.html`）
- 预期语义：两个 `name=tag` 文本框、两个 `name=opt` 的勾选框全部按文档序提交。
- 实际：修正调用后提交体为 `tag=first&tag=second&opt=a&opt=b&go=1`，顺序与取值全部符合（断言 2–4 通过）。
- 差异 1（调用方式错误）：`br.form['tag'] = 'first'` 抛 `AmbiguityError`；须用
  `set_value('first', 'tag', nr=0)` / `nr=1` 逐个赋值。
- 差异 2（解析器差异）：两个同名 checkbox 被合并为 1 个多值控件（DOM 中为 2 个）。
  提交结果不受影响，但按控件个数遍历/断言的业务代码会观察到差异。

### 02 禁用 fieldset（`pages/02_disabled_fieldset.html`）
- 预期语义（HTML5）：`fieldset[disabled]` 的后代控件（首个 `legend` 内除外）不得提交。
- 实际：朴素提交泄漏 `inner=x&inner_sel=s1`（断言 1–2 失败）；mechanize 只识别控件自身的
  `disabled` 属性，完全不解析 fieldset（`mechanize/_form.py` 无 fieldset 处理）。
- 分类：库不支持。业务适配：提交前对 fieldset 后代控件逐一 `control.disabled = True`，
  适配后提交体与 HTML5 语义一致（断言 3 通过，证据 `results/02-disabled-fieldset/02-disabled-fieldset_adapted_request.json`）。

### 03 外置控件（`pages/03_external_controls.html`）
- 预期语义：`form="f"` 的表单外控件（含提交按钮）归入表单 f；`form="no_such_form"` 与无
  `form` 属性的游离控件不随 f 提交。
- 实际：提交体 `internal=i&ext=e&go=ext_submit`，完全符合（2/2）。`form=` 关联由
  `mechanize/_form.py:144-146` 实现；未知 id 的控件落入 global_form，不污染目标表单。
- 判定：可直接使用。

### 04 提交按钮覆盖（`pages/04_submit_overrides.html`）
- 机制存在：`HTMLForm._click` 会读取被点按钮的 `formmethod/formaction/formenctype`
  （`mechanize/_form_controls.py:2472-2483`），`formmethod=post` 生效（断言 2 通过）。
- 差异 1（库不支持）：`formaction` 不做 base URL 解析，相对地址原样进入 Request，
  发送时报 `ValueError: unknown url type: /submit/04-override`。业务适配：点击前
  `ctl.attrs['formaction'] = urljoin(base, ctl.attrs['formaction'])`，适配后
  `POST /submit/04-override` 且正文 `q=1&btn=rel` 正确（断言 4–5 通过）。
- 差异 2（库不支持）：`formenctype="text/plain"` 抛
  `ValueError: unknown POST form encoding type 'text/plain'`（仅支持 urlencoded 与
  multipart，`mechanize/_form_controls.py:2534-2550`）。业务侧需改用 urlencoded 或自行编码。

### 05 文件输入（`pages/05_file_input.html`）
- 预期语义：multipart 正文携带文本域与文件域（filename、part Content-Type、文件字节）；
  未选文件的控件仍发送 `filename=""` 的空 part。
- 实际：7/7 全部符合，含空文件控件的空 part（证据 `results/05-file-input/` 与
  `results/wire/00{6,7}_body.bin`）。
- 差异（调用方式错误）：`br.form['doc'] = '/tmp/report.txt'` 抛
  `ValueError: value attribute is readonly`；必须用
  `br.form.add_file(文件对象, content_type, filename, name='doc')`。

### 06 多选行为（`pages/06_multi_select.html`）
- 预期语义：`<select multiple>` 提交全部选中项；无 `selected` 属性的单选 select 默认选中首项。
- 实际：多选 `opts=a&opts=c` 正确；单选默认提交 `single=x`，与浏览器一致（3/3）。
- 差异（调用方式错误）：ListControl 赋值必须给序列——`br.form['opts'] = 'c'` 与
  `br.form['single'] = 'y'` 均抛 `TypeError: ListControl, must set a sequence`；
  正确写法 `br.form['opts'] = ['a', 'c']`、`br.form['single'] = ['y']`（单选也要列表）。

## 证据入口

- 探针脚本：`probe/run_probe.py`（每个 case 的动作、异常、断言全部落盘）
- 最小页面：`probe/pages/01..06_*.html`（每个差异一个最小页面，保留不删）
- 逐案结果：`probe/results/<case-id>.json`（steps + assertions）
- 请求转储：`probe/results/<case-id>/*_request.json` 与 `*_body.bin`（客户端视角）
- 线上留痕：`probe/results/wire/index.json` + `NNN_body.bin`（处理器实际收到的请求）
- 汇总：`probe/results/summary.json`

注：探针不修改 mechanize 任何源码；所有失败用例（AmbiguityError、text/plain ValueError、
fieldset 泄漏等）均作为证据保留在上述 JSON 中。
