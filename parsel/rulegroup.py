r"""Extraction rule groups for pages carrying multiple template generations.

During a site relaunch the same field is often configured with several
candidate selectors (e.g. one per template generation).  Picking the first
selector that matches anything is fragile: hidden nodes of the old template
frequently match and get mistaken for real content.

This module lets you declare, per candidate selector, how many results are
acceptable and which simple value checks a result must pass, and then combine
the candidates in a group that either

* takes the first valid candidate (``mode="first"``), or
* requires all valid candidates to agree (``mode="consensus"``).

Failures are reported as explicit statuses
(:attr:`GroupStatus.ALL_FAILED`, :attr:`GroupStatus.PARTIAL`,
:attr:`GroupStatus.CONFLICT`) and are never collapsed into an empty string.

All validation happens on the parsed document; no page scripts are executed.

>>> from parsel.rulegroup import ExtractionRule, RuleGroup
>>> html = (
...     '<span class="price" style="display:none">N/A</span>'
...     '<span class="product-price">€19.99</span>'
... )
>>> group = RuleGroup(
...     [
...         ExtractionRule("span.price::text", pattern=r"€\d"),
...         ExtractionRule("span.product-price::text", pattern=r"€\d"),
...     ],
...     mode="first",
... )
>>> result = group.extract(html)
>>> result.status.name
'OK'
>>> result.value
'€19.99'
>>> result.candidates[0].status.name
'INVALID_VALUE'
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from .selector import Selector

if TYPE_CHECKING:
    from collections.abc import Sequence


class ExtractionError(Exception):
    """Raised when a group result has no usable value."""


class CandidateStatus(enum.Enum):
    """Outcome of evaluating a single candidate rule."""

    OK = "ok"
    NO_MATCH = "no_match"
    BAD_COUNT = "bad_count"
    INVALID_VALUE = "invalid_value"


class GroupStatus(enum.Enum):
    """Outcome of evaluating a whole rule group."""

    OK = "ok"
    ALL_FAILED = "all_failed"
    PARTIAL = "partial"
    CONFLICT = "conflict"


GroupMode = Literal["first", "consensus"]


@dataclass(frozen=True)
class ExtractionRule:
    """One candidate selector plus its acceptance criteria.

    :param query: CSS or XPath expression for the candidate.
    :param type: ``"css"`` (default) or ``"xpath"``.
    :param min_results: Minimum number of matched values allowed.
    :param max_results: Maximum number of matched values allowed
        (``None`` means unbounded).
    :param allow_empty: Whether empty/whitespace-only values are acceptable.
    :param pattern: Optional regular expression that every value must match
        (via :func:`re.search`).
    :param min_length: Optional minimum length for every value.
    :param max_length: Optional maximum length for every value.
    :param name: Optional human-readable label used in explanations.
    """

    query: str
    type: Literal["css", "xpath"] = "css"
    min_results: int = 1
    max_results: int | None = 1
    allow_empty: bool = False
    pattern: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        if self.type not in ("css", "xpath"):
            msg = f"unsupported selector type: {self.type!r}"
            raise ValueError(msg)
        if self.min_results < 0:
            msg = "min_results must be >= 0"
            raise ValueError(msg)
        if self.max_results is not None and self.max_results < self.min_results:
            msg = "max_results must be >= min_results"
            raise ValueError(msg)
        if self.pattern is not None:
            re.compile(self.pattern)

    @property
    def label(self) -> str:
        """Human-readable name of the rule."""
        return self.name or self.query


@dataclass
class CandidateResult:
    """Result of applying one :class:`ExtractionRule` to a document."""

    rule: ExtractionRule
    values: list[str]
    status: CandidateStatus
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether this candidate produced an acceptable value."""
        return self.status is CandidateStatus.OK


@dataclass
class GroupResult:
    """Aggregated outcome of a :class:`RuleGroup` extraction.

    ``candidates`` keeps the per-rule results so callers can inspect why
    each candidate was selected or rejected.
    """

    status: GroupStatus
    candidates: list[CandidateResult]
    mode: GroupMode = "first"

    @property
    def ok(self) -> bool:
        """Whether the group produced a value (fully or partially)."""
        return self.status in (GroupStatus.OK, GroupStatus.PARTIAL)

    @property
    def values(self) -> list[str]:
        """Values of the winning candidate(s).

        Raises :class:`ExtractionError` when the group failed or the valid
        candidates contradict each other, instead of returning ``""``.
        """
        if self.status is GroupStatus.ALL_FAILED:
            msg = "all candidates failed: " + "; ".join(
                f"{c.rule.label}: {c.status.value}" for c in self.candidates
            )
            raise ExtractionError(msg)
        if self.status is GroupStatus.CONFLICT:
            msg = "valid candidates disagree: " + "; ".join(
                f"{c.rule.label}={c.values!r}"
                for c in self.candidates
                if c.ok
            )
            raise ExtractionError(msg)
        for candidate in self.candidates:
            if candidate.ok:
                return candidate.values
        msg = "no valid candidate"  # pragma: no cover
        raise ExtractionError(msg)  # pragma: no cover

    @property
    def value(self) -> str:
        """Single extracted value.

        Raises :class:`ExtractionError` if the group has no value or the
        winning candidate produced more than one value.
        """
        values = self.values
        if len(values) != 1:
            msg = f"expected exactly one value, got {len(values)}"
            raise ExtractionError(msg)
        return values[0]

    def explain(self) -> str:
        """Human-readable report of why candidates were accepted/rejected."""
        lines = [f"group status: {self.status.value} (mode={self.mode})"]
        for candidate in self.candidates:
            line = f"- {candidate.rule.label}: {candidate.status.value}"
            if candidate.values:
                line += f" values={candidate.values!r}"
            if candidate.reasons:
                line += " (" + "; ".join(candidate.reasons) + ")"
            lines.append(line)
        return "\n".join(lines)


