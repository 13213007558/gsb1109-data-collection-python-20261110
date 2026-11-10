"""Batched, conflict-checked edits to HTML and XML element trees.

Edits are collected first and applied together by :func:`apply_edits`. Every
selector used to build an edit is evaluated against the tree as it is when the
edit is created, before anything is modified. The whole batch is then checked
for conflicts: if two edits target overlapping nodes, or otherwise cannot be
applied together, :class:`EditConflictError` is raised and the tree is left
untouched, so a batch never leaves a half-edited tree behind.
"""

from __future__ import annotations

import copy
import typing
from typing import TYPE_CHECKING, Self

from lxml import etree, html

from .utils import shorten

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from .selector import Selector


class InvalidEditError(ValueError):
    """Raised when an edit cannot be applied regardless of the other edits.

    This covers, for example, dropping the root element or targeting an
    element that is not part of the tree the batch is applied to.
    """


class EditConflictError(Exception):
    """Raised when two edits of a batch cannot be applied together.

    The two conflicting edits are available, in the order in which the
    conflict was detected, as the :attr:`edits` attribute. The tree is not
    modified when this exception is raised.
    """

    def __init__(self, message: str, edits: tuple[BatchEdit, BatchEdit]) -> None:
        super().__init__(message)
        self.edits: tuple[BatchEdit, BatchEdit] = edits


def _element_from_selector(selector: Selector) -> etree._Element:
    root = selector.root
    if not isinstance(root, etree._Element):
        raise ValueError(
            f"{selector!r} does not wrap an element; edits only apply to "
            "element selectors, not to text or attribute selectors"
        )
    return root


def _elements_from_selectors(
    selectors: Iterable[Selector],
) -> tuple[etree._Element, ...]:
    return tuple(_element_from_selector(selector) for selector in selectors)


def _dedupe(elements: Iterable[etree._Element]) -> tuple[etree._Element, ...]:
    return tuple(dict.fromkeys(elements))


def _describe_element(element: etree._Element) -> str:
    return shorten(etree.tostring(element, encoding="unicode"), width=60)


def _is_or_ancestor(ancestor: etree._Element, descendant: etree._Element) -> bool:
    return ancestor is descendant or ancestor in descendant.iterancestors()


class BatchEdit:
    """Base class for edits applied together by :func:`apply_edits`."""

    elements: tuple[etree._Element, ...]

    def _describe(self) -> str:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._describe()})"


class DropElements(BatchEdit):
    """Remove elements from the tree, preserving their tail text."""

    def __init__(self, elements: Iterable[etree._Element]) -> None:
        self.elements: tuple[etree._Element, ...] = _dedupe(elements)

    @classmethod
    def from_selectors(cls, selectors: Iterable[Selector]) -> Self:
        """Build the edit from the elements wrapped by *selectors*."""
        return cls(_elements_from_selectors(selectors))

    def _describe(self) -> str:
        return ", ".join(_describe_element(element) for element in self.elements)


class ReplaceElements(BatchEdit):
    """Replace elements with the parsed *markup*, preserving tail text.

    The markup is parsed with the same parser as the target tree (HTML or
    XML) when the batch is applied. It may expand to several elements or to
    no element at all; the tail text of every replaced element is kept.
    """

    def __init__(self, elements: Iterable[etree._Element], markup: str) -> None:
        self.elements: tuple[etree._Element, ...] = _dedupe(elements)
        self.markup: str = markup

    @classmethod
    def from_selectors(cls, selectors: Iterable[Selector], markup: str) -> Self:
        """Build the edit from the elements wrapped by *selectors*."""
        return cls(_elements_from_selectors(selectors), markup)

    def _describe(self) -> str:
        targets = ", ".join(_describe_element(element) for element in self.elements)
        return f"{targets} with {shorten(self.markup, width=40)!r}"


