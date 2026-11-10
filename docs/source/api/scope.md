# courlan.scope

Reusable crawl scope rules: decide whether a URL belongs to the site sections
a crawl is allowed to visit, without string `startswith` on raw URLs.

```python
from courlan import Scope, ScopeRule

scope = Scope([
    ScopeRule("example.org", path="/news", exclude=("/news/archive",)),
    ScopeRule("blog.example.org"),
])

match = scope.match("https://example.org./news/politics?utm_source=x")
if match:
    print(match.rule)  # the rule that matched
    print(match.url)   # https://example.org/news/politics?utm_source=x
```

Hosts are compared in canonical form (lowercase, punycode, no trailing dot,
canonical IP literals) and paths after percent-decoding, so IDN names, FQDN
trailing dots, IPv6 spellings and encoded slashes all yield consistent
decisions. Query strings and fragments never change which rule a path belongs
to. Invalid rules raise `ScopeRuleError` at construction, before any crawling
starts.

```{eval-rst}
.. automodule:: courlan.scope
   :members:
   :undoc-members:
   :show-inheritance:
```
