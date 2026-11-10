# Wayback 归档访问失败：异常分类与重试路径审查

本报告配套一个**完全离线、可运行**的诊断样例，不修改库的任何默认重试政策：

```bash
.venv/bin/python diagnostics/run_diagnostics.py
```

- 夹具：`diagnostics/fixtures.py`，共 24 个 memento 场景 + 10 个 CDX 场景，信号（状态码 / 正文 / 响应头）逐个变量构造，素材取自库自带 VCR 磁带中的真实报文。
- 运行器：`diagnostics/run_diagnostics.py`，用 `requests_mock` 拦截请求，驱动未修改的 `wayback.WaybackClient` / `WaybackSession` 真实代码路径；重试预算保持库默认值 6，仅把退避睡眠置零以保证秒级完成。
- 结构化结果：运行后写入 `diagnostics/output/results.json`。

---

## 一、分类总表（由诊断样例实测得出）

### 1. `get_memento()` 播放路径

| 业务情形 | 触发信号（实测组合） | 库实际抛出 | 会话层是否自动重试 | 依据 |
| --- | --- | --- | --- | --- |
| 无快照 | 404 且无任何封锁标记（空正文或普通 HTML） | `NoMementoError` | 否（404 不在重试集合） | `_client.py:1086` |
| 无快照（被覆盖的情况） | 404 但正文含 “URL has been excluded” | **`BlockedSiteError`**，不是 `NoMementoError` | 否 | 封锁检查先于 404 分支，`_client.py:1065-1074` |
| 站点被排除/下架 | 200 正文含 “This URL has been excluded …”（与真实磁带一致，无运行时头） | `BlockedSiteError` | 否（200 不重试） | `_client.py:1067`；磁带 `test_get_memento_raises_blocked_error.yaml` |
| 站点被排除/下架 | 任意状态 + 头 `X-Archive-Wayback-Runtime-Error` 含 `AdministrativeAccessControlException`，**正文可为空** | `BlockedSiteError` | 否 | `_client.py:1066` |
| 单资源被 robots 限制 | 头含 `RobotAccessControlException` | `BlockedByRobotsError` | 否 | `_client.py:1071` |
| 单资源被 robots 限制 | 200 正文任意位置含子串 `robots.txt`，无头 | `BlockedByRobotsError` | 否 | `_client.py:1072` |
| 服务临时错误 | 500/502/503/504/599/413/421，重试预算耗尽后仍失败 | `MementoPlaybackError`（消息带状态码） | **是**，最多 7 次尝试（初始 + 6 次重试） | `_client.py:330`、`_client.py:1089` |
| 服务临时错误（传输层） | 连接/读取超时、`MaxRetryError`、含 `NewConnectionError`/`Max retries` 字样的 `ConnectionError` | 预算耗尽 → `WaybackRetryError`（包装原始 cause） | 是 | `_client.py:459-474` |
| 服务临时错误（连接被对端重置） | `RemoteDisconnected`（“Connection aborted”） | **原始 `ConnectionError` 直接上抛** | **否** | `_client.py:471` 未覆盖该字样（见第三节缺口 G3） |
| 响应格式异常/形态不明 | 200 但无 `Memento-Datetime`、无运行时头、无跳转 | 通用 `MementoPlaybackError` | 否 | `_client.py:1077-1085` |
| 响应格式异常/形态不明 | 403 但无头、无正文标记 | 通用 `MementoPlaybackError`，**不会**判为封锁 | 否 | `_client.py:1089` |
| 信号冲突 | 404 状态 + 运行时头声称 `AdministrativeAccessControlException` | `BlockedSiteError`（头优先于状态码） | 否 | `_client.py:1065-1069` |
| 循环跳转 | 302 指回已访问过的 memento URL | `MementoPlaybackError`（circular） | 否 | `_client.py:1097-1098` |
| 实时限流 | 429 且无 `Memento-Datetime` | `RateLimitError`（携带 `retry_after`，无头时默认 60 秒） | **否，429 不自动重试** | `_client.py:415-417`、`_client.py:483-484` |
| 合法历史快照（非 API 故障） | 任意状态码（含 429/500）+ `Memento-Datetime` 头 | 正常返回 `Memento` | 否（memento 响应直接返回，绕过重试） | `_client.py:90-91`、`_client.py:410-413` |

