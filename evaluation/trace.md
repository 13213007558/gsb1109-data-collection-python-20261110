# Gate-by-gate trace (courlan 1.4.0, trailing_slash=True)

### A1 [query_dedup] http://www.test.org/page?id=1&id=1
intent: 同键同值重复，观察去重与规范化
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: id: lenient=keep/strict=keep; id: lenient=keep/strict=keep || lenient->'id=1&id=1' strict->'id=1&id=1'
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://www.test.org/page?id=1&id=1 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1&id=1
    - rebuild_url: PASS http://www.test.org/page?id=1&id=1
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> http://www.test.org/page?id=1&id=1 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1&id=1
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page?id=1&id=1
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> http://www.test.org/page?id=1&id=1 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1&id=1
    - rebuild_url: PASS http://www.test.org/page?id=1&id=1
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> http://www.test.org/page?id=1&id=1 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1&id=1
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page?id=1&id=1
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### A2 [query_dedup] http://www.test.org/page?id=1&id=2
intent: 同键不同值，观察多值保留
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: id: lenient=keep/strict=keep; id: lenient=keep/strict=keep || lenient->'id=1&id=2' strict->'id=1&id=2'
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://www.test.org/page?id=1&id=2 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1&id=2
    - rebuild_url: PASS http://www.test.org/page?id=1&id=2
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> http://www.test.org/page?id=1&id=2 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1&id=2
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page?id=1&id=2
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> http://www.test.org/page?id=1&id=2 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1&id=2
    - rebuild_url: PASS http://www.test.org/page?id=1&id=2
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> http://www.test.org/page?id=1&id=2 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1&id=2
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page?id=1&id=2
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### A3 [query_dedup] http://www.test.org/page?utm_source=news&id=7
intent: 跟踪参数与白名单参数混合
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: utm_source: lenient=drop(tracker)/strict=drop; id: lenient=keep/strict=keep || lenient->'id=7' strict->'id=7'
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://www.test.org/page?id=7 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=7
    - rebuild_url: PASS http://www.test.org/page?id=7
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> http://www.test.org/page?id=7 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=7
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page?id=7
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> http://www.test.org/page?id=7 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=7
    - rebuild_url: PASS http://www.test.org/page?id=7
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> http://www.test.org/page?id=7 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=7
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page?id=7
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### A4 [query_dedup] http://www.test.org/page?session=abc&p=3
intent: session 类跟踪参数
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: session: lenient=drop(tracker)/strict=drop; p: lenient=keep/strict=keep || lenient->'p=3' strict->'p=3'
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://www.test.org/page?p=3 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS p=3
    - rebuild_url: PASS http://www.test.org/page?p=3
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> http://www.test.org/page?p=3 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS p=3
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page?p=3
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> http://www.test.org/page?p=3 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS p=3
    - rebuild_url: PASS http://www.test.org/page?p=3
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> http://www.test.org/page?p=3 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS p=3
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page?p=3
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### A5 [query_dedup] http://www.test.org/page?ref=home&page=2
intent: ref 跟踪参数 + 分页白名单
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: ref: lenient=drop(tracker)/strict=drop; page: lenient=keep/strict=keep || lenient->'page=2' strict->'page=2'
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://www.test.org/page?page=2 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS page=2
    - rebuild_url: PASS http://www.test.org/page?page=2
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> http://www.test.org/page?page=2 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS page=2
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page?page=2
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> http://www.test.org/page?page=2 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS page=2
    - rebuild_url: PASS http://www.test.org/page?page=2
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> http://www.test.org/page?page=2 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS page=2
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page?page=2
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### A6 [query_dedup] http://www.test.org/page?download=report&id=9
intent: download 作为查询键（非附件）
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: download: lenient=keep/strict=drop; id: lenient=keep/strict=keep || lenient->'download=report&id=9' strict->'id=9'
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://www.test.org/page?download=report&id=9 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS download=report&id=9
    - rebuild_url: PASS http://www.test.org/page?download=report&id=9
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> http://www.test.org/page?id=9 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=9
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page?id=9
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> http://www.test.org/page?download=report&id=9 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS download=report&id=9
    - rebuild_url: PASS http://www.test.org/page?download=report&id=9
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> http://www.test.org/page?id=9 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=9
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page?id=9
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### A7 [query_dedup] http://www.test.org/page?foo=bar&baz=qux
intent: 严格模式白名单外参数全被剥离
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: foo: lenient=keep/strict=drop; baz: lenient=keep/strict=drop || lenient->'baz=qux&foo=bar' strict->''
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://www.test.org/page?baz=qux&foo=bar [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS baz=qux&foo=bar
    - rebuild_url: PASS http://www.test.org/page?baz=qux&foo=bar
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> http://www.test.org/page [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> http://www.test.org/page?baz=qux&foo=bar [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS baz=qux&foo=bar
    - rebuild_url: PASS http://www.test.org/page?baz=qux&foo=bar
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> http://www.test.org/page [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### A8a [query_dedup] http://www.test.org/search?q=courlan&page=1
intent: 无尾斜杠 search 路径
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: q: lenient=keep/strict=drop; page: lenient=keep/strict=keep || lenient->'page=1&q=courlan' strict->'page=1'
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://www.test.org/search?page=1&q=courlan [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /search
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS page=1&q=courlan
    - rebuild_url: PASS http://www.test.org/search?page=1&q=courlan
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> http://www.test.org/search?page=1 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /search
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS page=1
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/search?page=1
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> http://www.test.org/search?page=1&q=courlan [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /search
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS page=1&q=courlan
    - rebuild_url: PASS http://www.test.org/search?page=1&q=courlan
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> http://www.test.org/search?page=1 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /search
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS page=1
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/search?page=1
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### A8b [query_dedup] http://www.test.org/search/?q=courlan&page=1
intent: 带尾斜杠 search 命中站点结构
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: q: lenient=keep/strict=drop; page: lenient=keep/strict=keep || lenient->'page=1&q=courlan' strict->'page=1'
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /search/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /search/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- L-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /search/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /search/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern

### A9 [query_dedup] http://www.test.org/page?a=&b=2
intent: 空值参数处理
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: a: lenient=keep/strict=drop; b: lenient=keep/strict=drop || lenient->'b=2' strict->''
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://www.test.org/page?b=2 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS b=2
    - rebuild_url: PASS http://www.test.org/page?b=2
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> http://www.test.org/page [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> http://www.test.org/page?b=2 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS b=2
    - rebuild_url: PASS http://www.test.org/page?b=2
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> http://www.test.org/page [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### A11 [query_dedup] http://www.test.org/index?utm_source=x
intent: 跟踪参数剥离后暴露 index 首页
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: utm_source: lenient=drop(tracker)/strict=drop || lenient->'' strict->''
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://www.test.org/index [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /index
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://www.test.org/index
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- S: DROP@path_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /index
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: REJECT index/default/home page without query
- L-en: KEEP -> http://www.test.org/index [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /index
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://www.test.org/index
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: DROP@path_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /index
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: REJECT index/default/home page without query

### A12 [query_dedup] http://www.test.org/index.html?utm_source=x
intent: 剥离后暴露 index.html 首页
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: utm_source: lenient=drop(tracker)/strict=drop || lenient->'' strict->''
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://www.test.org/index.html [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /index.html
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://www.test.org/index.html
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- S: DROP@path_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /index.html
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: REJECT index/default/home page without query
- L-en: KEEP -> http://www.test.org/index.html [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /index.html
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://www.test.org/index.html
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: DROP@path_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /index.html
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: REJECT index/default/home page without query

