"""Explain why a CSS selector matches, or does not match, a given element.

The regular CSS entry points (``Selector.css()``) only return the matching
nodes. When a node that is visibly present in the document is not returned,
finding which of the selector's requirements fails means deleting parts of
the selector and retrying. This module inspects the same parsed selector the
CSS engine uses and checks every requirement against one specific element:

* the node's own conditions (type, id, classes, attributes, pseudo-classes),
* the ancestor and sibling relationships expressed by combinators,
* the positional conditions (``:nth-child()`` and friends).

Only syntax supported by the CSS engine in use is accepted. Anything outside
that support raises :class:`UnsupportedSelectorSyntax` instead of guessing a
conclusion. The explanation only reads the document: it never modifies nodes
and never evaluates user scripts.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from cssselect.parser import (
    Attrib,
    Class,
    CombinedSelector,
    Element,
    Function,
    Hash,
    Matching,
    Negation,
    Pseudo,
    Relation,
    SpecificityAdjustment,
    Token,
    ascii_lower,
    parse,
    parse_series,
)
from cssselect.parser import Selector as ParsedSelector
from cssselect.xpath import ExpressionError
from lxml import etree

from .csstranslator import GenericTranslator, HTMLTranslator

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cssselect.parser import Tree


@runtime_checkable
class _CompoundWrapper(Protocol):
    """The parsed nodes that wrap another selector via a ``selector`` field."""

    selector: Tree


# Categories of checks, used to group the requirements of a compound selector.
SELF_CONDITION = "self"
POSITION_CONDITION = "position"
RELATION_CONDITION = "relation"

CATEGORY_LABELS = {
    SELF_CONDITION: "node condition",
    POSITION_CONDITION: "position condition",
    RELATION_CONDITION: "relative selector",
}

_COMBINATOR_LABELS = {
    " ": "descendant",
    ">": "child",
    "+": "adjacent sibling",
    "~": "general sibling",
}

# Pseudo-classes the cssselect engine can translate. The ones that always
# evaluate to false are listed for honest reporting, not guessing.
_SUPPORTED_PSEUDOS = frozenset(
    {
        "root",
        "scope",
        "first-child",
        "last-child",
        "only-child",
        "first-of-type",
        "last-of-type",
        "only-of-type",
        "empty",
        "link",
        "visited",
        "hover",
        "active",
        "focus",
        "target",
        "enabled",
        "disabled",
        "checked",
    }
)

# Pseudo-classes the engine translates to a condition that never holds in a
# static document.
_NEVER_MATCHING_PSEUDOS = frozenset({"visited", "hover", "active", "focus", "target"})

# Pseudo-classes whose exact semantics are involved enough (form state,
# language scoping) that reusing the engine's own XPath condition is the only
# way to guarantee the same conclusion.
_ENGINE_PSEUDOS = frozenset({"link", "enabled", "disabled", "checked"})

_SUPPORTED_FUNCTIONS = frozenset(
    {
        "nth-child",
        "nth-last-child",
        "nth-of-type",
        "nth-last-of-type",
        "contains",
        "lang",
    }
)

_NTH_FUNCTIONS = frozenset(
    {"nth-child", "nth-last-child", "nth-of-type", "nth-last-of-type"}
)

_OF_TYPE_FUNCTIONS = frozenset({"nth-of-type", "nth-last-of-type"})

_SUPPORTED_ATTRIB_OPERATORS = frozenset(
    {"exists", "=", "~=", "|=", "^=", "$=", "*=", "!="}
)

_SUPPORTED_SIMPLE_PSEUDO_ELEMENTS = frozenset({"text"})
_SUPPORTED_FUNCTIONAL_PSEUDO_ELEMENTS = frozenset({"attr"})


class UnsupportedSelectorSyntax(ValueError):
    """Raised when a selector uses syntax the CSS engine cannot explain.

    The explanation feature accepts exactly the CSS syntax that the engine
    backing :meth:`Selector.css` supports. Syntax outside of that range,
    including syntax the engine itself rejects, raises this exception so that
    no conclusion is guessed.
    """


@dataclass(frozen=True)
class ConditionCheck:
    """One requirement attached to a single compound selector."""

    #: CSS fragment expressing the requirement, e.g. ``".price"`` or
    #: ``":nth-child(2)"``.
    expression: str
    #: One of ``"self"``, ``"position"`` or ``"relation"``.
    category: str
    #: Whether the requirement holds for the explained node.
    passed: bool
    #: Human-readable detail, including the observed value on failure.
    detail: str


@dataclass(frozen=True)
class CombinatorCheck:
    """One relationship between two adjacent compound selectors."""

    #: The raw CSS combinator: ``" "``, ``">"``, ``"+"`` or ``"~"``.
    combinator: str
    #: Canonical text of the compound selector on the left of the combinator.
    expression: str
    #: Whether the required ancestor or sibling relationship holds.
    passed: bool
    #: Human-readable detail, naming the missing relationship on failure.
    detail: str


@dataclass(frozen=True)
class CompoundCheck:
    """The checks for one compound selector, evaluated against one node."""

    #: Canonical text of the compound selector.
    expression: str
    #: Whether every requirement of the compound holds.
    matched: bool
    #: Individual node and position requirements, in selector order.
    checks: tuple[ConditionCheck, ...]


@dataclass(frozen=True)
class SelectorExplanation:
    """Explanation for one branch of a comma-separated selector list."""

    #: Canonical text of the whole selector branch.
    selector: str
    #: Pseudo-element of the branch (``"::text"``, ``"::attr(...)"``) or None.
    #: Pseudo-elements select text or attribute values rather than elements
    #: and do not restrict element matching.
    pseudo_element: str | None
    #: Whether the branch matches the explained node.
    matched: bool
    #: Compound selectors from left to right, the last one targeting the node.
    compounds: tuple[CompoundCheck, ...]
    #: Combinator checks, one per gap between adjacent compounds.
    combinators: tuple[CombinatorCheck, ...]
    #: Flat list of the necessary requirements that are not satisfied.
    failure_reasons: tuple[str, ...]


@dataclass(frozen=True)
class MatchExplanation:
    """Explanation of a CSS selector list against one element."""

    #: The CSS selector list as provided.
    selector: str
    #: Short description of the explained element.
    node: str
    #: Whether at least one branch matches the element.
    matched: bool
    #: One explanation per branch of the selector list, in source order.
    branches: tuple[SelectorExplanation, ...]

    @property
    def failure_reasons(self) -> tuple[str, ...]:
        """All missing requirements, across every non-matching branch."""
        reasons: list[str] = []
        for branch in self.branches:
            if not branch.matched:
                reasons.extend(
                    f"{branch.selector}: {reason}" for reason in branch.failure_reasons
                )
        return tuple(reasons)

    def __str__(self) -> str:
        lines = [
            f"{self.selector!r} against {self.node}: "
            f"{'matches' if self.matched else 'does not match'}"
        ]
        for branch in self.branches:
            lines.append(
                f"  - {branch.selector}: {'match' if branch.matched else 'no match'}"
            )
            for compound in branch.compounds:
                for check in compound.checks:
                    marker = "ok" if check.passed else "FAIL"
                    lines.append(
                        f"      [{marker}] {check.expression} ({check.category}): "
                        f"{check.detail}"
                    )
            for combinator in branch.combinators:
                marker = "ok" if combinator.passed else "FAIL"
                lines.append(
                    f"      [{marker}] {combinator.expression} "
                    f"({_COMBINATOR_LABELS[combinator.combinator]}): "
                    f"{combinator.detail}"
                )
            lines.extend(
                f"      missing: {reason}" for reason in branch.failure_reasons
            )
        return "\n".join(lines)


def _describe_node(element: etree._Element) -> str:
    tag = etree.QName(element).localname
    element_id = element.get("id")
    if element_id is not None:
        return f"<{tag} id={element_id!r}>"
    return f"<{tag}>"


def _element_children(element: etree._Element) -> list[etree._Element]:
    parent = element.getparent()
    if parent is None:
        return []
    return [child for child in parent if isinstance(child.tag, str)]


def _preceding_element_siblings(element: etree._Element) -> list[etree._Element]:
    parent = element.getparent()
    if parent is None:
        return []
    siblings = [child for child in parent if isinstance(child.tag, str)]
    index = siblings.index(element)
    return siblings[:index]


def _following_element_siblings(element: etree._Element) -> list[etree._Element]:
    parent = element.getparent()
    if parent is None:
        return []
    siblings = [child for child in parent if isinstance(child.tag, str)]
    index = siblings.index(element)
    return siblings[index + 1 :]


def _series_matches(a: int, b: int, position: int) -> bool:
    """Whether *position* (1-based) is accepted by an ``an+b`` nth series."""
    if a == 0:
        return position == b
    if a > 0:
        return position >= b and (position - b) % a == 0
    return position <= b and (b - position) % (-a) == 0


def _series_text(arguments: Sequence[Token]) -> str:
    return "".join(token.css() for token in arguments)


def _innermost_element(tree: Tree) -> Element:
    while not isinstance(tree, Element):
        tree = cast("_CompoundWrapper", tree).selector
    return tree


def _ensure_supported(tree: Tree) -> None:
    """Reject parsed nodes the CSS engine would not translate faithfully."""
    if isinstance(tree, CombinedSelector):
        if tree.combinator not in _COMBINATOR_LABELS:
            raise UnsupportedSelectorSyntax(
                f"Unsupported combinator {tree.combinator!r}"
            )
        _ensure_supported(tree.selector)
        _ensure_supported(tree.subselector)
        return
    if isinstance(tree, Element):
        if tree.namespace and tree.namespace != "*":
            raise UnsupportedSelectorSyntax(
                f"Namespaced type selectors such as {tree.canonical()!r} "
                "cannot be explained without a namespace prefix mapping"
            )
        return
    if isinstance(tree, (Hash, Class)):
        _ensure_supported(tree.selector)
        return
    if isinstance(tree, Attrib):
        if tree.operator not in _SUPPORTED_ATTRIB_OPERATORS:
            raise UnsupportedSelectorSyntax(
                f"Unsupported attribute operator {tree.operator!r}"
            )
        if tree.namespace and tree.namespace != "*":
            raise UnsupportedSelectorSyntax(
                "Namespaced attribute selectors cannot be explained without "
                "a namespace prefix mapping"
            )
        _ensure_supported(tree.selector)
        return
    if isinstance(tree, Negation):
        _ensure_supported(tree.selector)
        _ensure_supported(tree.subselector)
        return
    if isinstance(tree, (Matching, SpecificityAdjustment)):
        _ensure_supported(tree.selector)
        for subselector in tree.selector_list:
            if isinstance(subselector, CombinedSelector):
                # The engine itself rejects complex selectors here, and the
                # parser does not produce them, but guard hand-built trees.
                raise UnsupportedSelectorSyntax(
                    "Combined selectors are not supported inside :is() and :where()"
                )
            _ensure_supported(subselector)
        return
    if isinstance(tree, Relation):
        _ensure_supported(tree.selector)
        for rel_combinator, rel_selector in tree.arguments:
            combinator_value = rel_combinator.value or " "
            if combinator_value not in _COMBINATOR_LABELS:
                raise UnsupportedSelectorSyntax(
                    f"Unsupported combinator {combinator_value!r} in :has()"
                )
            _ensure_supported(rel_selector.parsed_tree)
        return
    if isinstance(tree, Pseudo):
        if tree.ident not in _SUPPORTED_PSEUDOS:
            raise UnsupportedSelectorSyntax(
                f"The pseudo-class :{tree.ident} is not supported by the CSS engine"
            )
        _ensure_supported(tree.selector)
        return
    if isinstance(tree, Function):
        if tree.name not in _SUPPORTED_FUNCTIONS:
            raise UnsupportedSelectorSyntax(
                f"The pseudo-class :{tree.name}() is not supported by the CSS engine"
            )
        if tree.name in _NTH_FUNCTIONS:
            try:
                parse_series(tree.arguments)
            except ValueError as exc:
                raise UnsupportedSelectorSyntax(
                    f"Invalid :{tree.name}() series "
                    f"{_series_text(tree.arguments)!r}: {exc}"
                ) from exc
            if (
                tree.name in _OF_TYPE_FUNCTIONS
                and _innermost_element(tree.selector).element is None
            ):
                raise UnsupportedSelectorSyntax(
                    f":{tree.name}() with a universal type selector is not "
                    "implemented in the CSS engine"
                )
        elif tree.argument_types() not in (["STRING"], ["IDENT"]):
            raise UnsupportedSelectorSyntax(
                f"Expected a single string or ident for :{tree.name}(), "
                f"got {tree.arguments!r}"
            )
        _ensure_supported(tree.selector)
        return
    raise UnsupportedSelectorSyntax(
        f"Cannot explain selector node {type(tree).__name__}"
    )


def _ensure_pseudo_element_supported(selector: ParsedSelector) -> None:
    pseudo_element = selector.pseudo_element
    if pseudo_element is None:
        return
    if isinstance(pseudo_element, str):
        if pseudo_element not in _SUPPORTED_SIMPLE_PSEUDO_ELEMENTS:
            raise UnsupportedSelectorSyntax(
                f"The pseudo-element ::{pseudo_element} is not supported by "
                "the CSS engine"
            )
        return
    if pseudo_element.name not in _SUPPORTED_FUNCTIONAL_PSEUDO_ELEMENTS:
        raise UnsupportedSelectorSyntax(
            f"The pseudo-element ::{pseudo_element.name}() is not supported "
            "by the CSS engine"
        )
    if pseudo_element.argument_types() not in (["STRING"], ["IDENT"]):
        raise UnsupportedSelectorSyntax(
            "Expected a single string or ident for ::attr(), got "
            f"{pseudo_element.arguments!r}"
        )


@lru_cache(maxsize=128)
def _parse_supported(css: str) -> tuple[ParsedSelector, ...]:
    selectors = tuple(parse(css))
    for selector in selectors:
        _ensure_supported(selector.parsed_tree)
        _ensure_pseudo_element_supported(selector)
    return selectors


@lru_cache(maxsize=256)
def _compile_boolean(xpath: str) -> etree.XPath:
    return etree.XPath(xpath)


class _Matcher:
    """Evaluates parsed selector requirements against lxml elements.

    Matching walks complex selectors right to left. Descendant (`` ``) and
    general sibling (``~``) combinators have several possible anchor nodes,
    so :meth:`_match_path` backtracks through all of them. The result
    therefore follows the same node set semantics as the engine's XPath
    translation rather than a single greedy path.
    """

    def __init__(self, document_type: str, scope: etree._Element) -> None:
        self.html_mode = document_type == "html"
        self.translator = HTMLTranslator() if self.html_mode else GenericTranslator()
        self.scope = scope

    def tag_name(self, element: etree._Element) -> str:
        name = str(etree.QName(element).localname)
        return name.lower() if self.html_mode else name

    def explain(self, css: str, element: etree._Element) -> MatchExplanation:
        branches = tuple(
            self._explain_selector(selector, element)
            for selector in _parse_supported(css)
        )
        return MatchExplanation(
            selector=css,
            node=_describe_node(element),
            matched=any(branch.matched for branch in branches),
            branches=branches,
        )

    def _explain_selector(
        self, selector: ParsedSelector, element: etree._Element
    ) -> SelectorExplanation:
        compounds: list[Tree] = []
        combinators: list[str] = []
        self._split_compounds(selector.parsed_tree, compounds, combinators)

        path = self._match_path(compounds, combinators, len(compounds) - 1, element)
        if path is not None:
            compound_checks = [
                self._check_compound(compound, node)
                for compound, node in zip(compounds, path, strict=True)
            ]
            combinator_checks = [
                CombinatorCheck(
                    combinator,
                    compounds[index].canonical(),
                    True,
                    self._found_relation_text(
                        combinator,
                        compounds[index].canonical(),
                        path[index],
                    ),
                )
                for index, combinator in enumerate(combinators)
            ]
            return SelectorExplanation(
                selector=selector.canonical(),
                pseudo_element=self._pseudo_element_text(selector),
                matched=True,
                compounds=tuple(compound_checks),
                combinators=tuple(combinator_checks),
                failure_reasons=(),
            )

        compound_checks, combinator_checks, failure_reasons = self._diagnostic_walk(
            compounds, combinators, element
        )
        return SelectorExplanation(
            selector=selector.canonical(),
            pseudo_element=self._pseudo_element_text(selector),
            matched=False,
            compounds=tuple(compound_checks),
            combinators=tuple(combinator_checks),
            failure_reasons=tuple(failure_reasons),
        )

    @staticmethod
    def _pseudo_element_text(selector: ParsedSelector) -> str | None:
        pseudo_element = selector.pseudo_element
        if pseudo_element is None:
            return None
        if isinstance(pseudo_element, str):
            return f"::{pseudo_element}"
        return f"::{pseudo_element.canonical()}"

    @staticmethod
    def _split_compounds(
        tree: Tree,
        compounds: list[Tree],
        combinators: list[str],
    ) -> None:
        if isinstance(tree, CombinedSelector):
            _Matcher._split_compounds(tree.selector, compounds, combinators)
            combinators.append(tree.combinator)
            compounds.append(tree.subselector)
        else:
            compounds.append(tree)

    def _match_path(
        self,
        compounds: list[Tree],
        combinators: list[str],
        index: int,
        element: etree._Element,
    ) -> list[etree._Element] | None:
        """Return a left-to-right node path matching all compounds, or None."""
        if not self._check_compound(compounds[index], element).matched:
            return None
        if index == 0:
            return [element]
        combinator = combinators[index - 1]
        for anchor in self._relation_anchors(combinator, element):
            sub_path = self._match_path(compounds, combinators, index - 1, anchor)
            if sub_path is not None:
                return [*sub_path, element]
        return None

    def _diagnostic_walk(
        self,
        compounds: list[Tree],
        combinators: list[str],
        element: etree._Element,
    ) -> tuple[list[CompoundCheck], list[CombinatorCheck], list[str]]:
        """Greedy right-to-left walk producing details for the first failure.

        Every evaluated compound is recorded with its individual checks, so a
        failed anchor compound shows which of its own node or position
        conditions fails instead of only naming the combinator. The walk
        stops at the first necessary requirement that does not hold.
        """
        compound_checks: list[CompoundCheck] = []
        combinator_checks: list[CombinatorCheck] = []
        failure_reasons: list[str] = []
        current: etree._Element = element

        for index in range(len(compounds) - 1, -1, -1):
            compound = compounds[index]
            check = self._check_compound(compound, current)
            compound_checks.append(check)
            if not check.matched:
                failure_reasons.extend(self._compound_failures(compound, check))
                break
            if index == 0:
                break
            combinator = combinators[index - 1]
            left_compound = compounds[index - 1]
            anchors = self._relation_anchors(combinator, current)
            if not anchors:
                combinator_check = CombinatorCheck(
                    combinator,
                    left_compound.canonical(),
                    False,
                    self._no_anchor_text(combinator, left_compound, current),
                )
                combinator_checks.append(combinator_check)
                failure_reasons.append(combinator_check.detail)
                break
            anchor_checks = [
                (anchor, self._check_compound(left_compound, anchor))
                for anchor in anchors
            ]
            matching = [
                anchor for anchor, anchor_check in anchor_checks if anchor_check.matched
            ]
            if matching:
                anchor = matching[0]
                combinator_checks.append(
                    CombinatorCheck(
                        combinator,
                        left_compound.canonical(),
                        True,
                        self._found_relation_text(
                            combinator, left_compound.canonical(), anchor
                        ),
                    )
                )
                current = anchor
                continue
            nearest_anchor, nearest_check = anchor_checks[0]
            compound_checks.append(nearest_check)
            combinator_check = CombinatorCheck(
                combinator,
                left_compound.canonical(),
                False,
                self._anchor_mismatch_text(
                    combinator,
                    left_compound,
                    nearest_anchor,
                    nearest_check,
                    len(anchors),
                ),
            )
            combinator_checks.append(combinator_check)
            failure_reasons.append(combinator_check.detail)
            break

        compound_checks.reverse()
        combinator_checks.reverse()
        return compound_checks, combinator_checks, failure_reasons

    @staticmethod
    def _compound_failures(compound: Tree, check: CompoundCheck) -> list[str]:
        return [
            f"{compound.canonical()} {CATEGORY_LABELS[item.category]} "
            f"{item.expression} failed: {item.detail}"
            for item in check.checks
            if not item.passed
        ]

    def _relation_anchors(
        self, combinator: str, element: etree._Element
    ) -> list[etree._Element]:
        """All nodes that can satisfy the left side of *combinator*."""
        if combinator == ">":
            parent = element.getparent()
            return [parent] if parent is not None else []
        if combinator == " ":
            anchors: list[etree._Element] = []
            ancestor = element.getparent()
            while ancestor is not None:
                anchors.append(ancestor)
                ancestor = ancestor.getparent()
            return anchors
        preceding = _preceding_element_siblings(element)
        if combinator == "+":
            return preceding[-1:]
        return preceding

    @staticmethod
    def _no_anchor_text(
        combinator: str,
        left_compound: Tree,
        right_element: etree._Element,
    ) -> str:
        label = _COMBINATOR_LABELS[combinator]
        expression = left_compound.canonical()
        if combinator in {" ", ">"}:
            return (
                f"required {label} matching {expression!r} is missing: "
                f"{_describe_node(right_element)} has no ancestor (it may "
                "be the document root)"
            )
        return (
            f"required {label} matching {expression!r} is missing: "
            f"{_describe_node(right_element)} has no preceding element "
            "sibling"
        )

    def _anchor_mismatch_text(
        self,
        combinator: str,
        left_compound: Tree,
        anchor: etree._Element,
        anchor_check: CompoundCheck,
        candidate_count: int,
    ) -> str:
        label = _COMBINATOR_LABELS[combinator]
        expression = left_compound.canonical()
        failed = [
            f"{item.expression} ({item.detail})"
            for item in anchor_check.checks
            if not item.passed
        ]
        candidate_text = (
            f"the only candidate {label} node"
            if candidate_count == 1
            else f"none of the {candidate_count} candidate {label} nodes; "
            f"the nearest one"
        )
        return (
            f"required {label} matching {expression!r} is missing: "
            f"{candidate_text} {_describe_node(anchor)} fails "
            f"{'; '.join(failed)}"
        )

    @staticmethod
    def _found_relation_text(
        combinator: str, expression: str, anchor: etree._Element
    ) -> str:
        label = _COMBINATOR_LABELS[combinator]
        return (
            f"{label} relationship holds with {_describe_node(anchor)} "
            f"matching {expression!r}"
        )

    def _check_compound(self, compound: Tree, element: etree._Element) -> CompoundCheck:
        checks = self._check_tree(compound, element)
        return CompoundCheck(
            expression=compound.canonical(),
            matched=all(item.passed for item in checks),
            checks=tuple(checks),
        )

    def _check_tree(self, tree: Tree, element: etree._Element) -> list[ConditionCheck]:
        leaves: list[Tree] = []
        node: Tree = tree
        while isinstance(node, _CompoundWrapper):
            leaves.append(node)
            node = node.selector
        leaves.append(node)
        leaves.reverse()
        checks: list[ConditionCheck] = []
        for leaf in leaves:
            checks.extend(self._check_leaf(leaf, element))
        return checks

    def _check_leaf(self, leaf: Tree, element: etree._Element) -> list[ConditionCheck]:
        if isinstance(leaf, Element):
            return [self._check_element(leaf, element)]
        if isinstance(leaf, Hash):
            return [self._check_hash(leaf, element)]
        if isinstance(leaf, Class):
            return [self._check_class(leaf, element)]
        if isinstance(leaf, Attrib):
            return [self._check_attrib(leaf, element)]
        if isinstance(leaf, Pseudo):
            return [self._check_pseudo(leaf, element)]
        if isinstance(leaf, Function):
            return [self._check_function(leaf, element)]
        if isinstance(leaf, Negation):
            return [self._check_negation(leaf, element)]
        if isinstance(leaf, (Matching, SpecificityAdjustment)):
            return [self._check_selector_list(leaf, element)]
        if isinstance(leaf, Relation):
            return [self._check_relation(leaf, element)]
        raise UnsupportedSelectorSyntax(
            f"Cannot explain selector node {type(leaf).__name__}"
        )

    def _check_element(self, leaf: Element, element: etree._Element) -> ConditionCheck:
        expression = leaf.canonical()
        if leaf.element is None:
            return ConditionCheck(
                expression,
                SELF_CONDITION,
                True,
                "universal selector matches any element",
            )
        wanted = ascii_lower(leaf.element) if self.html_mode else leaf.element
        actual = self.tag_name(element)
        passed = actual == wanted
        detail = (
            f"element type is {actual!r}"
            if passed
            else f"expected element type {wanted!r}, got {actual!r}"
        )
        return ConditionCheck(expression, SELF_CONDITION, passed, detail)

    def _check_hash(self, leaf: Hash, element: etree._Element) -> ConditionCheck:
        actual = element.get("id")
        passed = actual == leaf.id
        detail = (
            f"id is {actual!r}"
            if passed
            else f"expected id {leaf.id!r}, got {actual!r}"
        )
        return ConditionCheck(f"#{leaf.id}", SELF_CONDITION, passed, detail)

    def _check_class(self, leaf: Class, element: etree._Element) -> ConditionCheck:
        classes = element.get("class", "").split()
        passed = leaf.class_name in classes
        detail = (
            f"class {leaf.class_name!r} is present"
            if passed
            else f"class {leaf.class_name!r} is not among {classes!r}"
        )
        return ConditionCheck(f".{leaf.class_name}", SELF_CONDITION, passed, detail)

    def _attribute_values(self, leaf: Attrib, element: etree._Element) -> list[str]:
        if leaf.namespace == "*":
            return [
                value
                for key, value in element.attrib.items()
                if etree.QName(key).localname == leaf.attrib
            ]
        value = element.get(leaf.attrib)
        return [] if value is None else [value]

    def _check_attrib(self, leaf: Attrib, element: etree._Element) -> ConditionCheck:
        expression = leaf.canonical()[len(leaf.selector.canonical()) :]
        values = self._attribute_values(leaf, element)
        if leaf.operator == "exists":
            passed = bool(values)
            detail = (
                "attribute is present"
                if passed
                else f"attribute {leaf.attrib!r} is absent"
            )
            return ConditionCheck(expression, SELF_CONDITION, passed, detail)

        assert leaf.value is not None
        wanted = leaf.value.value
        assert wanted is not None
        if leaf.flag == "i":
            wanted = ascii_lower(wanted)
            compared = [ascii_lower(value) for value in values]
        else:
            compared = list(values)

        if leaf.operator == "=":
            passed = wanted in compared
        elif leaf.operator == "!=":
            # The engine treats a missing attribute as different, unless the
            # compared value is empty: not(@x) or @x != v.
            passed = not values or wanted not in compared
        elif leaf.operator == "~=":
            passed = any(wanted in value.split() for value in compared)
        elif leaf.operator == "|=":
            passed = any(
                value == wanted or value.startswith(f"{wanted}-") for value in compared
            )
        elif leaf.operator == "^=":
            passed = bool(wanted) and any(
                value.startswith(wanted) for value in compared
            )
        elif leaf.operator == "$=":
            passed = bool(wanted) and any(value.endswith(wanted) for value in compared)
        else:  # "*="
            passed = bool(wanted) and any(wanted in value for value in compared)

        actual = values[0] if values else None
        if passed:
            detail = (
                f"attribute {leaf.attrib!r} value {actual!r} satisfies {expression}"
            )
        elif not values:
            detail = f"attribute {leaf.attrib!r} is absent"
        else:
            detail = (
                f"attribute {leaf.attrib!r} value {actual!r} does not "
                f"satisfy {expression}"
            )
        return ConditionCheck(expression, SELF_CONDITION, passed, detail)

    def _check_pseudo(self, leaf: Pseudo, element: etree._Element) -> ConditionCheck:
        expression = f":{leaf.ident}"
        if leaf.ident in _NEVER_MATCHING_PSEUDOS:
            return ConditionCheck(
                expression,
                SELF_CONDITION,
                False,
                f":{leaf.ident} never matches in a static HTML document",
            )
        if leaf.ident in _ENGINE_PSEUDOS:
            return self._engine_condition(expression, leaf, element)
        if leaf.ident == "root":
            passed = element.getparent() is None
            detail = (
                "element is the document root"
                if passed
                else "element has a parent, so it is not the document root"
            )
            return ConditionCheck(expression, SELF_CONDITION, passed, detail)
        if leaf.ident == "scope":
            passed = element is self.scope
            detail = (
                "element is the scope of the selector"
                if passed
                else "element is not the scope of the selector"
            )
            return ConditionCheck(expression, SELF_CONDITION, passed, detail)
        if leaf.ident == "empty":
            has_children = len(element) > 0
            has_text = bool(element.text)
            passed = not has_children and not has_text
            if passed:
                detail = "element has no children and no text"
            elif has_children:
                detail = "element has child elements"
            else:
                detail = f"element has text {element.text!r}"
            return ConditionCheck(expression, SELF_CONDITION, passed, detail)
        if leaf.ident in {"first-child", "last-child", "only-child"}:
            return self._child_position_pseudo(expression, leaf.ident, element)
        return self._of_type_pseudo(expression, leaf.ident, element)

    def _child_position_pseudo(
        self, expression: str, ident: str, element: etree._Element
    ) -> ConditionCheck:
        preceding = len(_preceding_element_siblings(element))
        following = len(_following_element_siblings(element))
        if ident == "first-child":
            passed = preceding == 0
            detail = (
                "no element sibling precedes it"
                if passed
                else f"{preceding} element sibling(s) precede it"
            )
        elif ident == "last-child":
            passed = following == 0
            detail = (
                "no element sibling follows it"
                if passed
                else f"{following} element sibling(s) follow it"
            )
        else:
            passed = preceding == 0 and following == 0
            detail = (
                "it is the only element child of its parent"
                if passed
                else f"it has {preceding} preceding and {following} "
                "following element sibling(s)"
            )
        return ConditionCheck(expression, POSITION_CONDITION, passed, detail)

    def _of_type_pseudo(
        self, expression: str, ident: str, element: etree._Element
    ) -> ConditionCheck:
        position, total = self._type_position(element)
        tag = self.tag_name(element)
        if ident == "first-of-type":
            passed = position == 1
            detail = (
                f"first {tag!r} element child"
                if passed
                else f"{position - 1} {tag!r} element sibling(s) precede it"
            )
        elif ident == "last-of-type":
            passed = position == total
            detail = (
                f"last {tag!r} element child"
                if passed
                else f"{total - position} {tag!r} element sibling(s) follow it"
            )
        else:
            passed = total == 1
            detail = (
                "it is the only element of its type"
                if passed
                else f"there are {total} {tag!r} element siblings"
            )
        return ConditionCheck(expression, POSITION_CONDITION, passed, detail)

    def _type_position(self, element: etree._Element) -> tuple[int, int]:
        if element.getparent() is None:
            # The engine counts siblings on the parent axis; the document
            # root has none, which makes it the first and only element of
            # its type.
            return 1, 1
        siblings = _element_children(element)
        same_type = [
            sibling
            for sibling in siblings
            if self.tag_name(sibling) == self.tag_name(element)
        ]
        position = same_type.index(element) + 1
        return position, len(same_type)

    def _child_position(self, element: etree._Element) -> tuple[int, int]:
        if element.getparent() is None:
            # See _type_position(): a parentless root counts as position 1.
            return 1, 1
        siblings = _element_children(element)
        return siblings.index(element) + 1, len(siblings)

    def _check_function(
        self, leaf: Function, element: etree._Element
    ) -> ConditionCheck:
        expression = f":{leaf.name}({_series_text(leaf.arguments)})"
        if leaf.name in _NTH_FUNCTIONS:
            return self._check_nth(leaf, element, expression)
        if leaf.name == "contains":
            wanted = str(leaf.arguments[0].value)
            text = "".join(element.itertext())
            passed = wanted in text
            detail = (
                f"text contains {wanted!r}"
                if passed
                else f"text {text!r} does not contain {wanted!r}"
            )
            return ConditionCheck(expression, SELF_CONDITION, passed, detail)
        # :lang() relies on language axes that are easier to keep aligned
        # through the engine's own XPath condition.
        return self._engine_condition(expression, leaf, element)

    def _check_nth(
        self,
        leaf: Function,
        element: etree._Element,
        expression: str,
    ) -> ConditionCheck:
        a, b = parse_series(leaf.arguments)
        series = _series_text(leaf.arguments)
        of_type = leaf.name in _OF_TYPE_FUNCTIONS
        from_end = "last" in leaf.name
        if of_type:
            position, total = self._type_position(element)
            kind = f" {self.tag_name(element)!r}"
        else:
            position, total = self._child_position(element)
            kind = ""
        effective_position = total - position + 1 if from_end else position
        passed = _series_matches(a, b, effective_position)
        if passed:
            detail = (
                f"{effective_position}{kind} element sibling position "
                f"satisfies :{leaf.name}({series})"
            )
        else:
            detail = (
                f"{effective_position}{kind} element sibling position does "
                f"not satisfy :{leaf.name}({series})"
            )
        return ConditionCheck(expression, POSITION_CONDITION, passed, detail)

    def _check_negation(
        self, leaf: Negation, element: etree._Element
    ) -> ConditionCheck:
        expression = leaf.canonical()[len(leaf.selector.canonical()) :]
        excluded = self._matches_tree(leaf.subselector, element)
        passed = not excluded
        argument = leaf.subselector.canonical()
        detail = (
            f"none of the :not() argument {argument!r} matches the element"
            if passed
            else f":not() argument {argument!r} matches the element"
        )
        return ConditionCheck(expression, SELF_CONDITION, passed, detail)

    def _check_selector_list(
        self,
        leaf: Matching | SpecificityAdjustment,
        element: etree._Element,
    ) -> ConditionCheck:
        pseudo_name = "is" if isinstance(leaf, Matching) else "where"
        expression = leaf.canonical()[len(leaf.selector.canonical()) :]
        matching = [
            subselector.canonical()
            for subselector in leaf.selector_list
            if self._matches_tree(subselector, element)
        ]
        passed = bool(matching)
        alternatives = ", ".join(
            subselector.canonical() for subselector in leaf.selector_list
        )
        detail = (
            f":{pseudo_name}() alternative {matching[0]!r} matches"
            if passed
            else f"none of :{pseudo_name}({alternatives}) matches the element"
        )
        return ConditionCheck(expression, SELF_CONDITION, passed, detail)

    def _check_relation(
        self, leaf: Relation, element: etree._Element
    ) -> ConditionCheck:
        expression = leaf.canonical()[len(leaf.selector.canonical()) :]
        for combinator, subselector in leaf.arguments:
            combinator_value = combinator.value or " "
            candidates = self._relative_candidates(combinator_value, element)
            if any(
                self._matches_tree(subselector.parsed_tree, candidate)
                for candidate in candidates
            ):
                prefix = combinator_value.strip() or " "
                return ConditionCheck(
                    expression,
                    RELATION_CONDITION,
                    True,
                    f":has() argument {prefix!r}"
                    f"{subselector.canonical()} finds a matching node",
                )
        arguments = ", ".join(
            f"{(combinator.value or ' ').strip() or ' '}{subselector.canonical()}"
            for combinator, subselector in leaf.arguments
        )
        return ConditionCheck(
            expression,
            RELATION_CONDITION,
            False,
            f"no node relative to the element matches :has({arguments})",
        )

    def _relative_candidates(
        self, combinator: str, element: etree._Element
    ) -> list[etree._Element]:
        if combinator == " ":
            return list(element.iterdescendants())
        if combinator == ">":
            return [child for child in element if isinstance(child.tag, str)]
        following = _following_element_siblings(element)
        if combinator == "+":
            return following[:1]
        return following

    def _engine_condition(
        self,
        expression: str,
        leaf: Pseudo | Function,
        element: etree._Element,
    ) -> ConditionCheck:
        try:
            xpath_expr = self.translator.xpath(leaf)
        except ExpressionError as exc:
            raise UnsupportedSelectorSyntax(str(exc)) from exc
        if xpath_expr.condition:
            xpath = f"boolean(self::{xpath_expr.element}[{xpath_expr.condition}])"
        else:
            xpath = f"boolean(self::{xpath_expr.element})"
        passed = bool(_compile_boolean(xpath)(element))
        detail = (
            f"{expression} holds"
            if passed
            else f"{expression} does not hold for the element"
        )
        return ConditionCheck(expression, SELF_CONDITION, passed, detail)

    def _matches_tree(self, tree: Tree, element: etree._Element) -> bool:
        compounds: list[Tree] = []
        combinators: list[str] = []
        self._split_compounds(tree, compounds, combinators)
        return (
            self._match_path(compounds, combinators, len(compounds) - 1, element)
            is not None
        )


def explain_css_match(
    css: str,
    element: etree._Element,
    *,
    document_type: str = "html",
    scope: etree._Element | None = None,
) -> MatchExplanation:
    """Explain why *css* matches *element* or why it does not.

    :param css: a CSS selector list, as accepted by :meth:`Selector.css`.
    :param element: the lxml element to test.
    :param document_type: ``"html"`` (the default) or ``"xml"``. It controls
        case sensitivity and which translator is used, mirroring
        :class:`Selector`.
    :param scope: element acting as ``:scope``. Defaults to the root of the
        tree containing *element*.
    :raises SelectorSyntaxError: if *css* is not valid CSS selector syntax.
    :raises UnsupportedSelectorSyntax: if *css* uses syntax the CSS engine
        backing Parsel does not support, so no conclusion can be given.
    """
    if document_type not in {"html", "xml"}:
        raise ValueError(
            f"document_type must be 'html' or 'xml', got {document_type!r}"
        )
    if scope is None:
        scope = element
        while scope.getparent() is not None:
            scope = scope.getparent()
    return _Matcher(document_type, scope).explain(css, element)
