"""
Call-level policies controlling which evidence channels may provide a date.

A policy is attached to a single :func:`htmldate.find_date` call through the
``source_policy`` parameter. It never mutates module-level state, so different
policies can be used interleaved without interfering with each other or with
the default configuration.
"""

import json

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class Source(str, Enum):
    "Evidence channels a date can originate from."

    STRUCTURED = "structured"
    META = "meta"
    TIME = "time"
    CONTENT = "content"
    URL = "url"


# Accepted alternative spellings in user-provided mappings / JSON.
_SOURCE_ALIASES: dict[str, Source] = {
    "structured": Source.STRUCTURED,
    "structured-data": Source.STRUCTURED,
    "structured_data": Source.STRUCTURED,
    "json-ld": Source.STRUCTURED,
    "jsonld": Source.STRUCTURED,
    "json": Source.STRUCTURED,
    "meta": Source.META,
    "metadata": Source.META,
    "header": Source.META,
    "headers": Source.META,
    "time": Source.TIME,
    "time-element": Source.TIME,
    "content": Source.CONTENT,
    "body": Source.CONTENT,
    "text": Source.CONTENT,
    "url": Source.URL,
    "uri": Source.URL,
}

_ALL_SOURCES = frozenset(Source)


def _coerce_source(value: str | Source) -> Source:
    if isinstance(value, Source):
        return value
    key = str(value).strip().lower()
    try:
        return _SOURCE_ALIASES.get(key) or Source(key)
    except ValueError as err:
        valid = ", ".join(sorted(s.value for s in Source))
        raise ValueError(f"unknown date source {value!r}; valid sources: {valid}") from err


def _coerce_set(value: Any) -> frozenset[Source]:
    if value is None:
        return frozenset()
    if isinstance(value, (str, Source)):
        value = [value]
    try:
        return frozenset(_coerce_source(item) for item in value)
    except TypeError as err:
        raise TypeError(f"source list expected, got {type(value).__name__}") from err


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    """
    Per-call policy for the five evidence channels.

    :param allow: channels that may be used at all (default: all of them)
    :param disable: permitted channels that must not provide a date
    :param require: channels that must all provide a date before a result is
        returned, enabling cross-channel corroboration
    """

    allow: frozenset[Source] = _ALL_SOURCES
    disable: frozenset[Source] = frozenset()
    require: frozenset[Source] = frozenset()

    def __post_init__(self) -> None:
        # accept plain sets/lists/tuples and normalize to frozensets
        for name in ("allow", "disable", "require"):
            value = getattr(self, name)
            if not isinstance(value, frozenset):
                if isinstance(value, (set, list, tuple)):
                    object.__setattr__(
                        self, name, frozenset(_coerce_source(item) for item in value)
                    )
                else:
                    raise TypeError(f"'{name}' must be a set of source values")
        if not all(isinstance(item, Source) for item in self.allow):
            raise TypeError("'allow' must contain Source values")
        if not self.allow:
            raise ValueError("'allow' must contain at least one source")
        unknown = (self.disable | self.require) - self.allow
        if unknown:
            names = ", ".join(sorted(s.value for s in unknown))
            raise ValueError(f"disabled or required sources must also be allowed: {names}")
        overlap = self.disable & self.require
        if overlap:
            names = ", ".join(sorted(s.value for s in overlap))
            raise ValueError(f"sources cannot be both disabled and required: {names}")

    @classmethod
    def from_mapping(
        cls,
        config: Mapping[str, Any] | None,
        base: "SourcePolicy | None" = None,
    ) -> "SourcePolicy":
        """Build a policy from a mapping with ``allow``/``disable``/``require``
        keys, layering the changes on top of ``base`` (default: open policy)."""
        if base is None:
            base = cls()
        elif not isinstance(base, SourcePolicy):
            raise TypeError("'base' must be a SourcePolicy instance")
        if config is None:
            return base
        if not isinstance(config, Mapping):
            raise TypeError("policy must be a mapping of source lists")
        allow = base.allow
        disable = base.disable
        require = base.require
        if config.get("allow") is not None:
            allow = _coerce_set(config["allow"])
        if config.get("disable") is not None:
            disable = (disable | _coerce_set(config["disable"])) & allow
            require = require - disable
        if config.get("require") is not None:
            require = (require | _coerce_set(config["require"])) & allow
            disable = disable - require
        return cls(allow=allow, disable=disable, require=require)

    @classmethod
    def from_json(
        cls,
        text: str,
        base: "SourcePolicy | None" = None,
    ) -> "SourcePolicy":
        "Build a policy from a JSON document of source lists."
        try:
            data = json.loads(text)
        except json.JSONDecodeError as err:
            raise ValueError(f"invalid source policy JSON: {err}") from err
        return cls.from_mapping(data, base)

    def permits(self, source: Source) -> bool:
        "Return whether the channel is allowed and not disabled."
        return source in self.allow and source not in self.disable

    def block_reason(self, source: Source) -> str:
        "Explain why a non-permitted channel was excluded."
        if source not in self.allow:
            return "not in policy allow list"
        return "disabled by policy"


@dataclass(slots=True)
class SourceEvidence:
    "Bookkeeping of what each channel contributed during one extraction call."

    selected: dict[Source, str] = field(default_factory=dict)
    considered: dict[Source, list[str]] = field(default_factory=dict)
    excluded: dict[Source, str] = field(default_factory=dict)

    def record(self, source: Source, value: str | None) -> None:
        "Register a value a permitted channel produced, whether used or not."
        if value is None:
            return
        bucket = self.considered.setdefault(source, [])
        if value not in bucket:
            bucket.append(value)

    def adopt(self, source: Source, value: str) -> None:
        "Mark the value as the date contributed by the channel."
        self.record(source, value)
        self.selected.setdefault(source, value)

    def note_excluded(self, source: Source, reason: str) -> None:
        "Record that a channel was skipped because of the policy."
        self.excluded.setdefault(source, reason)

    def explanation(
        self,
        policy: SourcePolicy,
        result: str | None,
        disagreement: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Render the audit trail: adopted channels, considered but unused
        values, excluded channels and unsatisfied requirements."""
        missing_required = sorted(
            source.value
            for source in policy.require
            if source not in self.selected
        )
        blocked = {
            source: policy.block_reason(source)
            for source in sorted(_ALL_SOURCES, key=lambda item: item.value)
            if not policy.permits(source)
        }
        blocked.update((source, reason) for source, reason in self.excluded.items())
        return {
            "result": result,
            "used": [
                {
                    "source": source.value,
                    "date": value,
                    "required": source in policy.require,
                }
                for source, value in self.selected.items()
            ],
            "considered": {
                source.value: values
                for source, values in self.considered.items()
            },
            "excluded": {
                source.value: reason
                for source, reason in blocked.items()
            },
            "missing_required": missing_required,
            "disagreement": disagreement or [],
            "policy": {
                "allow": sorted(source.value for source in policy.allow),
                "disable": sorted(source.value for source in policy.disable),
                "require": sorted(source.value for source in policy.require),
            },
        }
