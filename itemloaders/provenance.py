"""
Optional provenance (source) tracking for Item Loaders.

This module answers questions like "where did this final, cleaned field value
come from?" without changing anything about how regular loading works:

- :class:`TrackedItemLoader` is an opt-in subclass of
  :class:`~itemloaders.ItemLoader`. Entry points (``add_value``, ``add_css``,
  ``add_xpath``, ``add_jmes`` and their ``replace_*`` counterparts) accept
  optional ``page``, ``selector`` and ``raw`` identifiers that are attached to
  the extracted data as :class:`SourceRef` objects.
- Provenance travels with the data through expansion (``re=`` extraction,
  one-to-many functions in :class:`~itemloaders.processors.MapCompose`),
  filtering (functions returning ``None``) and merging
  (:class:`~itemloaders.processors.Join`, output processors), so
  :meth:`TrackedItemLoader.get_field_sources` can report which raw inputs the
  final value of a field depends on.
- Every processor is still executed exactly once; provenance is propagated
  alongside the real computation, never by re-running processors.
- One-to-many transformations are tracked per element, following the actual
  data flow. They are never matched back to their inputs by position (index).
- Legacy processors that cannot describe their input→output mapping are
  handled conservatively: their outputs are linked to *all* their inputs and
  flagged with :data:`UNKNOWN_RANGE`, instead of inventing a precise mapping.

Custom processors can provide precise provenance by implementing::

    def apply_tracked(self, tracked, loader_context=None):
        ...

which receives a :class:`TrackedValue` or a list of them and must return the
tracked counterpart of whatever ``__call__`` would return.

Plain :class:`~itemloaders.ItemLoader` is completely unaffected and pays no
tracking overhead.
"""

from __future__ import annotations

from collections import ChainMap
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from parsel.utils import extract_regex

from itemloaders import ItemLoader
from itemloaders.common import wrap_loader_context
from itemloaders.processors import Compose, Identity, Join, MapCompose, TakeFirst
from itemloaders.utils import arg_to_iter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, MutableMapping
    from re import Pattern


class _UnknownRange:
    """Singleton type for the :data:`UNKNOWN_RANGE` marker."""

    def __repr__(self) -> str:
        return "UNKNOWN_RANGE"


#: Marker present in the sources of a value when the exact inputs it depends
#: on are unknown (e.g. it went through a legacy processor that cannot
#: describe its input→output mapping). The other sources in the set are the
#: candidate inputs; which of them actually contributed is unknown.
UNKNOWN_RANGE = _UnknownRange()


@dataclass(frozen=True)
class SourceRef:
    """Identifies one piece of raw input data.

    :param page: identifier of the page (or document) the data comes from.
    :param selector: the CSS/XPath/JMESPath expression used to extract it.
    :param raw: identifier of the raw value, usually the raw value itself.
    """

    page: str | None = None
    selector: Any = None
    raw: Any = None

    def __post_init__(self) -> None:
        try:
            hash(self.raw)
        except TypeError:
            object.__setattr__(self, "raw", repr(self.raw))


class TrackedValue:
    """A value bundled with the set of sources it depends on."""

    __slots__ = ("sources", "value")

    def __init__(self, value: Any, sources: Iterable[Any] = ()) -> None:
        self.value = value
        self.sources = frozenset(sources)

    def __repr__(self) -> str:
        return f"TrackedValue({self.value!r}, sources={self.sources!r})"


def _elements(tracked: Any) -> list[TrackedValue]:
    if tracked is None:
        return []
    if isinstance(tracked, TrackedValue):
        return [tracked]
    return list(tracked)


def _union_sources(elements: Iterable[TrackedValue]) -> frozenset:
    sources: set = set()
    for element in elements:
        sources.update(element.sources)
    return frozenset(sources)


def _unwrap(tracked: Any) -> Any:
    if tracked is None:
        return None
    if isinstance(tracked, TrackedValue):
        return tracked.value
    return [element.value for element in tracked]


def _wrap_result(result: Any, sources: frozenset) -> Any:
    if result is None:
        return None
    if isinstance(result, (list, tuple)):
        return [TrackedValue(value, sources) for value in result]
    return TrackedValue(result, sources)