class AttributeEdit(BatchEdit):
    """Base class for edits that change a single attribute by name."""

    elements: tuple[etree._Element, ...]
    name: str

    def _targets(self) -> str:
        return ", ".join(_describe_element(element) for element in self.elements)


class SetAttribute(AttributeEdit):
    """Set an attribute, given by its Clark or serialized name, on elements."""

    def __init__(
        self,
        elements: Iterable[etree._Element],
        name: str,
        value: str,
    ) -> None:
        self.elements: tuple[etree._Element, ...] = _dedupe(elements)
        self.name: str = name
        self.value: str = value

    @classmethod
    def from_selectors(
        cls,
        selectors: Iterable[Selector],
        name: str,
        value: str,
    ) -> Self:
        """Build the edit from the elements wrapped by *selectors*."""
        return cls(_elements_from_selectors(selectors), name, value)

    def _describe(self) -> str:
        return f"set {self.name!r}={self.value!r} on {self._targets()}"


class RemoveAttribute(AttributeEdit):
    """Remove an attribute, given by its Clark or serialized name, from elements."""

    def __init__(self, elements: Iterable[etree._Element], name: str) -> None:
        self.elements: tuple[etree._Element, ...] = _dedupe(elements)
        self.name: str = name

    @classmethod
    def from_selectors(cls, selectors: Iterable[Selector], name: str) -> Self:
        """Build the edit from the elements wrapped by *selectors*."""
        return cls(_elements_from_selectors(selectors), name)

    def _describe(self) -> str:
        return f"remove {self.name!r} from {self._targets()}"


_STRUCTURAL_EDITS = (DropElements, ReplaceElements)


class EditBatch:
    """Collect edits and apply them together.

    Selectors passed to the shortcut methods are evaluated immediately, on the
    unmodified tree. The batch itself only touches the tree when :meth:`apply`
    or :func:`apply_edits` is called, after the full batch has been checked
    for conflicts.
    """

    def __init__(self, edits: Iterable[BatchEdit] = ()) -> None:
        self._edits: list[BatchEdit] = list(edits)

    def add(self, edit: BatchEdit) -> Self:
        """Add an already built edit to the batch."""
        self._edits.append(edit)
        return self

    def drop(self, selectors: Iterable[Selector]) -> Self:
        """Schedule the elements wrapped by *selectors* for removal."""
        return self.add(DropElements.from_selectors(selectors))

    def replace(self, selectors: Iterable[Selector], markup: str) -> Self:
        """Schedule the elements wrapped by *selectors* for replacement."""
        return self.add(ReplaceElements.from_selectors(selectors, markup))

    def set_attribute(
        self, selectors: Iterable[Selector], name: str, value: str
    ) -> Self:
        """Schedule setting attribute *name* to *value* on *selectors*."""
        return self.add(SetAttribute.from_selectors(selectors, name, value))

    def remove_attribute(self, selectors: Iterable[Selector], name: str) -> Self:
        """Schedule removing attribute *name* from *selectors*."""
        return self.add(RemoveAttribute.from_selectors(selectors, name))

    def apply(
        self,
        root: etree._Element,
        *,
        in_place: bool = False,
    ) -> etree._Element:
        """Apply the whole batch, see :func:`apply_edits`."""
        return apply_edits(root, self._edits, in_place=in_place)

    def __len__(self) -> int:
        return len(self._edits)

    def __iter__(self) -> Iterator[BatchEdit]:
        return iter(self._edits)


def _validate_membership(root: etree._Element, edits: list[BatchEdit]) -> None:
    for edit in edits:
        for element in edit.elements:
            if element is root:
                continue
            if root not in element.iterancestors():
                raise InvalidEditError(
                    f"{edit!r} targets {_describe_element(element)}, which is "
                    "not part of the tree the edits are applied to"
                )