### 2. `search()` CDX 路径

| 业务情形 | 触发信号 | 库实际抛出 | 自动重试 | 依据 |
| --- | --- | --- | --- | --- |
| 站点被排除 | 403 **正文**含 `AdministrativeAccessControlException` | `BlockedSiteError` | 否 | `_client.py:762-763` |
| robots 限制 | 403 正文含 `RobotAccessControlException` | `BlockedByRobotsError` | 否 | `_client.py:764-765` |
| 站点被排除（证据不足） | 403 仅**响应头**含异常类、正文为空 | 通用 `WaybackException`，封锁信息丢失 | 否 | 搜索路径只查正文不查头，`_client.py:761-767`（缺口 G1） |
| 临时服务错误 | 503 等可重试状态，预算耗尽 | 通用 `WaybackException`（包装 HTTPError） | 会话层是，上抛后否 | `_client.py:767`（缺口 G2） |
| 响应格式异常 | 200 正文行无法拆成 7 字段 / 时间戳非法 | `UnexpectedResponseFormat` | 否 | `_client.py:792-797` |
| robots 限制（解析失败分支） | 无法解析的行内含 `RobotAccessControlException` | `BlockedByRobotsError` | 否 | `_client.py:793-794` |
| 零结果（正常） | 200 空正文 | 迭代器产出 0 条，不报错 | — | 实测夹具 `search_empty_body` |

注：`UnexpectedResponseFormat` 只存在于 CDX 解析路径；memento 路径没有等价异常，形态不明的播放响应一律落入 `MementoPlaybackError`。

---

## 二、判定优先级与易混淆点（实测）

`get_memento()` 在非 memento 响应上的判定顺序固定（`_client.py:1062-1089`）：

1. `Memento-Datetime` 头存在 → 直接当作合法快照，**所有错误分类都不再执行**，且会话层对该响应不重试。
2. 运行时头/正文命中 `AdministrativeAccessControlException` 或 “URL has been excluded” → `BlockedSiteError`。
3. 运行时头/正文命中 `RobotAccessControlException` 或子串 `robots.txt` → `BlockedByRobotsError`。
4. 运行时头存在但不属于访问控制类 → 通用 `MementoPlaybackError`（头原文进消息）。
5. 2xx 但不可播放（exact 约束、超出 target_window 等）→ 通用 `MementoPlaybackError`。
6. 恰好 404 → `NoMementoError`；其他非 2xx → 通用 `MementoPlaybackError`。

易混淆实测结论：

- **404 不等于“无快照”的充分条件**：同一 404，正文带排除句式即改判 `BlockedSiteError`（夹具 `no_snapshot_404_but_excluded_marker_in_body`）。
- **200 也可能是封锁**：真实下架页就是 200 + HTML 正文，状态码本身不参与该判定（夹具 `blocked_site_200_excluded_body`，与磁带一致）。
- **头与正文可单独定案**：memento 路径中运行时头正文为空仍判封锁；CDX 路径相反，只认正文（夹具 `blocked_site_403_header_only_empty_body` 与 `search_blocked_marker_only_in_header` 对照）。
- **子串误伤**：正文只要出现 `robots.txt` 即判 robots 限制。夹具 `robots_substring_false_friend` 中一句普通页面文案 “See our robots.txt policy …” 被误判为 `BlockedByRobotsError`。
- **历史 429/500 是数据不是故障**：带 `Memento-Datetime` 的归档 429/500 正常返回（夹具 `actual_memento_archived_429/500`），与实时 API 的 429 `RateLimitError` 完全不同。

---

## 三、重试路径审查：一次错误不应被解释为永久不可访问

### 实测的三条重试链路（`demonstrate_retry_paths()`）

1. 连续两次 503 后第三次返回 memento：自动恢复成功，共 3 次尝试——**瞬时故障被默认策略吸收**。
2. 持续 503：7 次尝试（默认预算 6）后才上抛 `MementoPlaybackError`。
3. 每次都 `RemoteDisconnected`：**只尝试 1 次**即上抛原始 `ConnectionError`，因为 `should_retry_error()` 的字符串匹配不含该情形。

### 机制要点