### B1 [encoding] http://www.test.org/a%20b%20c
intent: 百分号编码空格路径
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://www.test.org/a%20b%20c [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /a%20b%20c
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://www.test.org/a%20b%20c
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> http://www.test.org/a%20b%20c [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /a%20b%20c
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/a%20b%20c
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> http://www.test.org/a%20b%20c [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /a%20b%20c
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://www.test.org/a%20b%20c
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> http://www.test.org/a%20b%20c [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /a%20b%20c
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/a%20b%20c
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### B2 [encoding] http://www.test.org/a b c
intent: 字面空格，scrub 阶段直接压缩
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://www.test.org/abc [test.org]
    - basic_filter: PASS
    - scrub_url: PASS http://www.test.org/a b c -> http://www.test.org/abc
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /abc
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://www.test.org/abc
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> http://www.test.org/abc [test.org]
    - basic_filter: PASS
    - scrub_url: PASS http://www.test.org/a b c -> http://www.test.org/abc
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /abc
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/abc
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> http://www.test.org/abc [test.org]
    - basic_filter: PASS
    - scrub_url: PASS http://www.test.org/a b c -> http://www.test.org/abc
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /abc
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://www.test.org/abc
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> http://www.test.org/abc [test.org]
    - basic_filter: PASS
    - scrub_url: PASS http://www.test.org/a b c -> http://www.test.org/abc
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /abc
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/abc
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### B3 [encoding] http://www.test.org/page?q=M%FCnchen
intent: 非法 UTF-8 百分号序列（surrogate 往返）
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: q: lenient=keep/strict=drop || lenient->'q=M%FCnchen' strict->''
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://www.test.org/page?q=M%FCnchen [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS q=M%FCnchen
    - rebuild_url: PASS http://www.test.org/page?q=M%FCnchen
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> http://www.test.org/page [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> http://www.test.org/page?q=M%FCnchen [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS q=M%FCnchen
    - rebuild_url: PASS http://www.test.org/page?q=M%FCnchen
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> http://www.test.org/page [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### B4 [encoding] http://www.test.org/page?q=M%C3%BCnchen
intent: 合法 UTF-8（München）查询
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: q: lenient=keep/strict=drop || lenient->'q=M%C3%BCnchen' strict->''
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://www.test.org/page?q=M%C3%BCnchen [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS q=M%C3%BCnchen
    - rebuild_url: PASS http://www.test.org/page?q=M%C3%BCnchen
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> http://www.test.org/page [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> http://www.test.org/page?q=M%C3%BCnchen [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS q=M%C3%BCnchen
    - rebuild_url: PASS http://www.test.org/page?q=M%C3%BCnchen
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> http://www.test.org/page [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/page
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### B5 [encoding] https://www.bücher.de/katalog
intent: Unicode IDN 主机
attribution: label=bücher registrable=bücher.de | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.bücher.de/katalog [bücher.de]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.bücher.de
    - domain_filter: PASS host=www.bücher.de label=bücher registrable=bücher.de
    - normalize_path: PASS /katalog
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.bücher.de/katalog
    - blacklist: PASS label=bücher not blacklisted
- S: KEEP -> https://www.bücher.de/katalog [bücher.de]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.bücher.de
    - domain_filter: PASS host=www.bücher.de label=bücher registrable=bücher.de
    - normalize_path: PASS /katalog
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.bücher.de/katalog
    - blacklist: PASS label=bücher not blacklisted
- L-en: KEEP -> https://www.bücher.de/katalog [bücher.de]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.bücher.de
    - domain_filter: PASS host=www.bücher.de label=bücher registrable=bücher.de
    - normalize_path: PASS /katalog
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.bücher.de/katalog
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=bücher not blacklisted
- S-en: KEEP -> https://www.bücher.de/katalog [bücher.de]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.bücher.de
    - domain_filter: PASS host=www.bücher.de label=bücher registrable=bücher.de
    - normalize_path: PASS /katalog
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.bücher.de/katalog
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=bücher not blacklisted

### B6 [encoding] https://www.xn--bcher-kva.de/katalog
intent: punycode 主机，应与 B5 归一为同一形式
attribution: label=xn--bcher-kva registrable=xn--bcher-kva.de | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.bücher.de/katalog [bücher.de]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.xn--bcher-kva.de
    - domain_filter: PASS host=www.bücher.de label=bücher registrable=bücher.de
    - normalize_path: PASS /katalog
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.bücher.de/katalog
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=bücher not blacklisted
- S: KEEP -> https://www.bücher.de/katalog [bücher.de]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.xn--bcher-kva.de
    - domain_filter: PASS host=www.bücher.de label=bücher registrable=bücher.de
    - normalize_path: PASS /katalog
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.bücher.de/katalog
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=bücher not blacklisted
- L-en: KEEP -> https://www.bücher.de/katalog [bücher.de]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.xn--bcher-kva.de
    - domain_filter: PASS host=www.bücher.de label=bücher registrable=bücher.de
    - normalize_path: PASS /katalog
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.bücher.de/katalog
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=bücher not blacklisted
- S-en: KEEP -> https://www.bücher.de/katalog [bücher.de]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.xn--bcher-kva.de
    - domain_filter: PASS host=www.bücher.de label=bücher registrable=bücher.de
    - normalize_path: PASS /katalog
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.bücher.de/katalog
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=bücher not blacklisted

### B7 [encoding] http://www.test.org/caf%c3%a9/menu
intent: 小写百分号编码路径段
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://www.test.org/caf%c3%a9/menu [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /caf%c3%a9/menu
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://www.test.org/caf%c3%a9/menu
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> http://www.test.org/caf%c3%a9/menu [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /caf%c3%a9/menu
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/caf%c3%a9/menu
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> http://www.test.org/caf%c3%a9/menu [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /caf%c3%a9/menu
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://www.test.org/caf%c3%a9/menu
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> http://www.test.org/caf%c3%a9/menu [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /caf%c3%a9/menu
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/caf%c3%a9/menu
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### B8 [encoding] http://www.test.org/%2Fetc/passwd
intent: 编码斜杠不应被解码
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://www.test.org/%2Fetc/passwd [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /%2Fetc/passwd
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://www.test.org/%2Fetc/passwd
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> http://www.test.org/%2Fetc/passwd [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /%2Fetc/passwd
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/%2Fetc/passwd
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> http://www.test.org/%2Fetc/passwd [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /%2Fetc/passwd
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://www.test.org/%2Fetc/passwd
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> http://www.test.org/%2Fetc/passwd [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /%2Fetc/passwd
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://www.test.org/%2Fetc/passwd
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### B9 [encoding] https://例え.jp/テスト
intent: 日文 IDN 主机与非 ASCII 路径
attribution: label=例え registrable=例え.jp | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://例え.jp/%E3%83%86%E3%82%B9%E3%83%88 [例え.jp]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=例え.jp
    - domain_filter: PASS host=例え.jp label=例え registrable=例え.jp
    - normalize_path: PASS /テスト -> /%E3%83%86%E3%82%B9%E3%83%88
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://例え.jp/%E3%83%86%E3%82%B9%E3%83%88
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=例え not blacklisted
- S: KEEP -> https://例え.jp/%E3%83%86%E3%82%B9%E3%83%88 [例え.jp]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=例え.jp
    - domain_filter: PASS host=例え.jp label=例え registrable=例え.jp
    - normalize_path: PASS /テスト -> /%E3%83%86%E3%82%B9%E3%83%88
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://例え.jp/%E3%83%86%E3%82%B9%E3%83%88
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=例え not blacklisted
- L-en: KEEP -> https://例え.jp/%E3%83%86%E3%82%B9%E3%83%88 [例え.jp]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=例え.jp
    - domain_filter: PASS host=例え.jp label=例え registrable=例え.jp
    - normalize_path: PASS /テスト -> /%E3%83%86%E3%82%B9%E3%83%88
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://例え.jp/%E3%83%86%E3%82%B9%E3%83%88
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=例え not blacklisted
- S-en: KEEP -> https://例え.jp/%E3%83%86%E3%82%B9%E3%83%88 [例え.jp]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=例え.jp
    - domain_filter: PASS host=例え.jp label=例え registrable=例え.jp
    - normalize_path: PASS /テスト -> /%E3%83%86%E3%82%B9%E3%83%88
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://例え.jp/%E3%83%86%E3%82%B9%E3%83%88
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=例え not blacklisted

