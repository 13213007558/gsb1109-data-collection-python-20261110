# courlan.revisit

Configurable revisit policy for crawl outcomes.

A 404, a temporary timeout and repeated 429 responses do not share one
"unvisited" state: each observation is turned into either the earliest time
the address may be fetched again or a reason for which crawling it is paused.
The module only computes points in time — it never sleeps and reads no clock —
so every decision is deterministic and testable.

For usage examples and a timeline walkthrough, see the
[Revisit policy guide](../usage/revisit.md).

```{eval-rst}
.. automodule:: courlan.revisit
   :members:
   :undoc-members:
   :show-inheritance:
```
