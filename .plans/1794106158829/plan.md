# Plan: Content-Disposition filename parsing in w3lib.http — DONE

## Status
All steps complete. Implemented in w3lib/http.py, covered by tests/test_http.py,
NEWS updated. Full suite (1801 passed), ruff + ruff-format + strict mypy clean.

## Delivered
- ContentDispositionError(position=...) — 异常位置
- ContentDispositionFilename NamedTuple — filename/source/basic/extended/candidates/errors
- content_disposition_filename(header, *, fallback_to_basic=False)
- 普通/扩展/连续参数；引号内分号不切分；重复编号、缺段、charset 冲突均拒绝拼接
- 扩展无效时回退与否由 fallback_to_basic 决定；仅元数据，不写磁盘
- 缺段 vs 合法多段对照测试证明不会拼出另一份文件的名字