### C1 [navigation] https://www.test.org/page/2
intent: 分页导航页
attribution: label=test registrable=test.org | navigation=True not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page/2
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page/2
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- L-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page/2
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page/2
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern

### C2 [navigation] https://www.test.org/category/news/
intent: 分类导航
attribution: label=test registrable=test.org | navigation=True not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /category/news/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /category/news/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- L-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /category/news/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /category/news/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern

### C3 [navigation] https://www.test.org/tag/courlan
intent: 标签导航
attribution: label=test registrable=test.org | navigation=True not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /tag/courlan
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /tag/courlan
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- L-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /tag/courlan
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /tag/courlan
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern

### C4 [navigation] https://www.test.org/blog/?p=123
intent: WordPress 短链 ?p= 数字
attribution: label=test registrable=test.org | navigation=True not_crawlable=False
query audit: p: lenient=keep/strict=keep || lenient->'p=123' strict->'p=123'
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/blog/?p=123 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /blog/
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS p=123
    - rebuild_url: PASS https://www.test.org/blog/?p=123
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org/blog/?p=123 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /blog/
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS p=123
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/blog/?p=123
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> https://www.test.org/blog/?p=123 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /blog/
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS p=123
    - rebuild_url: PASS https://www.test.org/blog/?p=123
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> https://www.test.org/blog/?p=123 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /blog/
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS p=123
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/blog/?p=123
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### C5 [navigation] https://www.test.org/2020/01/
intent: 日期归档路径
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /2020/01/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /2020/01/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- L-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /2020/01/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /2020/01/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern

### C6 [navigation] https://www.test.org/archives/
intent: 归档页
attribution: label=test registrable=test.org | navigation=True not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /archives/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /archives/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- L-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /archives/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /archives/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern

### C7 [navigation] https://www.test.org/suche/?q=hund
intent: 德语站内搜索
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: q: lenient=keep/strict=drop || lenient->'q=hund' strict->''
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /suche/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /suche/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- L-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /suche/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /suche/
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern

### C8 [navigation] https://www.test.org/attachment/report
intent: 附件挂载页（WordPress）
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /attachment/report
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /attachment/report
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- L-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /attachment/report
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /attachment/report
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern

### C9 [navigation] https://www.test.org/wp-json/wp/v2/posts
intent: WP REST API 结构路径
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /wp-json/wp/v2/posts
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /wp-json/wp/v2/posts
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- L-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /wp-json/wp/v2/posts
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /wp-json/wp/v2/posts
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern

### C10 [navigation] https://www.test.org/author/max
intent: 作者归档
attribution: label=test registrable=test.org | navigation=True not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /author/max
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /author/max
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- L-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /author/max
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /author/max
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern

### C12 [navigation] https://www.test.org/gallery/1
intent: 图库导航拼写变体
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /gallery/1
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /gallery/1
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- L-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /gallery/1
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /gallery/1
    - extension_filter: PASS
    - type_filter#1: REJECT site-structure pattern

### D1 [attachment] https://www.test.org/downloads/2024/report.pdf
intent: 真实 PDF 附件
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /downloads/2024/report.pdf
    - extension_filter: REJECT non-whitelisted extension on /downloads/2024/report.pdf
- S: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /downloads/2024/report.pdf
    - extension_filter: REJECT non-whitelisted extension on /downloads/2024/report.pdf
- L-en: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /downloads/2024/report.pdf
    - extension_filter: REJECT non-whitelisted extension on /downloads/2024/report.pdf
- S-en: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /downloads/2024/report.pdf
    - extension_filter: REJECT non-whitelisted extension on /downloads/2024/report.pdf

### D2 [attachment] https://www.test.org/download/1024
intent: 下载落地 HTML 页（无扩展名）
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/download/1024 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /download/1024
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/download/1024
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org/download/1024 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /download/1024
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/download/1024
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> https://www.test.org/download/1024 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /download/1024
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/download/1024
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> https://www.test.org/download/1024 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /download/1024
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/download/1024
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### D3 [attachment] https://www.test.org/download.php?id=1024
intent: 白名单扩展名 .php + 白名单参数
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: id: lenient=keep/strict=keep || lenient->'id=1024' strict->'id=1024'
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/download.php?id=1024 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /download.php
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1024
    - rebuild_url: PASS https://www.test.org/download.php?id=1024
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org/download.php?id=1024 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /download.php
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1024
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/download.php?id=1024
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> https://www.test.org/download.php?id=1024 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /download.php
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1024
    - rebuild_url: PASS https://www.test.org/download.php?id=1024
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> https://www.test.org/download.php?id=1024 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /download.php
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1024
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/download.php?id=1024
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### D4 [attachment] https://www.test.org/page?file=movie.mp4
intent: 附件类型藏在查询串
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: file: lenient=keep/strict=drop || lenient->'file=movie.mp4' strict->''
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/page?file=movie.mp4 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS file=movie.mp4
    - rebuild_url: PASS https://www.test.org/page?file=movie.mp4
    - blacklist: PASS label=test not blacklisted
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: REJECT file/image type marker '.mp4'
- L-en: KEEP -> https://www.test.org/page?file=movie.mp4 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS file=movie.mp4
    - rebuild_url: PASS https://www.test.org/page?file=movie.mp4
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: REJECT file/image type marker '.mp4'

### D5 [attachment] https://www.test.org/page#movie.mp4
intent: 附件类型藏在 fragment
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/page#movie.mp4 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/page#movie.mp4
    - blacklist: PASS label=test not blacklisted
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: REJECT file/image type marker '.mp4'
- L-en: KEEP -> https://www.test.org/page#movie.mp4 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/page#movie.mp4
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: REJECT file/image type marker '.mp4'

### D6 [attachment] https://www.test.org/data/archive.zip
intent: 压缩包附件
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /data/archive.zip
    - extension_filter: REJECT non-whitelisted extension on /data/archive.zip
- S: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /data/archive.zip
    - extension_filter: REJECT non-whitelisted extension on /data/archive.zip
- L-en: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /data/archive.zip
    - extension_filter: REJECT non-whitelisted extension on /data/archive.zip