def apply_processor_tracked(
    proc: Callable[..., Any],
    tracked: Any,
    context: MutableMapping[str, Any] | None = None,
) -> Any:
    """Apply *proc* to tracked value(s) exactly once, propagating provenance.

    *tracked* is a :class:`TrackedValue` or a list of them; the return value
    mirrors the shape of what *proc* itself would return. Processors with a
    known input→output mapping (the built-in ones, or any processor defining
    ``apply_tracked``) get precise provenance; anything else is treated as a
    legacy processor and its outputs are flagged with :data:`UNKNOWN_RANGE`.
    """
    apply_tracked = getattr(proc, "apply_tracked", None)
    if apply_tracked is not None:
        return apply_tracked(tracked, loader_context=context)
    if isinstance(proc, Identity):
        return tracked
    if isinstance(proc, TakeFirst):
        for element in _elements(tracked):
            if element.value is not None and element.value != "":
                return element
        return None
    if isinstance(proc, Join):
        elements = _elements(tracked)
        return TrackedValue(
            proc.separator.join(element.value for element in elements),
            _union_sources(elements),
        )
    if isinstance(proc, MapCompose):
        return _apply_map_compose(proc, tracked, context)
    if isinstance(proc, Compose):
        return _apply_compose(proc, tracked, context)
    wrapped = wrap_loader_context(proc, context if context is not None else {})
    result = wrapped(_unwrap(tracked))
    sources = _union_sources(_elements(tracked)) | {UNKNOWN_RANGE}
    return _wrap_result(result, sources)


def _apply_map_compose(
    proc: MapCompose,
    tracked: Any,
    context: MutableMapping[str, Any] | None,
) -> list[TrackedValue]:
    elements = _elements(tracked)
    if context:
        ctx: MutableMapping[str, Any] = ChainMap(context, proc.default_loader_context)
    else:
        ctx = proc.default_loader_context
    wrapped_funcs = [wrap_loader_context(func, ctx) for func in proc.functions]
    for func in wrapped_funcs:
        next_elements: list[TrackedValue] = []
        for element in elements:
            try:
                result = func(element.value)
            except Exception as e:
                raise ValueError(
                    f"Error in MapCompose with {func!s} "
                    f"value={_unwrap(tracked)!r} error='{type(e).__name__}: {e!s}'"
                ) from e
            # Each output of this element inherits the element's own sources,
            # whether the function filtered it out (None), kept it 1:1 or
            # expanded it 1:N. Never matched by index.
            next_elements += [
                TrackedValue(value, element.sources) for value in arg_to_iter(result)
            ]
        elements = next_elements
    return elements


def _apply_compose(
    proc: Compose,
    tracked: Any,
    context: MutableMapping[str, Any] | None,
) -> Any:
    if context:
        ctx: MutableMapping[str, Any] = ChainMap(context, proc.default_loader_context)
    else:
        ctx = proc.default_loader_context
    value = tracked
    for func in proc.functions:
        if value is None and proc.stop_on_none:
            break
        try:
            value = apply_processor_tracked(func, value, ctx)
        except Exception as e:
            raise ValueError(
                f"Error in Compose with {func!s} "
                f"value={_unwrap(value)!r} error='{type(e).__name__}: {e!s}'"
            ) from e
    return value


def _selector_id(rule: Any) -> Any:
    return rule if isinstance(rule, str) else tuple(rule)


_DEFAULT = object()