def _check_conflicts(edits: list[BatchEdit]) -> None:
    structural: list[tuple[BatchEdit, frozenset[etree._Element]]] = []
    attributes: list[tuple[AttributeEdit, etree._Element, str]] = []
    for edit in edits:
        if isinstance(edit, _STRUCTURAL_EDITS):
            targets = frozenset(edit.elements)
            for element in targets:
                if element.getparent() is None:
                    raise InvalidEditError(
                        f"{edit!r} targets the root element, which has no "
                        "parent to remove or replace it from"
                    )
            for previous_edit, previous_targets in structural:
                if any(
                    _is_or_ancestor(first, second) or _is_or_ancestor(second, first)
                    for first in targets
                    for second in previous_targets
                ):
                    raise EditConflictError(
                        f"conflicting edits {previous_edit!r} and {edit!r}: "
                        "they target the same node or ancestor and descendant "
                        "nodes",
                        (previous_edit, edit),
                    )
            for previous_edit, previous_element, _ in attributes:
                if previous_element in targets or any(
                    target in previous_element.iterancestors() for target in targets
                ):
                    raise EditConflictError(
                        f"conflicting edits {previous_edit!r} and {edit!r}: "
                        "the structural edit removes or replaces the element "
                        "carrying the attribute edit",
                        (previous_edit, edit),
                    )
            structural.append((edit, targets))
            continue
        if not isinstance(edit, AttributeEdit):
            msg = f"Unsupported edit type: {type(edit).__name__}"
            raise TypeError(msg)
        for element in edit.elements:
            for previous_edit, previous_element, previous_name in attributes:
                if element is previous_element and edit.name == previous_name:
                    raise EditConflictError(
                        f"conflicting edits {previous_edit!r} and {edit!r}: "
                        f"they both modify attribute {edit.name!r} of the "
                        "same element",
                        (previous_edit, edit),
                    )
            for previous_edit, previous_targets in structural:
                if element in previous_targets or any(
                    target in element.iterancestors() for target in previous_targets
                ):
                    raise EditConflictError(
                        f"conflicting edits {previous_edit!r} and {edit!r}: "
                        "the structural edit removes or replaces the element "
                        "carrying the attribute edit",
                        (previous_edit, edit),
                    )
            attributes.append((edit, element, edit.name))


def _identity_map(
    source_root: etree._Element, target_root: etree._Element
) -> dict[etree._Element, etree._Element]:
    return dict(
        zip(
            source_root.iter(),
            target_root.iter(),
            strict=True,
        )
    )


def _parse_markup(markup: str, *, is_html: bool) -> etree._Element:
    """Parse *markup* into a detached container holding the replacement nodes.

    The container's text holds any leading text, and its children are the
    element nodes, each with its own tail text.
    """
    if is_html:
        # Wrapping in an element keeps leading whitespace, which
        # fragments_fromstring() drops when it precedes every element.
        document = html.document_fromstring(f"<div>{markup}</div>")
        return document.find(".//div")
    try:
        return etree.fromstring(f"<dummy>{markup}</dummy>")
    except etree.XMLSyntaxError as exc:
        raise InvalidEditError(f"invalid XML replacement markup: {markup!r} ({exc})")


def _merge_text_before(parent: etree._Element, index: int, text: str) -> None:
    if not text:
        return
    if index == 0:
        parent.text = (parent.text or "") + text
    else:
        previous = parent[index - 1]
        previous.tail = (previous.tail or "") + text


def _drop_element(element: etree._Element, *, is_html: bool) -> None:
    parent = element.getparent()
    assert parent is not None  # nosec
    if is_html:
        # HtmlElement.drop_tree() preserves the tail text itself.
        typing.cast("html.HtmlElement", element).drop_tree()
        return
    index = parent.index(element)
    tail = element.tail
    parent.remove(element)
    _merge_text_before(parent, index, tail or "")