- S-en: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /data/archive.zip
    - extension_filter: REJECT non-whitelisted extension on /data/archive.zip

### D7 [attachment] https://www.test.org/feed.xml
intent: XML：宽松放行、严格按类型拒绝
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/feed.xml [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /feed.xml
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/feed.xml
    - blacklist: PASS label=test not blacklisted
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /feed.xml
    - extension_filter: PASS
    - type_filter#1: REJECT file/image type marker '.xml'
- L-en: KEEP -> https://www.test.org/feed.xml [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /feed.xml
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/feed.xml
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /feed.xml
    - extension_filter: PASS
    - type_filter#1: REJECT file/image type marker '.xml'

### D8 [attachment] https://www.test.org/audio/song.mp3
intent: 音频附件
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/audio/song.mp3 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /audio/song.mp3
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/audio/song.mp3
    - blacklist: PASS label=test not blacklisted
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /audio/song.mp3
    - extension_filter: PASS
    - type_filter#1: REJECT file/image type marker '.mp3'
- L-en: KEEP -> https://www.test.org/audio/song.mp3 [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /audio/song.mp3
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/audio/song.mp3
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /audio/song.mp3
    - extension_filter: PASS
    - type_filter#1: REJECT file/image type marker '.mp3'

### D9 [attachment] https://cdn-img.example.org/pic
intent: 主机名含图片标记
attribution: label=example registrable=example.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@domain_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=cdn-img.example.org
    - domain_filter: REJECT unsuitable host name
- S: DROP@domain_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=cdn-img.example.org
    - domain_filter: REJECT unsuitable host name
- L-en: DROP@domain_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=cdn-img.example.org
    - domain_filter: REJECT unsuitable host name
- S-en: DROP@domain_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=cdn-img.example.org
    - domain_filter: REJECT unsuitable host name

### D10 [attachment] https://www.test.org/images/banner.jpg
intent: 图片附件
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /images/banner.jpg
    - extension_filter: REJECT non-whitelisted extension on /images/banner.jpg
- S: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /images/banner.jpg
    - extension_filter: REJECT non-whitelisted extension on /images/banner.jpg
- L-en: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /images/banner.jpg
    - extension_filter: REJECT non-whitelisted extension on /images/banner.jpg
- S-en: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /images/banner.jpg
    - extension_filter: REJECT non-whitelisted extension on /images/banner.jpg

### D11 [attachment] https://www.test.org/media/clip.mov
intent: 视频附件
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /media/clip.mov
    - extension_filter: REJECT non-whitelisted extension on /media/clip.mov
- S: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /media/clip.mov
    - extension_filter: REJECT non-whitelisted extension on /media/clip.mov
- L-en: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /media/clip.mov
    - extension_filter: REJECT non-whitelisted extension on /media/clip.mov
- S-en: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /media/clip.mov
    - extension_filter: REJECT non-whitelisted extension on /media/clip.mov

### D12 [attachment] https://www.test.org/downloads
intent: 下载栏目首页（无扩展名无尾斜杠）
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/downloads [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /downloads
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/downloads
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org/downloads [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /downloads
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/downloads
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> https://www.test.org/downloads [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /downloads
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/downloads
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> https://www.test.org/downloads [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /downloads
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/downloads
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### D13 [attachment] https://www.test.org/get/file.PDF
intent: 大写扩展名应同样识别
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /get/file.PDF
    - extension_filter: REJECT non-whitelisted extension on /get/file.PDF
- S: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /get/file.PDF
    - extension_filter: REJECT non-whitelisted extension on /get/file.PDF
- L-en: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /get/file.PDF
    - extension_filter: REJECT non-whitelisted extension on /get/file.PDF
- S-en: DROP@extension_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /get/file.PDF
    - extension_filter: REJECT non-whitelisted extension on /get/file.PDF

### D14 [attachment] https://www.test.org/download?file=report.pdf
intent: 下载页查询里携带 PDF 文件名
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: file: lenient=keep/strict=drop || lenient->'file=report.pdf' strict->''
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/download?file=report.pdf [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /download
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS file=report.pdf
    - rebuild_url: PASS https://www.test.org/download?file=report.pdf
    - blacklist: PASS label=test not blacklisted
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /download
    - extension_filter: PASS
    - type_filter#1: REJECT file/image type marker '.pdf'
- L-en: KEEP -> https://www.test.org/download?file=report.pdf [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /download
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS file=report.pdf
    - rebuild_url: PASS https://www.test.org/download?file=report.pdf
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /download
    - extension_filter: PASS
    - type_filter#1: REJECT file/image type marker '.pdf'

### E1 [i18n_host] https://en.example.org/article
intent: 英文子域主机
attribution: label=example registrable=example.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | host:en=+1
lang_filter(de): lenient=True strict=False | host:en=-1
- L: KEEP -> https://en.example.org/article [example.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=en.example.org
    - domain_filter: PASS host=en.example.org label=example registrable=example.org
    - normalize_path: PASS /article
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://en.example.org/article
    - blacklist: PASS label=example not blacklisted
- S: KEEP -> https://en.example.org/article [example.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=en.example.org
    - domain_filter: PASS host=en.example.org label=example registrable=example.org
    - normalize_path: PASS /article
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://en.example.org/article
    - blacklist: PASS label=example not blacklisted
- L-en: KEEP -> https://en.example.org/article [example.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=en.example.org
    - domain_filter: PASS host=en.example.org label=example registrable=example.org
    - normalize_path: PASS /article
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://en.example.org/article
    - lang_filter: PASS strict=False host:en=+1
    - blacklist: PASS label=example not blacklisted
- S-en: KEEP -> https://en.example.org/article [example.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=en.example.org
    - domain_filter: PASS host=en.example.org label=example registrable=example.org
    - normalize_path: PASS /article
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://en.example.org/article
    - lang_filter: PASS strict=True host:en=+1
    - blacklist: PASS label=example not blacklisted

### E2 [i18n_host] https://de.example.org/article
intent: 德文子域主机
attribution: label=example registrable=example.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=False | host:de=-1
lang_filter(de): lenient=True strict=True | host:de=+1
- L: KEEP -> https://de.example.org/article [example.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=de.example.org
    - domain_filter: PASS host=de.example.org label=example registrable=example.org
    - normalize_path: PASS /article
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://de.example.org/article
    - blacklist: PASS label=example not blacklisted
- S: KEEP -> https://de.example.org/article [example.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=de.example.org
    - domain_filter: PASS host=de.example.org label=example registrable=example.org
    - normalize_path: PASS /article
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://de.example.org/article
    - blacklist: PASS label=example not blacklisted
- L-en: KEEP -> https://de.example.org/article [example.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=de.example.org
    - domain_filter: PASS host=de.example.org label=example registrable=example.org
    - normalize_path: PASS /article
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://de.example.org/article
    - lang_filter: PASS strict=False host:de=-1
    - blacklist: PASS label=example not blacklisted
- S-en: DROP@lang_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=de.example.org
    - domain_filter: PASS host=de.example.org label=example registrable=example.org
    - normalize_path: PASS /article
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://de.example.org/article
    - lang_filter: REJECT strict=True host:de=-1

### E3 [i18n_host] https://fr.example.org/article
intent: 法文子域主机
attribution: label=example registrable=example.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=False | host:fr=-1
lang_filter(de): lenient=True strict=False | host:fr=-1
- L: KEEP -> https://fr.example.org/article [example.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=fr.example.org
    - domain_filter: PASS host=fr.example.org label=example registrable=example.org
    - normalize_path: PASS /article
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://fr.example.org/article
    - blacklist: PASS label=example not blacklisted
- S: KEEP -> https://fr.example.org/article [example.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=fr.example.org
    - domain_filter: PASS host=fr.example.org label=example registrable=example.org
    - normalize_path: PASS /article
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://fr.example.org/article
    - blacklist: PASS label=example not blacklisted
- L-en: KEEP -> https://fr.example.org/article [example.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=fr.example.org
    - domain_filter: PASS host=fr.example.org label=example registrable=example.org
    - normalize_path: PASS /article
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://fr.example.org/article
    - lang_filter: PASS strict=False host:fr=-1
    - blacklist: PASS label=example not blacklisted
- S-en: DROP@lang_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=fr.example.org
    - domain_filter: PASS host=fr.example.org label=example registrable=example.org
    - normalize_path: PASS /article
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://fr.example.org/article
    - lang_filter: REJECT strict=True host:fr=-1

### E4 [i18n_host] https://www.test.org/en/article1.html
intent: 路径 /en/ 语言线索
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | path:en=+1
lang_filter(de): lenient=False strict=False | path:en=-1
- L: KEEP -> https://www.test.org/en/article1.html [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /en/article1.html
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/en/article1.html
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org/en/article1.html [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /en/article1.html
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/en/article1.html
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> https://www.test.org/en/article1.html [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /en/article1.html
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/en/article1.html
    - lang_filter: PASS strict=False path:en=+1
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> https://www.test.org/en/article1.html [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /en/article1.html
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/en/article1.html
    - lang_filter: PASS strict=True path:en=+1
    - blacklist: PASS label=test not blacklisted

### E5 [i18n_host] https://www.test.org/es/article3.html
intent: 路径 /es/ 非目标语言
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=False strict=False | path:es=-1
lang_filter(de): lenient=False strict=False | path:es=-1
- L: KEEP -> https://www.test.org/es/article3.html [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /es/article3.html
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/es/article3.html
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org/es/article3.html [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /es/article3.html
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/es/article3.html
    - blacklist: PASS label=test not blacklisted
- L-en: DROP@lang_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /es/article3.html
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/es/article3.html
    - lang_filter: REJECT strict=False path:es=-1
- S-en: DROP@lang_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /es/article3.html
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/es/article3.html
    - lang_filter: REJECT strict=True path:es=-1

### E6 [i18n_host] https://www.test.org/de/impressum
intent: 非英语路径叠加 impressum
attribution: label=test registrable=test.org | navigation=False not_crawlable=True
lang_filter(en): lenient=False strict=False | path:de=-1
lang_filter(de): lenient=True strict=True | path:de=+1
- L: KEEP -> https://www.test.org/de/impressum [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /de/impressum
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/de/impressum
    - blacklist: PASS label=test not blacklisted
- S: DROP@path_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /de/impressum
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: REJECT not-crawlable (contact/imprint/login)
- L-en: DROP@lang_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /de/impressum
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/de/impressum
    - lang_filter: REJECT strict=False path:de=-1
- S-en: DROP@path_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /de/impressum
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: REJECT not-crawlable (contact/imprint/login)

### E7 [i18n_host] https://www.test.org/en-US/article
intent: 带地区的 en-US 语言段
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | path:en-US=+1
lang_filter(de): lenient=False strict=False | path:en-US=-1
- L: KEEP -> https://www.test.org/en-US/article [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /en-US/article
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/en-US/article
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org/en-US/article [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /en-US/article
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/en-US/article
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> https://www.test.org/en-US/article [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /en-US/article
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/en-US/article
    - lang_filter: PASS strict=False path:en-US=+1
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> https://www.test.org/en-US/article [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /en-US/article
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/en-US/article
    - lang_filter: PASS strict=True path:en-US=+1
    - blacklist: PASS label=test not blacklisted

### E8 [i18n_host] https://www.test.org/en/de/x
intent: 双语言段互相抵消
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | path:en=+1 path:de=-1
lang_filter(de): lenient=True strict=True | path:en=-1 path:de=+1
- L: KEEP -> https://www.test.org/en/de/x [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /en/de/x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/en/de/x
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org/en/de/x [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /en/de/x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/en/de/x
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> https://www.test.org/en/de/x [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /en/de/x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/en/de/x
    - lang_filter: PASS strict=False path:en=+1 path:de=-1
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> https://www.test.org/en/de/x [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /en/de/x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/en/de/x
    - lang_filter: PASS strict=True path:en=+1 path:de=-1
    - blacklist: PASS label=test not blacklisted

### E9 [i18n_host] https://www.test.org/page?lang=fr&id=1
intent: lang 查询参数声明法语
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: lang: lenient=keep/strict=keep; id: lenient=keep/strict=keep || clean_query RAISES (lang param contradicts target 'en')
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/page?id=1&lang=fr [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1&lang=fr
    - rebuild_url: PASS https://www.test.org/page?id=1&lang=fr
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org/page?id=1&lang=fr [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1&lang=fr
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/page?id=1&lang=fr
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- L-en: DROP@clean_query
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: REJECT lang param contradicts target 'en'
- S-en: DROP@clean_query
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: REJECT lang param contradicts target 'en'

### E10 [i18n_host] https://www.test.org/page?language=en&id=1
intent: language 查询参数声明英语
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: language: lenient=keep/strict=keep; id: lenient=keep/strict=keep || lenient->'id=1&language=en' strict->'id=1&language=en'
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/page?id=1&language=en [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1&language=en
    - rebuild_url: PASS https://www.test.org/page?id=1&language=en
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org/page?id=1&language=en [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1&language=en
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/page?id=1&language=en
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> https://www.test.org/page?id=1&language=en [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1&language=en
    - rebuild_url: PASS https://www.test.org/page?id=1&language=en
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> https://www.test.org/page?id=1&language=en [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS id=1&language=en
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/page?id=1&language=en
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### E14a [i18n_host] http://shop.ck/
intent: PSL 通配符后缀，无可注册域
attribution: label=None registrable=None | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@registrable_domain
    - basic_filter: PASS
    - scrub_url: PASS http://shop.ck/ -> http://shop.ck
    - validate_url: PASS netloc=shop.ck
    - domain_filter: PASS host=shop.ck label=None registrable=None
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://shop.ck
    - registrable_domain: REJECT no registrable domain (PSL suffix)
- S: DROP@registrable_domain
    - basic_filter: PASS
    - scrub_url: PASS http://shop.ck/ -> http://shop.ck
    - validate_url: PASS netloc=shop.ck
    - domain_filter: PASS host=shop.ck label=None registrable=None
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://shop.ck
    - registrable_domain: REJECT no registrable domain (PSL suffix)
- L-en: DROP@registrable_domain
    - basic_filter: PASS
    - scrub_url: PASS http://shop.ck/ -> http://shop.ck
    - validate_url: PASS netloc=shop.ck
    - domain_filter: PASS host=shop.ck label=None registrable=None
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://shop.ck
    - lang_filter: PASS strict=False no cue
    - registrable_domain: REJECT no registrable domain (PSL suffix)
- S-en: DROP@registrable_domain
    - basic_filter: PASS
    - scrub_url: PASS http://shop.ck/ -> http://shop.ck
    - validate_url: PASS netloc=shop.ck
    - domain_filter: PASS host=shop.ck label=None registrable=None
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://shop.ck
    - lang_filter: PASS strict=True no cue
    - registrable_domain: REJECT no registrable domain (PSL suffix)

### E14b [i18n_host] http://www.ck/
intent: PSL 例外规则可归属，但被 validate_url 短主机门槛拦截
attribution: label=www registrable=www.ck | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@validate_url
    - basic_filter: PASS
    - scrub_url: PASS http://www.ck/ -> http://www.ck
    - validate_url: REJECT scheme/host plausibility failed
- S: DROP@validate_url
    - basic_filter: PASS
    - scrub_url: PASS http://www.ck/ -> http://www.ck
    - validate_url: REJECT scheme/host plausibility failed
- L-en: DROP@validate_url
    - basic_filter: PASS
    - scrub_url: PASS http://www.ck/ -> http://www.ck
    - validate_url: REJECT scheme/host plausibility failed
- S-en: DROP@validate_url
    - basic_filter: PASS
    - scrub_url: PASS http://www.ck/ -> http://www.ck
    - validate_url: REJECT scheme/host plausibility failed

### E14f [i18n_host] http://city.kawasaki.jp/
intent: PSL 例外规则，可注册域为 city.kawasaki.jp
attribution: label=city registrable=city.kawasaki.jp | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://city.kawasaki.jp [city.kawasaki.jp]
    - basic_filter: PASS
    - scrub_url: PASS http://city.kawasaki.jp/ -> http://city.kawasaki.jp
    - validate_url: PASS netloc=city.kawasaki.jp
    - domain_filter: PASS host=city.kawasaki.jp label=city registrable=city.kawasaki.jp
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://city.kawasaki.jp
    - blacklist: PASS label=city not blacklisted
- S: KEEP -> http://city.kawasaki.jp [city.kawasaki.jp]
    - basic_filter: PASS
    - scrub_url: PASS http://city.kawasaki.jp/ -> http://city.kawasaki.jp
    - validate_url: PASS netloc=city.kawasaki.jp
    - domain_filter: PASS host=city.kawasaki.jp label=city registrable=city.kawasaki.jp
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://city.kawasaki.jp
    - blacklist: PASS label=city not blacklisted
- L-en: KEEP -> http://city.kawasaki.jp [city.kawasaki.jp]
    - basic_filter: PASS
    - scrub_url: PASS http://city.kawasaki.jp/ -> http://city.kawasaki.jp
    - validate_url: PASS netloc=city.kawasaki.jp
    - domain_filter: PASS host=city.kawasaki.jp label=city registrable=city.kawasaki.jp
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://city.kawasaki.jp
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=city not blacklisted
- S-en: KEEP -> http://city.kawasaki.jp [city.kawasaki.jp]
    - basic_filter: PASS
    - scrub_url: PASS http://city.kawasaki.jp/ -> http://city.kawasaki.jp
    - validate_url: PASS netloc=city.kawasaki.jp
    - domain_filter: PASS host=city.kawasaki.jp label=city registrable=city.kawasaki.jp
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://city.kawasaki.jp
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=city not blacklisted

### E14g [i18n_host] https://foo.kobe.jp/
intent: PSL 通配符：foo.kobe.jp 本身即公共后缀，无归属
attribution: label=None registrable=None | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@registrable_domain
    - basic_filter: PASS
    - scrub_url: PASS https://foo.kobe.jp/ -> https://foo.kobe.jp
    - validate_url: PASS netloc=foo.kobe.jp
    - domain_filter: PASS host=foo.kobe.jp label=None registrable=None
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://foo.kobe.jp
    - registrable_domain: REJECT no registrable domain (PSL suffix)
- S: DROP@registrable_domain
    - basic_filter: PASS
    - scrub_url: PASS https://foo.kobe.jp/ -> https://foo.kobe.jp
    - validate_url: PASS netloc=foo.kobe.jp
    - domain_filter: PASS host=foo.kobe.jp label=None registrable=None
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://foo.kobe.jp
    - registrable_domain: REJECT no registrable domain (PSL suffix)
- L-en: DROP@registrable_domain
    - basic_filter: PASS
    - scrub_url: PASS https://foo.kobe.jp/ -> https://foo.kobe.jp
    - validate_url: PASS netloc=foo.kobe.jp
    - domain_filter: PASS host=foo.kobe.jp label=None registrable=None
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://foo.kobe.jp
    - lang_filter: PASS strict=False no cue
    - registrable_domain: REJECT no registrable domain (PSL suffix)
- S-en: DROP@registrable_domain
    - basic_filter: PASS
    - scrub_url: PASS https://foo.kobe.jp/ -> https://foo.kobe.jp
    - validate_url: PASS netloc=foo.kobe.jp
    - domain_filter: PASS host=foo.kobe.jp label=None registrable=None
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://foo.kobe.jp
    - lang_filter: PASS strict=True no cue
    - registrable_domain: REJECT no registrable domain (PSL suffix)

### E14h [i18n_host] http://ck-shop.example.ck/x
intent: 通配符后缀下的正常可注册域
attribution: label=ck-shop registrable=ck-shop.example.ck | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://ck-shop.example.ck/x [ck-shop.example.ck]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=ck-shop.example.ck
    - domain_filter: PASS host=ck-shop.example.ck label=ck-shop registrable=ck-shop.example.ck
    - normalize_path: PASS /x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://ck-shop.example.ck/x
    - blacklist: PASS label=ck-shop not blacklisted
- S: KEEP -> http://ck-shop.example.ck/x [ck-shop.example.ck]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=ck-shop.example.ck
    - domain_filter: PASS host=ck-shop.example.ck label=ck-shop registrable=ck-shop.example.ck
    - normalize_path: PASS /x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://ck-shop.example.ck/x
    - blacklist: PASS label=ck-shop not blacklisted
- L-en: KEEP -> http://ck-shop.example.ck/x [ck-shop.example.ck]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=ck-shop.example.ck
    - domain_filter: PASS host=ck-shop.example.ck label=ck-shop registrable=ck-shop.example.ck
    - normalize_path: PASS /x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://ck-shop.example.ck/x
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=ck-shop not blacklisted
- S-en: KEEP -> http://ck-shop.example.ck/x [ck-shop.example.ck]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=ck-shop.example.ck
    - domain_filter: PASS host=ck-shop.example.ck label=ck-shop registrable=ck-shop.example.ck
    - normalize_path: PASS /x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://ck-shop.example.ck/x
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=ck-shop not blacklisted

### E14c [i18n_host] https://news.bbc.co.uk/
intent: 多级后缀归属判断
attribution: label=bbc registrable=bbc.co.uk | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://news.bbc.co.uk [bbc.co.uk]
    - basic_filter: PASS
    - scrub_url: PASS https://news.bbc.co.uk/ -> https://news.bbc.co.uk
    - validate_url: PASS netloc=news.bbc.co.uk
    - domain_filter: PASS host=news.bbc.co.uk label=bbc registrable=bbc.co.uk
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://news.bbc.co.uk
    - blacklist: PASS label=bbc not blacklisted
- S: KEEP -> https://news.bbc.co.uk [bbc.co.uk]
    - basic_filter: PASS
    - scrub_url: PASS https://news.bbc.co.uk/ -> https://news.bbc.co.uk
    - validate_url: PASS netloc=news.bbc.co.uk
    - domain_filter: PASS host=news.bbc.co.uk label=bbc registrable=bbc.co.uk
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://news.bbc.co.uk
    - blacklist: PASS label=bbc not blacklisted
- L-en: KEEP -> https://news.bbc.co.uk [bbc.co.uk]
    - basic_filter: PASS
    - scrub_url: PASS https://news.bbc.co.uk/ -> https://news.bbc.co.uk
    - validate_url: PASS netloc=news.bbc.co.uk
    - domain_filter: PASS host=news.bbc.co.uk label=bbc registrable=bbc.co.uk
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://news.bbc.co.uk
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=bbc not blacklisted
- S-en: KEEP -> https://news.bbc.co.uk [bbc.co.uk]
    - basic_filter: PASS
    - scrub_url: PASS https://news.bbc.co.uk/ -> https://news.bbc.co.uk
    - validate_url: PASS netloc=news.bbc.co.uk
    - domain_filter: PASS host=news.bbc.co.uk label=bbc registrable=bbc.co.uk
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://news.bbc.co.uk
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=bbc not blacklisted

### E14d [i18n_host] http://127.0.0.1:8080/path
intent: IPv4 字面值主机
attribution: label=127.0.0.1 registrable=127.0.0.1 | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://127.0.0.1:8080/path [127.0.0.1]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=127.0.0.1:8080
    - domain_filter: PASS host=127.0.0.1:8080 label=127.0.0.1 registrable=127.0.0.1
    - normalize_path: PASS /path
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://127.0.0.1:8080/path
    - blacklist: PASS label=127.0.0.1 not blacklisted
- S: KEEP -> http://127.0.0.1:8080/path [127.0.0.1]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=127.0.0.1:8080
    - domain_filter: PASS host=127.0.0.1:8080 label=127.0.0.1 registrable=127.0.0.1
    - normalize_path: PASS /path
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://127.0.0.1:8080/path
    - blacklist: PASS label=127.0.0.1 not blacklisted
- L-en: KEEP -> http://127.0.0.1:8080/path [127.0.0.1]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=127.0.0.1:8080
    - domain_filter: PASS host=127.0.0.1:8080 label=127.0.0.1 registrable=127.0.0.1
    - normalize_path: PASS /path
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://127.0.0.1:8080/path
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=127.0.0.1 not blacklisted
- S-en: KEEP -> http://127.0.0.1:8080/path [127.0.0.1]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=127.0.0.1:8080
    - domain_filter: PASS host=127.0.0.1:8080 label=127.0.0.1 registrable=127.0.0.1
    - normalize_path: PASS /path
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://127.0.0.1:8080/path
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=127.0.0.1 not blacklisted

### E14e [i18n_host] http://[2001:db8::1]/path
intent: IPv6 字面值主机
attribution: label=2001:db8::1 registrable=2001:db8::1 | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> http://[2001:db8::1]/path [2001:db8::1]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=[2001:db8::1]
    - domain_filter: PASS host=[2001:db8::1] label=2001:db8::1 registrable=2001:db8::1
    - normalize_path: PASS /path
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://[2001:db8::1]/path
    - blacklist: PASS label=2001:db8::1 not blacklisted
- S: KEEP -> http://[2001:db8::1]/path [2001:db8::1]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=[2001:db8::1]
    - domain_filter: PASS host=[2001:db8::1] label=2001:db8::1 registrable=2001:db8::1
    - normalize_path: PASS /path
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://[2001:db8::1]/path
    - blacklist: PASS label=2001:db8::1 not blacklisted
- L-en: KEEP -> http://[2001:db8::1]/path [2001:db8::1]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=[2001:db8::1]
    - domain_filter: PASS host=[2001:db8::1] label=2001:db8::1 registrable=2001:db8::1
    - normalize_path: PASS /path
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS http://[2001:db8::1]/path
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=2001:db8::1 not blacklisted
- S-en: KEEP -> http://[2001:db8::1]/path [2001:db8::1]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=[2001:db8::1]
    - domain_filter: PASS host=[2001:db8::1] label=2001:db8::1 registrable=2001:db8::1
    - normalize_path: PASS /path
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS http://[2001:db8::1]/path
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=2001:db8::1 not blacklisted

### E15a [i18n_host] https://www.facebook.com/x
intent: 黑名单社交域
attribution: label=facebook registrable=facebook.com | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.facebook.com/x [facebook.com]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.facebook.com
    - domain_filter: PASS host=www.facebook.com label=facebook registrable=facebook.com
    - normalize_path: PASS /x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.facebook.com/x
    - blacklist: PASS label=facebook not blacklisted
- S: DROP@blacklist
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.facebook.com
    - domain_filter: PASS host=www.facebook.com label=facebook registrable=facebook.com
    - normalize_path: PASS /x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.facebook.com/x
    - blacklist: REJECT label=facebook domain=facebook.com
- L-en: KEEP -> https://www.facebook.com/x [facebook.com]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.facebook.com
    - domain_filter: PASS host=www.facebook.com label=facebook registrable=facebook.com
    - normalize_path: PASS /x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.facebook.com/x
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=facebook not blacklisted
- S-en: DROP@blacklist
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.facebook.com
    - domain_filter: PASS host=www.facebook.com label=facebook registrable=facebook.com
    - normalize_path: PASS /x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.facebook.com/x
    - lang_filter: PASS strict=True no cue
    - blacklist: REJECT label=facebook domain=facebook.com

### E15b [i18n_host] https://mail.google.com/a
intent: 黑名单 Google 子域
attribution: label=google registrable=google.com | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://mail.google.com/a [google.com]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=mail.google.com
    - domain_filter: PASS host=mail.google.com label=google registrable=google.com
    - normalize_path: PASS /a
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://mail.google.com/a
    - blacklist: PASS label=google not blacklisted
- S: DROP@blacklist
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=mail.google.com
    - domain_filter: PASS host=mail.google.com label=google registrable=google.com
    - normalize_path: PASS /a
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://mail.google.com/a
    - blacklist: REJECT label=google domain=google.com
- L-en: KEEP -> https://mail.google.com/a [google.com]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=mail.google.com
    - domain_filter: PASS host=mail.google.com label=google registrable=google.com
    - normalize_path: PASS /a
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://mail.google.com/a
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=google not blacklisted
- S-en: DROP@blacklist
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=mail.google.com
    - domain_filter: PASS host=mail.google.com label=google registrable=google.com
    - normalize_path: PASS /a
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://mail.google.com/a
    - lang_filter: PASS strict=True no cue
    - blacklist: REJECT label=google domain=google.com

### E15c [i18n_host] https://bit.ly/x
intent: 黑名单短链（bit 标签）
attribution: label=bit registrable=bit.ly | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://bit.ly/x [bit.ly]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=bit.ly
    - domain_filter: PASS host=bit.ly label=bit registrable=bit.ly
    - normalize_path: PASS /x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://bit.ly/x
    - blacklist: PASS label=bit not blacklisted
- S: DROP@blacklist
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=bit.ly
    - domain_filter: PASS host=bit.ly label=bit registrable=bit.ly
    - normalize_path: PASS /x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://bit.ly/x
    - blacklist: REJECT label=bit domain=bit.ly
- L-en: KEEP -> https://bit.ly/x [bit.ly]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=bit.ly
    - domain_filter: PASS host=bit.ly label=bit registrable=bit.ly
    - normalize_path: PASS /x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://bit.ly/x
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=bit not blacklisted
- S-en: DROP@blacklist
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=bit.ly
    - domain_filter: PASS host=bit.ly label=bit registrable=bit.ly
    - normalize_path: PASS /x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://bit.ly/x
    - lang_filter: PASS strict=True no cue
    - blacklist: REJECT label=bit domain=bit.ly

### E15d [i18n_host] https://web.example.org/x
intent: 通用词 web 也在黑名单
attribution: label=example registrable=example.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://web.example.org/x [example.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=web.example.org
    - domain_filter: PASS host=web.example.org label=example registrable=example.org
    - normalize_path: PASS /x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://web.example.org/x
    - blacklist: PASS label=example not blacklisted
- S: KEEP -> https://web.example.org/x [example.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=web.example.org
    - domain_filter: PASS host=web.example.org label=example registrable=example.org
    - normalize_path: PASS /x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://web.example.org/x
    - blacklist: PASS label=example not blacklisted
- L-en: KEEP -> https://web.example.org/x [example.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=web.example.org
    - domain_filter: PASS host=web.example.org label=example registrable=example.org
    - normalize_path: PASS /x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://web.example.org/x
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=example not blacklisted
- S-en: KEEP -> https://web.example.org/x [example.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=web.example.org
    - domain_filter: PASS host=web.example.org label=example registrable=example.org
    - normalize_path: PASS /x
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://web.example.org/x
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=example not blacklisted

### F1 [normalization] https://www.test.org/page#section
intent: 普通 fragment 保留差异
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/page#section [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/page#section
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org/page [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/page
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> https://www.test.org/page#section [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/page#section
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> https://www.test.org/page [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/page
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### F2 [normalization] https://www.test.org/page#utm_source=news
intent: fragment 内跟踪参数
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/page [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/page
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org/page [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/page
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> https://www.test.org/page [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/page
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> https://www.test.org/page [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/page
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### F3 [normalization] https://WWW.TEST.ORG:443/path
intent: 大写主机与默认端口
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/path [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=WWW.TEST.ORG:443
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /path
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/path
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org/path [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=WWW.TEST.ORG:443
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /path
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/path
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> https://www.test.org/path [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=WWW.TEST.ORG:443
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /path
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/path
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> https://www.test.org/path [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=WWW.TEST.ORG:443
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /path
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/path
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### F4 [normalization] https://user:pass@www.test.org/path
intent: userinfo 应被剥离
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/path [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=user:pass@www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /path
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/path
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org/path [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=user:pass@www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /path
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/path
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> https://www.test.org/path [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=user:pass@www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /path
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/path
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> https://www.test.org/path [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=user:pass@www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /path
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/path
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### F5 [normalization] https://www.test.org/a//b///c
intent: 重复斜杠折叠
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/a/b/c [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /a//b///c -> /a/b/c
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/a/b/c
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org/a/b/c [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /a//b///c -> /a/b/c
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/a/b/c
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> https://www.test.org/a/b/c [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /a//b///c -> /a/b/c
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/a/b/c
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> https://www.test.org/a/b/c [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /a//b///c -> /a/b/c
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/a/b/c
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### F6a [normalization] https://www.test.org/
intent: 根路径尾斜杠规范
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org [test.org]
    - basic_filter: PASS
    - scrub_url: PASS https://www.test.org/ -> https://www.test.org
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org [test.org]
    - basic_filter: PASS
    - scrub_url: PASS https://www.test.org/ -> https://www.test.org
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> https://www.test.org [test.org]
    - basic_filter: PASS
    - scrub_url: PASS https://www.test.org/ -> https://www.test.org
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> https://www.test.org [test.org]
    - basic_filter: PASS
    - scrub_url: PASS https://www.test.org/ -> https://www.test.org
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### F6b [normalization] https://www.test.org
intent: 无尾斜杠根主机
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> https://www.test.org [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> https://www.test.org [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS (empty)
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### F7 [normalization] https://www.test.org/livecams/stream
intent: 成人/直播关键词
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/livecams/stream [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /livecams/stream
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/livecams/stream
    - blacklist: PASS label=test not blacklisted
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /livecams/stream
    - extension_filter: PASS
    - type_filter#1: REJECT adult/video keyword
- L-en: KEEP -> https://www.test.org/livecams/stream [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /livecams/stream
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/livecams/stream
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /livecams/stream
    - extension_filter: PASS
    - type_filter#1: REJECT adult/video keyword

### F8 [normalization] https://www.test.org/feed
intent: feed 路径
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /feed
    - extension_filter: PASS
    - type_filter#1: REJECT feed/rss pattern
- S: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /feed
    - extension_filter: PASS
    - type_filter#1: REJECT feed/rss pattern
- L-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /feed
    - extension_filter: PASS
    - type_filter#1: REJECT feed/rss pattern
- S-en: DROP@type_filter#1
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /feed
    - extension_filter: PASS
    - type_filter#1: REJECT feed/rss pattern

### F9 [normalization] https://www.test.org/page?x=porno
intent: 成人关键词藏在查询串
attribution: label=test registrable=test.org | navigation=False not_crawlable=False
query audit: x: lenient=keep/strict=drop || lenient->'x=porno' strict->''
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/page?x=porno [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS x=porno
    - rebuild_url: PASS https://www.test.org/page?x=porno
    - blacklist: PASS label=test not blacklisted
- S: KEEP -> https://www.test.org/page [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/page
    - type_filter#2: PASS rebuilt URL re-checked
    - blacklist: PASS label=test not blacklisted
- L-en: KEEP -> https://www.test.org/page?x=porno [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS x=porno
    - rebuild_url: PASS https://www.test.org/page?x=porno
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: KEEP -> https://www.test.org/page [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /page
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: PASS
    - rebuild_url: PASS https://www.test.org/page
    - type_filter#2: PASS rebuilt URL re-checked
    - lang_filter: PASS strict=True no cue
    - blacklist: PASS label=test not blacklisted

### F10 [normalization] https://www.test.org/kontakt/
intent: 德语联系页，严格 path_filter 拒绝
attribution: label=test registrable=test.org | navigation=False not_crawlable=True
lang_filter(en): lenient=True strict=True | no cue
lang_filter(de): lenient=True strict=True | no cue
- L: KEEP -> https://www.test.org/kontakt/ [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /kontakt/
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/kontakt/
    - blacklist: PASS label=test not blacklisted
- S: DROP@path_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /kontakt/
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: REJECT not-crawlable (contact/imprint/login)
- L-en: KEEP -> https://www.test.org/kontakt/ [test.org]
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /kontakt/
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - rebuild_url: PASS https://www.test.org/kontakt/
    - lang_filter: PASS strict=False no cue
    - blacklist: PASS label=test not blacklisted
- S-en: DROP@path_filter
    - basic_filter: PASS
    - scrub_url: PASS no change
    - validate_url: PASS netloc=www.test.org
    - domain_filter: PASS host=www.test.org label=test registrable=test.org
    - normalize_path: PASS /kontakt/
    - extension_filter: PASS
    - type_filter#1: PASS
    - clean_query: PASS (empty)
    - path_filter: REJECT not-crawlable (contact/imprint/login)