class RuleGroup:
    """A set of candidate :class:`ExtractionRule` objects for one field.

    :param rules: Candidate rules, in priority order.
    :param mode: ``"first"`` to adopt the first valid candidate, or
        ``"consensus"`` to require all valid candidates to agree.
    """

    def __init__(
        self, rules: Sequence[ExtractionRule], mode: GroupMode = "first"
    ) -> None:
        if not rules:
            msg = "a rule group needs at least one rule"
            raise ValueError(msg)
        if mode not in ("first", "consensus"):
            msg = f"unsupported mode: {mode!r}"
            raise ValueError(msg)
        self.rules = list(rules)
        self.mode: GroupMode = mode

    def extract(
        self, text: str | Selector, type: str | None = None  # noqa: A002
    ) -> GroupResult:
        """Run all candidate rules against ``text`` or a ``Selector``.

        Parsing is done with parsel's regular lxml-based parsers; no page
        scripts are executed at any point.
        """
        selector = (
            text
            if isinstance(text, Selector)
            else Selector(text=text, type=type or "html")
        )
        candidates = [self._apply(rule, selector) for rule in self.rules]
        if self.mode == "first":
            return self._resolve_first(candidates)
        return self._resolve_consensus(candidates)

    def _apply(self, rule: ExtractionRule, selector: Selector) -> CandidateResult:
        if rule.type == "css":
            raw = selector.css(rule.query).getall()
        else:
            raw = selector.xpath(rule.query).getall()
        values = [v.strip() if isinstance(v, str) else str(v) for v in raw]

        if not values:
            return CandidateResult(
                rule, values, CandidateStatus.NO_MATCH, ["selector matched nothing"]
            )

        reasons: list[str] = []
        if len(values) < rule.min_results:
            reasons.append(
                f"got {len(values)} result(s), need at least {rule.min_results}"
            )
        if rule.max_results is not None and len(values) > rule.max_results:
            reasons.append(
                f"got {len(values)} result(s), allow at most {rule.max_results}"
            )
        if reasons:
            return CandidateResult(rule, values, CandidateStatus.BAD_COUNT, reasons)

        for value in values:
            if not rule.allow_empty and not value:
                reasons.append("empty value not allowed")
            if rule.pattern is not None and not re.search(rule.pattern, value):
                reasons.append(f"value {value!r} does not match /{rule.pattern}/")
            if rule.min_length is not None and len(value) < rule.min_length:
                reasons.append(
                    f"value {value!r} shorter than {rule.min_length} chars"
                )
            if rule.max_length is not None and len(value) > rule.max_length:
                reasons.append(
                    f"value {value!r} longer than {rule.max_length} chars"
                )
        if reasons:
            return CandidateResult(
                rule, values, CandidateStatus.INVALID_VALUE, reasons
            )
        return CandidateResult(rule, values, CandidateStatus.OK)

    def _resolve_first(self, candidates: list[CandidateResult]) -> GroupResult:
        status = (
            GroupStatus.OK
            if any(c.ok for c in candidates)
            else GroupStatus.ALL_FAILED
        )
        return GroupResult(status, candidates, mode=self.mode)

    def _resolve_consensus(self, candidates: list[CandidateResult]) -> GroupResult:
        valid = [c for c in candidates if c.ok]
        if not valid:
            return GroupResult(GroupStatus.ALL_FAILED, candidates, mode=self.mode)
        agreed = {tuple(c.values) for c in valid}
        if len(agreed) > 1:
            return GroupResult(GroupStatus.CONFLICT, candidates, mode=self.mode)
        status = (
            GroupStatus.OK if len(valid) == len(candidates) else GroupStatus.PARTIAL
        )
        return GroupResult(status, candidates, mode=self.mode)


__all__ = [
    "CandidateResult",
    "CandidateStatus",
    "ExtractionError",
    "ExtractionRule",
    "GroupResult",
    "GroupStatus",
    "RuleGroup",
]