class TrackedItemLoader(ItemLoader):
    """An :class:`~itemloaders.ItemLoader` that tracks value provenance.

    Behaves exactly like a regular loader, but every collected value carries
    the sources it came from, so :meth:`get_field_sources` can report which
    raw inputs the final value of a field depends on.

    :param page: identifier of the page being parsed, attached by default to
        the :class:`SourceRef` of every value collected by this loader.

    Entry points accept extra keyword-only arguments ``page``, ``selector``
    and ``raw`` to describe the origin of the added data. ``add_css``,
    ``add_xpath`` and ``add_jmes`` fill in ``selector`` automatically, and
    ``raw`` defaults to the added value itself.
    """

    def __init__(
        self,
        item: Any = None,
        selector: Any = None,
        parent: ItemLoader | None = None,
        stats: Any = None,
        page: str | None = None,
        **context: Any,
    ):
        super().__init__(
            item=item, selector=selector, parent=parent, stats=stats, **context
        )
        self.page = page
        if parent is None:
            # Pre-existing item data has no known origin.
            for field_name, values in self._local_values.items():
                self._local_values[field_name] = [
                    value
                    if isinstance(value, TrackedValue)
                    else TrackedValue(value, {UNKNOWN_RANGE})
                    for value in values
                ]

    def _get_nested_context(self, context: dict[str, Any]) -> dict[str, Any]:
        context = super()._get_nested_context(context)
        context.setdefault("page", self.page)
        return context

    def _wrap_entry(self, value: Any, page: Any, selector: Any, raw: Any) -> Any:
        if page is None:
            page = self.page

        def wrap(v: Any) -> TrackedValue:
            if isinstance(v, TrackedValue):
                return v
            r = v if raw is _DEFAULT else raw
            if page is None and selector is None and r is None:
                return TrackedValue(v)
            return TrackedValue(v, {SourceRef(page=page, selector=selector, raw=r)})

        if isinstance(value, (list, tuple)):
            return [wrap(v) for v in value]
        return wrap(value)

    def _run_processors(
        self,
        tracked: Any,
        processors: tuple[Callable[..., Any], ...],
        re: str | Pattern[str] | None = None,
    ) -> Any:
        if re:
            # re extraction can expand one value into many matches; every
            # match keeps the sources of the value it was extracted from.
            tracked = [
                TrackedValue(match, element.sources)
                for element in _elements(tracked)
                for match in extract_regex(re, element.value)
            ]
        for proc in processors:
            if tracked is None:
                break
            try:
                tracked = apply_processor_tracked(proc, tracked, self.context)
            except Exception as e:
                raise ValueError(
                    f"Error with processor {proc.__class__.__name__} "
                    f"value={_unwrap(tracked)!r} error='{type(e).__name__}: {e!s}'"
                ) from e
        return tracked

    def _add_tracked(self, field_name: str, tracked: Any) -> None:
        elements = _elements(tracked)
        proc = self.get_input_processor(field_name)
        try:
            result = apply_processor_tracked(proc, elements, self.context)
        except Exception as e:
            raise ValueError(
                f"Error with input processor {proc.__class__.__name__}:"
                f" field={field_name!r} value={_unwrap(elements)!r}"
                f" error='{type(e).__name__}: {e!s}'"
            ) from e
        if result is None:
            return
        stored = _elements(result)
        if not stored or not _unwrap(result):
            return
        self._values.setdefault(field_name, [])
        self._values[field_name] += stored

    def add_value(
        self,
        field_name: str | None,
        value: Any,
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        page: str | None = None,
        selector: Any = None,
        raw: Any = _DEFAULT,
        **kw: Any,
    ) -> TrackedItemLoader:
        tracked = self._wrap_entry(value, page, selector, raw)
        tracked = self._run_processors(tracked, processors, re=re)
        if tracked is None:
            return self
        if not field_name:
            sources = tracked.sources if isinstance(tracked, TrackedValue) else ()
            for k, v in _unwrap(tracked).items():
                self._add_tracked(
                    k, v if isinstance(v, TrackedValue) else TrackedValue(v, sources)
                )
        else:
            self._add_tracked(field_name, tracked)
        return self

    def replace_value(
        self,
        field_name: str | None,
        value: Any,
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        page: str | None = None,
        selector: Any = None,
        raw: Any = _DEFAULT,
        **kw: Any,
    ) -> TrackedItemLoader:
        tracked = self._wrap_entry(value, page, selector, raw)
        tracked = self._run_processors(tracked, processors, re=re)
        if tracked is None:
            return self
        if not field_name:
            sources = tracked.sources if isinstance(tracked, TrackedValue) else ()
            for k, v in _unwrap(tracked).items():
                self._values.pop(k, None)
                self._add_tracked(
                    k, v if isinstance(v, TrackedValue) else TrackedValue(v, sources)
                )
        else:
            self._values.pop(field_name, None)
            self._add_tracked(field_name, tracked)
        return self

    def get_value(
        self,
        value: Any,
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        **kw: Any,
    ) -> Any:
        tracked = self._wrap_entry(value, page=None, selector=None, raw=None)
        tracked = self._run_processors(tracked, processors, re=re)
        return _unwrap(tracked)

    def get_output_value(self, field_name: str) -> Any:
        proc = self.get_output_processor(field_name)
        tracked = self.get_tracked_values(field_name)
        try:
            result = apply_processor_tracked(proc, tracked, self.context)
        except Exception as e:
            raise ValueError(
                f"Error with output processor: field={field_name!r} "
                f"value={_unwrap(tracked)!r} error='{type(e).__name__}: {e!s}'"
            ) from e
        return _unwrap(result)

    def get_tracked_values(self, field_name: str) -> list[TrackedValue]:
        """Return the collected values for the given field, with provenance."""
        return [
            value
            if isinstance(value, TrackedValue)
            else TrackedValue(value, {UNKNOWN_RANGE})
            for value in self._values.get(field_name, [])
        ]

    def get_collected_values(self, field_name: str) -> list[Any]:
        return [value.value for value in self.get_tracked_values(field_name)]

    def get_field_sources(self, field_name: str) -> frozenset:
        """Return the sources the final value of *field_name* depends on.

        The field's output processor is applied (once) to the collected
        values, and the sources of the resulting value are returned: a set of
        :class:`SourceRef` objects, possibly containing :data:`UNKNOWN_RANGE`
        if the exact contributing inputs cannot be determined.
        """
        proc = self.get_output_processor(field_name)
        tracked = self.get_tracked_values(field_name)
        result = apply_processor_tracked(proc, tracked, self.context)
        return _union_sources(_elements(result))

    def add_xpath(
        self,
        field_name: str | None,
        xpath: str | Iterable[str],
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        page: str | None = None,
        **kw: Any,
    ) -> TrackedItemLoader:
        values = self._get_xpathvalues(xpath, field_name, **kw)
        return self.add_value(
            field_name,
            values,
            *processors,
            re=re,
            page=page,
            selector=_selector_id(xpath),
            **kw,
        )

    def replace_xpath(
        self,
        field_name: str | None,
        xpath: str | Iterable[str],
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        page: str | None = None,
        **kw: Any,
    ) -> TrackedItemLoader:
        values = self._get_xpathvalues(xpath, field_name, **kw)
        return self.replace_value(
            field_name,
            values,
            *processors,
            re=re,
            page=page,
            selector=_selector_id(xpath),
            **kw,
        )

    def add_css(
        self,
        field_name: str | None,
        css: str | Iterable[str],
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        page: str | None = None,
        **kw: Any,
    ) -> TrackedItemLoader:
        values = self._get_cssvalues(css, field_name)
        return self.add_value(
            field_name,
            values,
            *processors,
            re=re,
            page=page,
            selector=_selector_id(css),
            **kw,
        )

    def replace_css(
        self,
        field_name: str | None,
        css: str | Iterable[str],
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        page: str | None = None,
        **kw: Any,
    ) -> TrackedItemLoader:
        values = self._get_cssvalues(css, field_name)
        return self.replace_value(
            field_name,
            values,
            *processors,
            re=re,
            page=page,
            selector=_selector_id(css),
            **kw,
        )

    def add_jmes(
        self,
        field_name: str | None,
        jmes: str | Iterable[str],
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        page: str | None = None,
        **kw: Any,
    ) -> TrackedItemLoader:
        values = self._get_jmesvalues(jmes, field_name)
        return self.add_value(
            field_name,
            values,
            *processors,
            re=re,
            page=page,
            selector=_selector_id(jmes),
            **kw,
        )

    def replace_jmes(
        self,
        field_name: str | None,
        jmes: str | Iterable[str],
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        page: str | None = None,
        **kw: Any,
    ) -> TrackedItemLoader:
        values = self._get_jmesvalues(jmes, field_name)
        return self.replace_value(
            field_name,
            values,
            *processors,
            re=re,
            page=page,
            selector=_selector_id(jmes),
            **kw,
        )