def _replace_element(element: etree._Element, container: etree._Element) -> None:
    parent = element.getparent()
    assert parent is not None  # nosec
    index = parent.index(element)
    new_children = list(container)
    tail = element.tail
    _merge_text_before(parent, index, container.text or "")
    for offset, child in enumerate(new_children):
        parent.insert(index + offset, child)
    parent.remove(element)
    if new_children:
        last = new_children[-1]
        last.tail = (last.tail or "") + (tail or "")
    else:
        _merge_text_before(parent, index, tail or "")


def _is_connected(root: etree._Element, element: etree._Element) -> bool:
    return element is root or root in element.iterancestors()


def _apply(
    edits: list[BatchEdit],
    target_root: etree._Element,
    mapping: dict[etree._Element, etree._Element],
    replacements: dict[int, etree._Element],
    *,
    is_html: bool,
) -> None:
    for edit in edits:
        elements = [mapping.get(element, element) for element in edit.elements]
        # A single edit can target both an ancestor and its descendants. Once
        # the ancestor is removed or replaced, those descendants are no longer
        # part of the result tree, so they must be skipped.
        elements = [
            element for element in elements if _is_connected(target_root, element)
        ]
        if isinstance(edit, DropElements):
            for element in elements:
                _drop_element(element, is_html=is_html)
        elif isinstance(edit, ReplaceElements):
            template = replacements[id(edit)]
            for element in elements:
                _replace_element(element, copy.deepcopy(template))
        elif isinstance(edit, SetAttribute):
            for element in elements:
                element.set(edit.name, edit.value)
        elif isinstance(edit, RemoveAttribute):
            for element in elements:
                element.attrib.pop(edit.name, None)
        else:  # pragma: no cover
            msg = f"Unsupported edit type: {type(edit).__name__}"
            raise TypeError(msg)


def _prepare_replacements(
    edits: list[BatchEdit], *, is_html: bool
) -> dict[int, etree._Element]:
    """Parse every replacement markup before the tree is touched."""
    return {
        id(edit): _parse_markup(edit.markup, is_html=is_html)
        for edit in edits
        if isinstance(edit, ReplaceElements)
    }


def _validate_attribute_names(edits: list[BatchEdit], *, is_html: bool) -> None:
    """Check attribute names on a detached element before touching the tree."""
    scratch = html.Element("dummy") if is_html else etree.Element("dummy")
    for edit in edits:
        if isinstance(edit, SetAttribute):
            try:
                scratch.set(edit.name, edit.value)
            except ValueError as exc:
                raise InvalidEditError(
                    f"{edit!r} uses an invalid attribute name {edit.name!r}: {exc}"
                )
        elif isinstance(edit, RemoveAttribute):
            try:
                scratch.attrib.pop(edit.name, None)
            except ValueError as exc:
                raise InvalidEditError(
                    f"{edit!r} uses an invalid attribute name {edit.name!r}: {exc}"
                )


def apply_edits(
    root: etree._Element,
    edits: EditBatch | Iterable[BatchEdit],
    *,
    in_place: bool = False,
) -> etree._Element:
    """Apply a batch of edits to an HTML or XML element tree.

    All edits are checked before the tree is modified. When *in_place* is
    ``False`` (the default), the edits are applied to a deep copy of *root*
    and the copy is returned, leaving *root* untouched. When *in_place* is
    ``True``, *root* itself is modified and returned.

    Tail text of removed or replaced nodes and namespaced attributes are
    preserved. Conflicting edits raise :class:`EditConflictError` and leave
    the tree untouched.
    """
    edit_list = list(edits)
    _validate_membership(root, edit_list)
    _check_conflicts(edit_list)
    is_html = isinstance(root, html.HtmlElement)
    replacements = _prepare_replacements(edit_list, is_html=is_html)
    _validate_attribute_names(edit_list, is_html=is_html)
    if in_place:
        target_root = root
        mapping: dict[etree._Element, etree._Element] = {}
    else:
        target_root = copy.deepcopy(root)
        mapping = _identity_map(root, target_root)
    _apply(
        edit_list,
        target_root,
        mapping,
        replacements,
        is_html=is_html,
    )
    return target_root
