"""XPath and JMESPath selectors based on the lxml and jmespath Python
packages."""

from __future__ import annotations

import codecs
import copy
import json
import re
import typing
import warnings
from functools import lru_cache
from io import BytesIO
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    Self,
    SupportsIndex,
    TypeAlias,
    TypedDict,
    TypeVar,
)

import jmespath
from lxml import etree, html

from .csstranslator import GenericTranslator, HTMLTranslator
from .utils import extract_regex, flatten, iflatten, shorten

if TYPE_CHECKING:
    from collections.abc import Mapping
    from re import Pattern


_SelectorType = TypeVar("_SelectorType", bound="Selector")
_ParserType: TypeAlias = etree.XMLParser | etree.HTMLParser
# simplified _OutputMethodArg from types-lxml
_TostringMethodType = Literal[
    "html",
    "xml",
]


class CannotRemoveElementWithoutRoot(Exception):
    pass


class CannotRemoveElementWithoutParent(Exception):
    pass


class CannotDropElementWithoutParent(CannotRemoveElementWithoutParent):
    pass


class AbsoluteXPathWarning(UserWarning):
    """Emitted when an absolute XPath is used on a nested selector."""


class SafeXMLParser(etree.XMLParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("resolve_entities", False)
        super().__init__(*args, **kwargs)


class CTGroupValue(TypedDict):
    _parser: type[etree.XMLParser | html.HTMLParser]
    _csstranslator: GenericTranslator | HTMLTranslator
    _tostring_method: _TostringMethodType


_ctgroup: dict[str, CTGroupValue] = {
    "html": {
        "_parser": html.HTMLParser,
        "_csstranslator": HTMLTranslator(),
        "_tostring_method": "html",
    },
    "xml": {
        "_parser": SafeXMLParser,
        "_csstranslator": GenericTranslator(),
        "_tostring_method": "xml",
    },
}


def _xml_or_html(type_: str | None) -> str:
    return "xml" if type_ == "xml" else "html"


_ENCODING_ERROR_TYPES = frozenset(
    {etree.ErrorTypes.ERR_INVALID_CHAR, etree.ErrorTypes.ERR_INVALID_ENCODING}
)


def _has_encoding_error(parser: _ParserType) -> bool:
    return any(error.type in _ENCODING_ERROR_TYPES for error in parser.error_log)


def _root_from_decoded_body(
    body: bytes,
    parser_cls: type[_ParserType],
    base_url: str | None,
    huge_tree: bool,
    encoding: str,
) -> etree._Element:
    """Create a root node from *body*, with undecodable bytes replaced."""
    return create_root_node(
        body.decode(encoding, errors="replace"),
        parser_cls,
        base_url=base_url,
        huge_tree=huge_tree,
    )


_XML_DECLARATION = re.compile(r"[\s\ufeff]*<\?xml\s")


def _detect_xml_or_html(text: str) -> str:
    return "xml" if _XML_DECLARATION.match(text) else "html"


def create_root_node(
    text: str,
    parser_cls: type[_ParserType],
    base_url: str | None = None,
    huge_tree: bool = True,
    body: bytes = b"",
    encoding: str = "utf-8",
) -> etree._Element:
    """Create root node for text using given parser class."""
    if text:
        body = text.strip().replace("\x00", "").encode(encoding) or b"<html/>"
    elif codecs.lookup(encoding).name != "utf-8":
        # lxml handles bytes that the declared encoding cannot decode by
        # truncating the document there, and characters that span several
        # bytes, as in UTF-16, are broken by the null and whitespace stripping
        # below. Only UTF-8 bytes are passed as is, since lxml does report
        # invalid UTF-8, and it covers most input.
        return _root_from_decoded_body(body, parser_cls, base_url, huge_tree, encoding)
    else:
        body = body.replace(b"\x00", b"").strip() or b"<html/>"

    parser = parser_cls(recover=True, encoding=encoding, huge_tree=huge_tree)
    root = None
    try:
        root = etree.fromstring(body, parser=parser, base_url=base_url)
    except etree.XMLSyntaxError:
        if text or not _has_encoding_error(parser):
            raise
    if not text and _has_encoding_error(parser):
        # Invalid bytes reach the tree as is, and reading them raises
        # UnicodeDecodeError from lxml.
        return _root_from_decoded_body(body, parser_cls, base_url, huge_tree, encoding)
    if not huge_tree:
        for error in parser.error_log:
            if "use XML_PARSE_HUGE option" in error.message:
                warnings.warn(
                    "Input data is too big. Set huge_tree=True for huge_tree support.",
                    stacklevel=2,
                )
    if root is None:
        root = etree.fromstring(b"<html/>", parser=parser, base_url=base_url)
    return root


class SelectorList(list[_SelectorType]):
    """
    The :class:`SelectorList` class is a subclass of the builtin ``list``
    class, which provides a few additional methods.
    """

    @typing.overload
    def __getitem__(self, pos: SupportsIndex) -> _SelectorType:
        pass

    @typing.overload
    def __getitem__(self, pos: slice) -> SelectorList[_SelectorType]:
        pass

    def __getitem__(
        self, pos: SupportsIndex | slice
    ) -> _SelectorType | SelectorList[_SelectorType]:
        o = super().__getitem__(pos)
        if isinstance(pos, slice):
            return self.__class__(typing.cast("SelectorList[_SelectorType]", o))
        return typing.cast("_SelectorType", o)

    def __getstate__(self) -> None:
        raise TypeError("can't pickle SelectorList objects")

    def jmespath(self, query: str, **kwargs: Any) -> SelectorList[_SelectorType]:
        """
        Call the ``.jmespath()`` method for each element in this list and return
        their results flattened as another :class:`SelectorList`.

        ``query`` is the same argument as the one in :meth:`Selector.jmespath`.

        Any additional named arguments are passed to the underlying
        ``jmespath.search`` call, e.g.::

            selector.jmespath('author.name', options=jmespath.Options(dict_cls=collections.OrderedDict))
        """
        return self.__class__(flatten([x.jmespath(query, **kwargs) for x in self]))

    def jsonpath(self, query: str) -> SelectorList[_SelectorType]:
        """
        Call the ``.jsonpath()`` method for each element in this list and return
        their results flattened as another :class:`SelectorList`.

        .. versionadded:: VERSION

        ``query`` is the same argument as the one in :meth:`Selector.jsonpath`.
        """
        return self.__class__(flatten([x.jsonpath(query) for x in self]))

    def xpath(
        self,
        xpath: str,
        namespaces: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> SelectorList[_SelectorType]:
        """
        Call the ``.xpath()`` method for each element in this list and return
        their results flattened as another :class:`SelectorList`.

        ``xpath`` is the same argument as the one in :meth:`Selector.xpath`

        ``namespaces`` is an optional ``prefix: namespace-uri`` mapping (dict)
        for additional prefixes to those registered with ``register_namespace(prefix, uri)``.
        Contrary to ``register_namespace()``, these prefixes are not
        saved for future calls.

        Any additional named arguments can be used to pass values for XPath
        variables in the XPath expression, e.g.::

            selector.xpath('//a[href=$url]', url="http://www.example.com")
        """
        if xpath[:1] == "/":
            for sel in self:
                if sel._warn_if_nested(xpath, stacklevel=3):
                    break
        return self.__class__(
            flatten([x._xpath(xpath, namespaces, kwargs) for x in self])
        )

    def css(self, query: str) -> SelectorList[_SelectorType]:
        """
        Call the ``.css()`` method for each element in this list and return
        their results flattened as another :class:`SelectorList`.

        ``query`` is the same argument as the one in :meth:`Selector.css`
        """
        return self.__class__(flatten([x.css(query) for x in self]))

    def re(self, regex: str | Pattern[str], replace_entities: bool = True) -> list[str]:
        """
        Call the ``.re()`` method for each element in this list and return
        their results flattened, as a list of strings.

        By default, character entity references are replaced by their
        corresponding character (except for ``&amp;`` and ``&lt;``).
        Passing ``replace_entities`` as ``False`` switches off these
        replacements.
        """
        return flatten([x.re(regex, replace_entities=replace_entities) for x in self])

    @typing.overload
    def re_first(
        self,
        regex: str | Pattern[str],
        default: None = None,
        replace_entities: bool = True,
    ) -> str | None:
        pass

    @typing.overload
    def re_first(
        self,
        regex: str | Pattern[str],
        default: str,
        replace_entities: bool = True,
    ) -> str:
        pass

    def re_first(
        self,
        regex: str | Pattern[str],
        default: str | None = None,
        replace_entities: bool = True,
    ) -> str | None:
        """
        Call the ``.re()`` method for elements in this list and return the
        first matching result as a string. If the list is empty or the
        regex doesn't match anything, return the default value (``None`` if
        the argument is not provided).

        By default, character entity references are replaced by their
        corresponding character (except for ``&amp;`` and ``&lt;``).
        Passing ``replace_entities`` as ``False`` switches off these
        replacements.
        """
        for el in iflatten(
            x.re(regex, replace_entities=replace_entities) for x in self
        ):
            return typing.cast("str", el)
        return default

    def getall(self) -> list[str]:
        """
        Call the ``.get()`` method for each element in this list and return
        their results flattened, as a list of strings.
        """
        return [x.get() for x in self]

    extract = getall

    @typing.overload
    def get(self, default: None = None) -> str | None:
        pass

    @typing.overload
    def get(self, default: str) -> str:
        pass

    def get(self, default: str | None = None) -> Any:
        """
        Return the result of ``.get()`` for the first element in this list.
        If the list is empty, return the default value.
        """
        for x in self:
            return x.get()
        return default

    extract_first = get

    @property
    def attrib(self) -> Mapping[str, str]:
        """Return the attributes dictionary for the first element.
        If the list is empty, return an empty dict.
        """
        for x in self:
            return x.attrib
        return {}

    def drop(self) -> None:
        """
        Drop matched nodes from the parent for each element in this list.
        """
        for x in self:
            x.drop()


_NOT_SET = object()


def _get_root_from_text(text: str, *, type_: str, **lxml_kwargs: Any) -> etree._Element:
    return create_root_node(text, _ctgroup[type_]["_parser"], **lxml_kwargs)


def _get_root_and_type_from_bytes(
    body: bytes,
    encoding: str,
    *,
    input_type: str | None,
    **lxml_kwargs: Any,
) -> tuple[Any, str]:
    if input_type == "text":
        return body.decode(encoding), input_type
    if input_type in ("json", None) and codecs.lookup(encoding).name == "utf-8":
        try:
            data = json.load(BytesIO(body))
        except ValueError:
            data = _NOT_SET
        if data is not _NOT_SET and (input_type == "json" or _is_json_document(data)):
            return data, "json"
    if input_type == "json":
        return None, "json"
    assert input_type in ("html", "xml", None)  # nosec
    type_ = input_type or _detect_xml_or_html(body[:256].decode(encoding, "ignore"))
    root = create_root_node(
        text="",
        body=body,
        encoding=encoding,
        parser_cls=_ctgroup[type_]["_parser"],
        **lxml_kwargs,
    )
    return root, type_


def _get_root_and_type_from_text(
    text: str, *, input_type: str | None, **lxml_kwargs: Any
) -> tuple[Any, str]:
    if input_type == "text":
        return text, input_type
    if input_type in ("json", None):
        try:
            data = json.loads(text)
        except ValueError:
            data = _NOT_SET
        if data is not _NOT_SET and (input_type == "json" or _is_json_document(data)):
            return data, "json"
    if input_type == "json":
        return None, "json"
    assert input_type in ("html", "xml", None)  # nosec
    type_ = input_type or _detect_xml_or_html(text)
    root = _get_root_from_text(text, type_=type_, **lxml_kwargs)
    return root, type_


# lxml registers the functions of these namespaces on every call of an
# evaluator that maps them, and with libxml2 2.13+ every registration after the
# first adds an entry to the evaluator's error log, which cannot be cleared.
_EXSLT_NAMESPACES = frozenset(
    f"http://exslt.org/{name}"
    for name in ("dates-and-times", "math", "sets", "strings")
)


@lru_cache(maxsize=2048)
def _compile_xpath(
    query: str, namespaces: tuple[tuple[str, str], ...], smart_strings: bool
) -> etree.XPath:
    return etree.XPath(query, namespaces=dict(namespaces), smart_strings=smart_strings)


def _get_xpath_evaluator(
    query: str, namespaces: dict[str, str], smart_strings: bool
) -> etree.XPath:
    namespaces = {
        prefix: uri
        for prefix, uri in namespaces.items()
        if uri not in _EXSLT_NAMESPACES
        or not isinstance(query, str)
        or f"{prefix}:" in query
    }
    if _EXSLT_NAMESPACES.intersection(namespaces.values()):
        return etree.XPath(query, namespaces=namespaces, smart_strings=smart_strings)
    return _compile_xpath(query, tuple(sorted(namespaces.items())), smart_strings)


def _get_root_type(root: Any, *, input_type: str | None) -> str:
    if isinstance(root, etree._Element):
        if input_type in {"json", "text"}:
            raise ValueError(
                f"Selector got an lxml.etree._Element object as root, "
                f"and {input_type!r} as type."
            )
        return _xml_or_html(input_type)
    return input_type or "json"


def _is_json_document(data: Any) -> bool:
    """Return whether *data* is a JSON object or array.

    Scalar JSON values are valid JSON documents as well, but they are also
    valid text and HTML, which is what they usually are when type detection
    gets to see them, so type detection ignores them.
    """
    return isinstance(data, (dict, list))


def _load_json_or_none(text: str | None) -> Any:
    try:
        return json.loads(text)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class JSONPathError(ValueError):
    """Raised when a JSONPath query is invalid or cannot be evaluated.

    .. versionadded:: VERSION
    """


@lru_cache(maxsize=1)
def _jsonpath_backend() -> tuple[Any, Any, type[Exception]]:
    """Return a query compiler, a function that runs a compiled query on data,
    and the base error class of the underlying JSONPath package."""
    try:
        import jsonpath_rfc9535  # noqa: PLC0415
    except ImportError:
        pass
    else:
        rfc9535_env = jsonpath_rfc9535.JSONPathEnvironment()
        return (
            rfc9535_env.compile,
            lambda compiled, data: compiled.find(data).values(),
            jsonpath_rfc9535.JSONPathError,
        )
    try:
        import jsonpath  # noqa: PLC0415
    except ImportError:
        raise ImportError(
            "You need to install jsonpath-rfc9535 or python-jsonpath, e.g. "
            "pip install parsel[jsonpath-rfc9535], to use JSONPath "
            "expressions."
        )
    # strict mode restricts the syntax to RFC 9535, leaving out the extensions
    # that python-jsonpath enables by default.
    jsonpath_env = jsonpath.JSONPathEnvironment(strict=True)
    return (
        jsonpath_env.compile,
        lambda compiled, data: compiled.findall(data),
        jsonpath.JSONPathError,
    )


@lru_cache(maxsize=512)
def _compile_jsonpath(query: str) -> Any:
    compile_query, run_query, backend_error = _jsonpath_backend()
    try:
        compiled = compile_query(query)
    except backend_error as error:
        raise JSONPathError(str(error))

    def find(data: Any) -> Any:
        try:
            return run_query(compiled, data)
        except backend_error as error:
            raise JSONPathError(str(error))

    return find


class _QueryView:
    """Mutation lineage shared by a query object and its nested selectors.

    Every call to :meth:`SelectorDocument.selector` creates a separate view.
    The first mutation deep-copies the document. The view keeps the map of
    old nodes to copied nodes, and each query object relocates its own root
    lazily on the next access, before the tree is changed. Other views keep
    pointing at the original document and cannot see the mutation.

    Nodes are used as map keys rather than their ``id()``: lxml recycles
    Python element wrappers, while node hashing and equality follow the
    underlying C node, so element keys stay valid for the view's lifetime.
    """

    __slots__ = ("document", "migration")

    def __init__(self, document: SelectorDocument) -> None:
        self.document = document
        #: old node -> equivalent node in the mutation branch, if branched
        self.migration: dict[etree._Element, etree._Element] | None = None


class SelectorDocument:
    """A parsed document that several independent query objects can share.

    The input is parsed only once, no matter how many :class:`Selector`
    query objects are created with :meth:`selector`. Each query object keeps
    its own namespaces, XPath variables and query options; only the parsed
    tree itself is shared.

    Query objects returned by :meth:`selector` are read-only views. Calling a
    method that modifies the tree, such as :meth:`Selector.drop` or
    :meth:`Selector.remove_namespaces`, transparently copies the parsed tree
    (copy-on-write): the mutation only affects the query object that asked
    for it and query objects derived from it, while every other query object
    keeps seeing the original document.

    The constructor accepts the same ``text``, ``body``, ``encoding``,
    ``type``, ``root``, ``base_url`` and ``huge_tree`` arguments as
    :class:`Selector`.
    """

    __slots__ = (
        "__weakref__",
        "_branch",
        "_huge_tree",
        "_root",
        "_text",
        "type",
    )

    #: Query class built by :meth:`selector`; override in a subclass to use
    #: a custom :class:`Selector` subclass.
    selector_cls: type[Selector]

    def __init__(
        self,
        text: str | None = None,
        type: str | None = None,  # noqa: A002
        body: bytes | bytearray = b"",
        encoding: str = "utf-8",
        root: Any | None = _NOT_SET,
        base_url: str | None = None,
        huge_tree: bool = True,
    ) -> None:
        if type not in ("html", "json", "text", "xml", None):
            raise ValueError(f"Invalid type: {type}")

        if text is not None:
            if root is not _NOT_SET:
                warnings.warn(
                    "SelectorDocument got both text and root, root is being ignored.",
                    stacklevel=2,
                )
            if not isinstance(text, str):
                msg = f"text argument should be of type str, got {text.__class__}"
                raise TypeError(msg)
            parsed_root, parsed_type = _get_root_and_type_from_text(
                text,
                input_type=type,
                base_url=base_url,
                huge_tree=huge_tree,
            )
        elif body:
            if not isinstance(body, (bytes, bytearray)):
                msg = (
                    "body argument should be of type bytes or bytearray, got "
                    f"{body.__class__}"
                )
                raise TypeError(msg)
            parsed_root, parsed_type = _get_root_and_type_from_bytes(
                body=bytes(body),
                encoding=encoding,
                input_type=type,
                base_url=base_url,
                huge_tree=huge_tree,
            )
        elif root is _NOT_SET:
            raise ValueError(
                "SelectorDocument needs text, body, or root arguments"
            )
        else:
            parsed_root = root
            parsed_type = _get_root_type(root, input_type=type)

        self._root = parsed_root
        self.type = parsed_type
        self._text = text
        self._huge_tree = huge_tree
        self._branch = False

    @classmethod
    def _from_parsed(
        cls,
        root: Any,
        type_: str,
        text: str | None,
        huge_tree: bool,
    ) -> Self:
        """Wrap an already parsed root without parsing again."""
        document = cls.__new__(cls)
        document._root = root
        document.type = type_
        document._text = text
        document._huge_tree = huge_tree
        document._branch = False
        return document

    def selector(
        self,
        namespaces: Mapping[str, str] | None = None,
        _expr: str | None = None,
    ) -> Selector:
        """Return an independent query object over this parsed document.

        ``namespaces`` is private to the returned query object and does not
        affect the document or other query objects.
        """
        return self.selector_cls(
            _view=_QueryView(self), namespaces=namespaces, _expr=_expr
        )

    @property
    def root(self) -> Any:
        """The parsed root object (an lxml element for HTML/XML)."""
        return self._root

    def copy_for_mutation(self) -> SelectorDocument:
        """Return a private, deep-copied branch of this document."""
        if isinstance(self._root, etree._Element):
            # Copy the whole underlying tree so that parent chains survive,
            # then anchor the branch at the equivalent of this document root.
            tree = self._root.getroottree()
            full_copy = copy.deepcopy(tree.getroot())
            if self._root.getparent() is None:
                branch_root = full_copy
            else:
                path = tree.getpath(self._root)
                branch_root = full_copy.getroottree().xpath(path)[0]
        else:
            branch_root = copy.deepcopy(self._root)
        branch = self._from_parsed(
            branch_root, self.type, self._text, self._huge_tree
        )
        branch._branch = True
        return branch


class Selector:
    """Wrapper for input data in HTML, JSON, or XML format, that allows
    selecting parts of it using selection expressions.

    You can write selection expressions in CSS or XPath for HTML and XML
    inputs, or in JMESPath or JSONPath for JSON inputs.

    ``text`` is an ``str`` object.

    ``body`` is a ``bytes`` or ``bytearray`` object. It can be used together with the
    ``encoding`` argument instead of the ``text`` argument.

    ``type`` defines the selector type. It can be ``"html"``, ``"json"``,
    ``"xml"`` or ``"text"``. If not specified, the input is handled as
    ``"json"`` if it is a JSON object or array, as ``"xml"`` if it starts with
    an XML declaration, and as ``"html"`` otherwise.

    ``base_url`` allows setting a URL for the document. This is needed when looking up external entities with relative paths.
    See the documentation for :func:`lxml.etree.fromstring` for more information.

    ``huge_tree`` controls the lxml/libxml2 feature that forbids parsing
    certain large documents to protect from possible memory exhaustion. The
    argument is ``True`` by default, which disables the protection to allow
    parsing such documents. Set it to ``False`` if you want to enable the
    protection. See `this lxml FAQ entry
    <https://lxml.de/FAQ.html#is-lxml-vulnerable-to-xml-bombs>`_ for more
    information.
    """

    __slots__ = [
        "__weakref__",
        "_expr",
        "_root",
        "_view",
        "namespaces",
        "type",
    ]

    _default_namespaces = {
        "re": "http://exslt.org/regular-expressions",
        # supported in libxslt:
        # set:difference
        # set:has-same-node
        # set:intersection
        # set:leading
        # set:trailing
        "set": "http://exslt.org/sets",
    }
    _lxml_smart_strings = False
    selectorlist_cls = SelectorList["Selector"]

    def __init__(
        self,
        text: str | None = None,
        type: str | None = None,  # noqa: A002
        body: bytes | bytearray = b"",
        encoding: str = "utf-8",
        namespaces: Mapping[str, str] | None = None,
        root: Any | None = _NOT_SET,
        base_url: str | None = None,
        _expr: str | None = None,
        huge_tree: bool = True,
        _view: _QueryView | None = None,
    ) -> None:
        if _view is not None:
            # Query view created by a shared SelectorDocument: no parsing.
            self._view = _view
            self._root = _view.document.root
            self.type = _view.document.type
        else:
            if type not in ("html", "json", "text", "xml", None):
                raise ValueError(f"Invalid type: {type}")

            if text is not None:
                if root is not _NOT_SET:
                    warnings.warn(
                        "Selector got both text and root, root is being ignored.",
                        stacklevel=2,
                    )
                if not isinstance(text, str):
                    msg = f"text argument should be of type str, got {text.__class__}"
                    raise TypeError(msg)

                root, type = _get_root_and_type_from_text(  # noqa: A001
                    text,
                    input_type=type,
                    base_url=base_url,
                    huge_tree=huge_tree,
                )
            elif body:
                if not isinstance(body, (bytes, bytearray)):
                    msg = (
                        "body argument should be of type bytes or bytearray, got "
                        f"{body.__class__}"
                    )
                    raise TypeError(msg)
                root, type = _get_root_and_type_from_bytes(  # noqa: A001
                    body=bytes(body),
                    encoding=encoding,
                    input_type=type,
                    base_url=base_url,
                    huge_tree=huge_tree,
                )
            elif root is _NOT_SET:
                raise ValueError("Selector needs text, body, or root arguments")
            else:
                type = _get_root_type(root, input_type=type)  # noqa: A001

            self._root = root
            self.type = type
            # A standalone selector owns a private document over its root.
            self._view = _QueryView(
                SelectorDocument._from_parsed(root, self.type, text, huge_tree)
            )

        self.namespaces = dict(self._default_namespaces)
        if namespaces is not None:
            self.namespaces.update(namespaces)

        self._expr = _expr

    @property
    def root(self) -> Any:
        """The node this query object points at in its current document.

        After a copy-on-write mutation the node is relocated lazily to the
        equivalent node in the mutation branch on first access.
        """
        migration = self._view.migration
        root = self._root
        if migration is not None and isinstance(root, etree._Element):
            # Branch nodes are not keys of the map, so this becomes a no-op
            # once the selector has been relocated.
            relocated = migration.get(root)
            if relocated is not None:
                self._root = relocated
                return relocated
        return root

    @root.setter
    def root(self, value: Any) -> None:
        self._root = value

    @property
    def document(self) -> SelectorDocument:
        """The parsed document this query object currently reads from.

        Query objects created from the same :class:`SelectorDocument` start
        on that document; a mutation moves the mutating lineage onto a
        private copy-on-write branch.
        """
        return self._view.document

    @property
    def _text(self) -> str | None:
        # Kept for backward compatibility with code reading this private
        # attribute; the text now lives on the shared document.
        return self._view.document._text

    def _child_selector(
        self, root: Any, query: str, type_: str | None = None
    ) -> Self:
        """Build a nested query object in the same mutation lineage."""
        child_type = type_ or _xml_or_html(self.type)
        if type(self).__init__ is Selector.__init__:
            # Fast path: allocate directly and skip the parsing constructor
            # and the default-namespace dictionary that would be replaced.
            child = self.__class__.__new__(self.__class__)
            child._view = self._view
            child._root = root
            child.type = child_type
            child.namespaces = dict(self.namespaces)
            child._expr = query
            return child
        # Subclasses with a custom __init__ keep using the public root= path,
        # then join the shared mutation lineage afterwards.
        child = self.__class__(
            root=root,
            _expr=query,
            namespaces=self.namespaces,
            type=child_type,
        )
        child._view = self._view
        return child

    def _mutating_document(self) -> SelectorDocument:
        """Return a document this query object may modify.

        The first mutation of a lineage that shares an unmodified document
        creates a private copy-on-write branch and records the old-to-new
        node map, so each query object relocates lazily on its next
        :attr:`root` access. Query objects in other lineages keep reading
        the original tree.
        """
        document = self._view.document
        if not document._branch:
            branch = document.copy_for_mutation()
            old_root = document.root
            if isinstance(old_root, etree._Element):
                self._view.migration = dict(
                    zip(old_root.iter(), branch.root.iter(), strict=True)
                )
            self._view.document = branch
            document = branch
        return document

    def __getstate__(self) -> Any:
        raise TypeError("can't pickle Selector objects")

    def _get_root(
        self,
        text: str = "",
        base_url: str | None = None,
        huge_tree: bool = True,
        type_: str | None = None,
        body: bytes = b"",
        encoding: str = "utf-8",
    ) -> etree._Element:
        return create_root_node(
            text,
            body=body,
            encoding=encoding,
            parser_cls=_ctgroup[type_ or self.type]["_parser"],
            base_url=base_url,
            huge_tree=huge_tree,
        )

    def _json_data(self) -> Any:
        if isinstance(self.root, str):
            return _load_json_or_none(self.root)
        if self.type == "json":
            return self.root
        assert self.type in {"html", "xml"}  # nosec
        return _load_json_or_none(self.root.text)

    def _json_selector_list(self, values: Any, query: str) -> SelectorList[Self]:
        def make_selector(x: Any) -> Selector:  # closure function
            if isinstance(x, str):
                return self.__class__(text=x, _expr=query, type="text")
            return self.__class__(root=x, _expr=query)

        return typing.cast(
            "SelectorList[Self]",
            self.selectorlist_cls([make_selector(x) for x in values]),
        )

    def jmespath(
        self,
        query: str,
        **kwargs: Any,
    ) -> SelectorList[Self]:
        """
        Find objects matching the JMESPath ``query`` and return the result as a
        :class:`SelectorList` instance with all elements flattened. List
        elements implement :class:`Selector` interface too.

        ``query`` is a string containing the `JMESPath
        <https://jmespath.org/>`_ query to apply.

        Any additional named arguments are passed to the underlying
        ``jmespath.search`` call, e.g.::

            selector.jmespath('author.name', options=jmespath.Options(dict_cls=collections.OrderedDict))
        """
        result = jmespath.search(query, self._json_data(), **kwargs)
        if result is None:
            result = []
        elif not isinstance(result, list):
            result = [result]
        return self._json_selector_list(result, query)

    def jsonpath(self, query: str) -> SelectorList[Self]:
        """
        Find objects matching the JSONPath ``query`` and return the result as a
        :class:`SelectorList` instance with all elements flattened. List
        elements implement :class:`Selector` interface too.

        .. versionadded:: VERSION

        ``query`` is a string containing the `JSONPath
        <https://datatracker.ietf.org/doc/html/rfc9535>`_ query to apply.
        Invalid queries raise :class:`JSONPathError`.

        Requires a JSONPath package, see :ref:`jsonpath-install`.
        """
        return self._json_selector_list(
            _compile_jsonpath(query)(self._json_data()), query
        )

    def xpath(
        self,
        query: str,
        namespaces: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> SelectorList[Self]:
        """
        Find nodes matching the xpath ``query`` and return the result as a
        :class:`SelectorList` instance with all elements flattened. List
        elements implement :class:`Selector` interface too.

        ``query`` is a string containing the XPATH query to apply.

        ``namespaces`` is an optional ``prefix: namespace-uri`` mapping (dict)
        for additional prefixes to those registered with ``register_namespace(prefix, uri)``.
        Contrary to ``register_namespace()``, these prefixes are not
        saved for future calls.

        Any additional named arguments can be used to pass values for XPath
        variables in the XPath expression, e.g.::

            selector.xpath('//a[href=$url]', url="http://www.example.com")
        """
        if query[:1] == "/":
            self._warn_if_nested(query, stacklevel=3)
        return self._xpath(query, namespaces, kwargs)

    def _warn_if_nested(self, xpath: str, *, stacklevel: int) -> bool:
        if isinstance(self.root, etree._Element) and self.root.getparent() is not None:
            warnings.warn(
                f"Absolute XPath {xpath!r} used on a nested selector",
                AbsoluteXPathWarning,
                stacklevel=stacklevel,
            )
            return True
        return False

    def _xpath(
        self,
        query: str,
        namespaces: Mapping[str, str] | None,
        kwargs: dict[str, Any],
    ) -> SelectorList[Self]:
        if self.type not in ("html", "xml", "text"):
            raise ValueError(f"Cannot use xpath on a Selector of type {self.type!r}")
        if self.type in ("html", "xml"):
            root = self.root
            if not hasattr(root, "xpath"):
                if isinstance(root, str) and query.strip() == ".":
                    return typing.cast(
                        "SelectorList[Self]",
                        self.selectorlist_cls(
                            [self._child_selector(self.root, query, self.type)]
                        ),
                    )
                return typing.cast("SelectorList[Self]", self.selectorlist_cls([]))
        else:
            root = self._get_root(self._view.document._text or "", type_="html")

        nsp = dict(self.namespaces)
        if namespaces is not None:
            nsp.update(namespaces)
        try:
            xpathev = _get_xpath_evaluator(query, nsp, self._lxml_smart_strings)
            result = xpathev(root, **kwargs)
        except etree.XPathError as exc:
            raise ValueError(f"XPath error: {exc} in {query}")

        if not isinstance(result, list):
            result = [result]

        result = [self._child_selector(node, query) for node in result]
        return typing.cast("SelectorList[Self]", self.selectorlist_cls(result))

    def css(self, query: str) -> SelectorList[Self]:
        """
        Apply the given CSS selector and return a :class:`SelectorList` instance.

        ``query`` is a string containing the CSS selector to apply.

        In the background, CSS queries are translated into XPath queries using
        `cssselect`_ library and run ``.xpath()`` method.

        .. _cssselect: https://pypi.python.org/pypi/cssselect/
        """
        if self.type not in ("html", "xml", "text"):
            raise ValueError(f"Cannot use css on a Selector of type {self.type!r}")
        return self.xpath(self._css2xpath(query))

    def _css2xpath(self, query: str) -> str:
        type_ = _xml_or_html(self.type)
        return _ctgroup[type_]["_csstranslator"].css_to_xpath(query)

    def re(self, regex: str | Pattern[str], replace_entities: bool = True) -> list[str]:
        """
        Apply the given regex and return a list of strings with the
        matches.

        ``regex`` can be either a compiled regular expression or a string which
        will be compiled to a regular expression using ``re.compile(regex)``.

        By default, character entity references are replaced by their
        corresponding character (except for ``&amp;`` and ``&lt;``).
        Passing ``replace_entities`` as ``False`` switches off these
        replacements.
        """
        data = self.get()
        return extract_regex(regex, data, replace_entities=replace_entities)

    @typing.overload
    def re_first(
        self,
        regex: str | Pattern[str],
        default: None = None,
        replace_entities: bool = True,
    ) -> str | None:
        pass

    @typing.overload
    def re_first(
        self,
        regex: str | Pattern[str],
        default: str,
        replace_entities: bool = True,
    ) -> str:
        pass

    def re_first(
        self,
        regex: str | Pattern[str],
        default: str | None = None,
        replace_entities: bool = True,
    ) -> str | None:
        """
        Apply the given regex and return the first string which matches. If
        there is no match, return the default value (``None`` if the argument
        is not provided).

        By default, character entity references are replaced by their
        corresponding character (except for ``&amp;`` and ``&lt;``).
        Passing ``replace_entities`` as ``False`` switches off these
        replacements.
        """
        return next(
            iflatten(self.re(regex, replace_entities=replace_entities)),
            default,
        )

    def get(self) -> Any:
        """
        Serialize and return the matched nodes.

        For HTML and XML, the result is always a string.
        """
        if self.type in ("text", "json"):
            return self.root
        try:
            return etree.tostring(
                self.root,
                method=_ctgroup[self.type]["_tostring_method"],
                encoding="unicode",
                with_tail=False,
            )
        except (AttributeError, TypeError):
            if self.root is True:
                return "1"
            if self.root is False:
                return "0"
            return str(self.root)

    extract = get

    def getall(self) -> list[str]:
        """
        Serialize and return the matched node in a 1-element list of strings.
        """
        return [self.get()]

    def register_namespace(self, prefix: str, uri: str) -> None:
        """
        Register the given namespace to be used in this :class:`Selector`.
        Without registering namespaces you can't select or extract data from
        non-standard namespaces. See :ref:`selector-examples-xml`.
        """
        self.namespaces[prefix] = uri

    def remove_namespaces(self) -> None:
        """
        Remove all namespaces, allowing to traverse the document using
        namespace-less xpaths. See :ref:`removing-namespaces`.
        For JSON selectors, this method does nothing.

        This modifies a private copy-on-write branch of the document, so
        other query objects sharing the same parsed document are unaffected.
        """
        if self.type == "json":
            return

        self._mutating_document()
        for el in self.root.iter("*"):
            if el.tag.startswith("{"):
                el.tag = el.tag.split("}", 1)[1]
            # loop on element attributes also
            for an in el.attrib:
                if an.startswith("{"):
                    el.attrib[an.split("}", 1)[1]] = el.attrib.pop(an)
        # remove namespace declarations
        etree.cleanup_namespaces(self.root)

    def drop(self) -> None:
        """
        Drop matched nodes from the parent element.

        The first drop on a query object sharing a document creates a
        private copy-on-write branch: other query objects keep seeing the
        original tree, while this query object and ones derived from it see
        the dropped node.
        """
        try:
            parent = self.root.getparent()
        except AttributeError:
            # 'str' object has no attribute 'getparent'
            raise CannotRemoveElementWithoutRoot(
                "The node you're trying to drop has no root, "
                "are you trying to drop a pseudo-element? "
                "Try to use 'li' as a selector instead of 'li::text' or "
                "'//li' instead of '//li/text()', for example."
            )

        no_parent_message = (
            "The node you're trying to remove has no parent, "
            "are you trying to remove a root element?"
        )
        if parent is None:
            raise CannotDropElementWithoutParent(no_parent_message)

        # Validate first so an impossible drop never creates a private copy.
        self._mutating_document()
        root = self.root
        try:
            if self.type == "xml":
                # Unlike HtmlElement.drop_tree(), _Element.remove() also
                # removes the tail text, so we need to preserve it manually
                if root.tail:
                    previous = root.getprevious()
                    if previous is not None:
                        previous.tail = (previous.tail or "") + root.tail
                    else:
                        root.getparent().text = (
                            root.getparent().text or ""
                        ) + root.tail
                root.getparent().remove(root)
            else:
                typing.cast("html.HtmlElement", root).drop_tree()
        except (AttributeError, AssertionError):
            # 'NoneType' object has no attribute 'drop_tree'
            raise CannotDropElementWithoutParent(no_parent_message)

    @property
    def attrib(self) -> dict[str, str]:
        """
        Return the attributes dictionary for underlying element.
        For JSON selectors, return an empty dict.
        """
        if self.type == "json":
            return {}

        return dict(self.root.attrib)

    def __bool__(self) -> bool:
        """
        Return ``True`` if there is any real content selected or ``False``
        otherwise.  In other words, the boolean value of a :class:`Selector` is
        given by the contents it selects.
        """
        return bool(self.get())

    __nonzero__ = __bool__

    def __str__(self) -> str:
        return str(self.get())

    def __repr__(self) -> str:
        data = repr(shorten(str(self.get()), width=40))
        return f"<{type(self).__name__} query={self._expr!r} data={data}>"


SelectorDocument.selector_cls = Selector