- 可重试状态集合：`{413, 421, 500, 502, 503, 504, 599}`（`_client.py:330`）。403、404、429、200 均不在其中。
- 可重试传输异常：`ConnectTimeoutError`、`ReadTimeoutError`、`MaxRetryError`、`ProxyError`、`RetryError`、`Timeout`，以及字符串含 `NewConnectionError` 或 `Max retries` 的 `ConnectionError`（`_client.py:459-474`）。
- 退避：首次立即重试，之后 `backoff * 2**(n-1)`，默认 backoff=2；429 参考 `Retry-After`，无头时按 60 秒（`_client.py:476-490`）。
- 429 不进入重试集合，预算机制对它无效——库把等待决策交给调用方（`RateLimitError.retry_after`）。
- `NoMementoError` 是 `MementoPlaybackError` 的子类（`exceptions.py:43`），其文档明确“不应换时间窗重试同一 URL”；这是库唯一带“确定性不可用”语义的异常。

### 证据不足 / 风险点

- **G1（CDX 封锁信号丢失）**：`search()` 只在正文里找异常类名；若 Wayback 仅通过 `X-Archive-Wayback-Runtime-Error` 头传递封锁原因，调用方只能收到无分类的 `WaybackException`。memento 路径同时看头和正文，两条路径行为不一致。
- **G2（CDX 临时错误无分类）**：CDX 的 5xx 耗尽重试后上抛通用 `WaybackException`，调用方无法从异常类型区分“服务临时故障”与其他 HTTP 错误，只能字符串匹配消息。
- **G3（RemoteDisconnected 不重试）**：代码注释已承认见过该瞬时故障并“may warrant retrying”（`_client.py:467-470`），但判定逻辑未覆盖；典型瞬时断连反而一次即失败。与此同时，含 `NewConnectionError` 的 DNS 类故障被当作可重试——DNS 配置错误可能是持续性的，重试 7 次只会放大延迟。两者的重试取向存在倒挂。
- **G4（`MementoPlaybackError` 语义混合）**：同一异常既表示确定性的播放失败（循环跳转、exact 约束不满足），也承载重试耗尽后的 5xx（消息为 “503 error while loading …”）。调用方**不能**把该异常一律视为永久不可访问；需要按消息文本或自己记录重试次数二次判断。
- **G5（英文固定句式依赖）**：正文判定依赖 “URL has been excluded” 与 “robots.txt” 两个英文字面量。Wayback 页面文案一旦调整，封锁会静默降级为通用 `MementoPlaybackError`；`robots.txt` 子串则反向存在误伤（见第二节）。
- **G6（429 不自动重试）**：默认策略对实时限流直接上抛 `RateLimitError`，是否等待重试完全在调用方；多进程并发场景需要调用方自行实现退避。
- **G7（404 即无快照的单一证据）**：`NoMementoError` 只由裸 404 推出，没有交叉验证 CDX 是否确无记录。当前与 Wayback 语义一致，但若维护态错误页返回 404，会被误判为“从未归档”。现有磁带中的无快照响应同时带 `x-ts: 404` 等辅助头，库并未利用这些头做佐证。

### 给业务侧的处置建议（不涉及改库）

- 确定性结论（不重试、换时间窗也无意义）：`NoMementoError`、`BlockedSiteError`、`BlockedByRobotsError`。
- 应安排退避后重试：`RateLimitError`（按 `retry_after`）、`WaybackRetryError`、原始 `ConnectionError`/`Timeout`，以及消息含 5xx 状态码的 `MementoPlaybackError`。
- 需要人工/二次核验：通用 `MementoPlaybackError`、`UnexpectedResponseFormat`、CDX 路径的通用 `WaybackException`。
- 任何情形下都不应仅凭一次失败就标记 URL 永久不可访问；上表中只有三个封锁/无快照异常携带确定性语义，且它们都有明确的信号证据。

---

## 四、复现与产物

```bash
# 离线运行，约 0.1 秒，无网络访问
.venv/bin/python diagnostics/run_diagnostics.py

# 产物
#   控制台：两张分类表 + 三条重试链路演示 + 每条夹具的输入/输出/重试依据
#   diagnostics/output/results.json：34 个场景的结构化判定结果
```

本次审查未修改 `src/wayback/` 下任何库代码，默认 `retries=6`、`backoff=2`、`retryable_statuses` 均保持原样。
