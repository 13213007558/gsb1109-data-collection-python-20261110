"""Tabular (CSV) import and export of items, with a companion schema file.

:func:`export_csv` writes a batch of items to a CSV file plus a JSON schema
sidecar (``<name>.schema.json``) that describes the columns and the item
type, so that :func:`import_csv` can validate the data back into items.

Cell representation
-------------------

Each CSV column maps to a top-level item field. Values are represented as
follows:

* missing field (not present in the item): empty cell
* ``None``: ``null``
* ``True`` / ``False``: ``true`` / ``false``
* numbers: their JSON representation (e.g. ``42``, ``9.5``)
* nested items, dicts and lists: their JSON representation
  (e.g. ``{"city": "Berlin"}``, ``["a", "b"]``)
* strings: written verbatim, unless they are empty or would be misread as
  one of the representations above (e.g. the string ``"null"`` or
  ``"42"``), in which case they are written as a JSON string
  (e.g. ``"null"``, ``"42"``)

Because nested data is kept as JSON inside a single cell and no key
flattening is used, field names that contain commas, dots, quotes or any
other delimiter never clash with the encoding.

Safe display mode
-----------------

Spreadsheet software may interpret cells whose text starts with ``=``,
``+``, ``-``, ``@``, a tab or a carriage return as formulas, which is both
a usability and a security concern (CSV formula injection). Exporting with
``safe=True`` forces such strings to be written as JSON strings (i.e.
wrapped in double quotes), so spreadsheet software displays them as text.
The change is fully reversible: importing reads JSON strings back as
strings, no matter which mode was used to export.
"""

from __future__ import annotations

import contextlib
import csv
import json
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from itemadapter.adapter import ItemAdapter

__all__ = [
    "CellError",
    "TabularImportError",
    "export_csv",
    "import_csv",
]

FORMAT_NAME = "itemadapter-tabular"
FORMAT_VERSION = 1

_MISSING = object()

# Leading characters that spreadsheet software may interpret as the start of
# a formula.
_FORMULA_PREFIXES = frozenset("=+-@\t\r")

_JSON_TYPE_CHECKS = {
    "null": lambda value: value is None,
    "boolean": lambda value: isinstance(value, bool),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "string": lambda value: isinstance(value, str),
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, dict),
}


@dataclass
class CellError:
    """A single problem found while importing a CSV file.

    ``row`` is the 1-based line number in the CSV file (the header is line
    1, so the first data row is line 2). ``column`` is the 1-based column
    index. Either may be ``None`` for issues that are not tied to a specific
    row or column.
    """

    row: int | None
    column: int | None
    field: str | None
    message: str

    def __str__(self) -> str:
        location = []
        if self.row is not None:
            location.append(f"row {self.row}")
        if self.column is not None:
            location.append(f"column {self.column}")
        if self.field is not None:
            location.append(f"field {self.field!r}")
        prefix = ", ".join(location)
        return f"{prefix}: {self.message}" if prefix else self.message


class TabularImportError(ValueError):
    """Raised when a CSV file cannot be imported.

    The ``errors`` attribute lists every problem found. No item is created
    at all unless the whole file is valid, so a batch is never partially
    imported.
    """

    def __init__(self, errors: list[CellError]) -> None:
        self.errors = list(errors)
        shown = "; ".join(str(error) for error in self.errors[:5])
        remaining = len(self.errors) - 5
        if remaining > 0:
            shown = f"{shown}; … and {remaining} more"
        super().__init__(f"{len(self.errors)} error(s) importing CSV data: {shown}")


def _decode_cell(text: str) -> Any:
    """Decode a non-empty CSV cell into a Python value."""
    if text == "null":
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    if text[0] in '"[{':
        return json.loads(text)
    try:
        value = json.loads(text)
    except ValueError:
        return text
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return text


def _encode_string(text: str, *, safe: bool) -> str:
    quote = not text
    if not quote and safe and text[0] in _FORMULA_PREFIXES:
        quote = True
    if not quote:
        try:
            decoded = _decode_cell(text)
        except ValueError:
            quote = True
        else:
            quote = not (isinstance(decoded, str) and decoded == text)
    if quote:
        return json.dumps(text, ensure_ascii=False)
    return text


def _to_jsonable(value: Any) -> Any:
    """Convert nested items and containers into JSON-serializable data."""
    if isinstance(value, dict):
        return {key: _to_jsonable(element) for key, element in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_jsonable(element) for element in value]
    if ItemAdapter.is_item(value):
        return _to_jsonable(ItemAdapter(value).asdict())
    return value


def _encode_cell(value: Any, *, safe: bool) -> str:
    if value is _MISSING:
        return ""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return _encode_string(value, safe=safe)
    try:
        return json.dumps(_to_jsonable(value), ensure_ascii=False)
    except (TypeError, ValueError) as ex:
        raise TypeError(f"Value of type {type(value).__name__} is not JSON-serializable") from ex


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _validate_value(
    value: Any,
    schema: dict[str, Any],
    *,
    row: int | None,
    column_of: dict[str, int],
    errors: list[CellError],
    path: str,
    top: str | None = None,
) -> None:
    """Validate *value* against the subset of JSON Schema that
    :func:`export_csv` emits, appending :class:`CellError` entries.

    ``path`` is the dotted path of the value within the record, used in
    messages. ``top`` is the top-level field name, used to locate the CSV
    column the value came from.
    """
    if not schema:
        return
    column = column_of.get(top) if top is not None else None
    field = path or None
    any_of = schema.get("anyOf")
    if any_of is not None:
        collected: list[CellError] = []
        for subschema in any_of:
            candidate: list[CellError] = []
            _validate_value(
                value,
                subschema,
                row=row,
                column_of=column_of,
                errors=candidate,
                path=path,
                top=top,
            )
            if not candidate:
                break
            collected.extend(candidate)
        else:
            errors.append(
                CellError(
                    row,
                    column,
                    field,
                    f"value does not match any of the allowed types at {path or '<root>'}",
                )
            )
        return
    type_names = schema.get("type")
    if type_names:
        if isinstance(type_names, str):
            type_names = [type_names]
        if not any(_JSON_TYPE_CHECKS[name](value) for name in type_names):
            errors.append(
                CellError(
                    row,
                    column,
                    field,
                    f"expected {_format_type_names(type_names)} at {path or '<root>'}, "
                    f"got {_json_type_name(value)}",
                )
            )
            return
    enum = schema.get("enum")
    if enum is not None and value not in enum:
        errors.append(CellError(row, column, field, f"value must be one of {enum!r}"))
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                missing_path = f"{path}.{name}" if path else name
                errors.append(
                    CellError(
                        row,
                        column_of.get(name) if not path else column,
                        missing_path,
                        f"missing required field {missing_path!r}",
                    )
                )
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    unknown_path = f"{path}.{name}" if path else name
                    errors.append(
                        CellError(
                            row,
                            column_of.get(name) if not path else column,
                            unknown_path,
                            f"unknown field {unknown_path!r}",
                        )
                    )
        for name, subschema in properties.items():
            if name in value:
                _validate_value(
                    value[name],
                    subschema,
                    row=row,
                    column_of=column_of,
                    errors=errors,
                    path=f"{path}.{name}" if path else name,
                    top=top or name,
                )
    if isinstance(value, list):
        items = schema.get("items")
        if items:
            for element in value:
                _validate_value(
                    element,
                    items,
                    row=row,
                    column_of=column_of,
                    errors=errors,
                    path=path,
                    top=top,
                )


def _format_type_names(type_names: list[str]) -> str:
    if len(type_names) == 1:
        return type_names[0]
    return " or ".join(type_names)


def _safe_type_hints(item_class: type) -> dict[str, Any]:
    try:
        return get_type_hints(item_class)
    except Exception:
        return {}


def _convert_value(type_hint: Any, value: Any) -> Any:
    """Rebuild nested items and containers based on a field type hint."""
    if type_hint is None or type_hint is Any or value is None:
        return value
    origin = get_origin(type_hint)
    if origin in (Union, UnionType):
        for arg in get_args(type_hint):
            if arg is type(None):
                continue
            with contextlib.suppress(Exception):
                return _convert_value(arg, value)
        return value
    if origin is list and isinstance(value, list):
        args = get_args(type_hint)
        item_hint = args[0] if args else None
        return [_convert_value(item_hint, element) for element in value]
    if origin is dict and isinstance(value, dict):
        args = get_args(type_hint)
        value_hint = args[1] if len(args) == 2 else None
        return {key: _convert_value(value_hint, element) for key, element in value.items()}
    if origin is not None:
        return value
    if (
        isinstance(type_hint, type)
        and ItemAdapter.is_item_class(type_hint)
        and isinstance(value, dict)
    ):
        return _build_item(type_hint, value)
    return value


def _build_item(item_class: type, record: dict[str, Any]) -> Any:
    hints = _safe_type_hints(item_class)
    converted = {name: _convert_value(hints.get(name), value) for name, value in record.items()}
    if issubclass(item_class, dict):
        return item_class(converted)
    return item_class(**converted)


def _import_item_class(dotted_path: str) -> type:
    parts = dotted_path.split(".")
    for split in range(len(parts) - 1, 0, -1):
        try:
            module = import_module(".".join(parts[:split]))
        except ImportError:
            continue
        obj: Any = module
        for part in parts[split:]:
            obj = getattr(obj, part)
        if isinstance(obj, type):
            return obj
    raise ValueError(f"Cannot import item class {dotted_path!r}")


def _default_schema_path(csv_path: Path) -> Path:
    return csv_path.with_suffix(".schema.json")


def export_csv(
    items: Any,
    csv_path: str | Path,
    *,
    item_class: type | None = None,
    schema_path: str | Path | None = None,
    safe: bool = False,
) -> Path:
    """Export *items* to *csv_path*, plus a companion JSON schema file.

    *item_class* is inferred from the first item when not given. The schema
    file is written next to the CSV file as ``<name>.schema.json`` unless
    *schema_path* is given. With ``safe=True``, strings that spreadsheet
    software could interpret as formulas are written as JSON strings, so
    they display as text; importing remains lossless either way.

    Returns the path of the schema file.
    """
    items = list(items)
    if item_class is None:
        if not items:
            raise ValueError("item_class is required when exporting an empty batch")
        item_class = items[0].__class__
    field_names = ItemAdapter.get_field_names_from_class(item_class)
    if field_names is None:
        field_names = []
        seen = set()
        for item in items:
            for name in ItemAdapter(item).field_names():
                if name not in seen:
                    seen.add(name)
                    field_names.append(name)
    csv_path = Path(csv_path)
    schema_path = Path(schema_path) if schema_path is not None else _default_schema_path(csv_path)
    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(field_names)
        for item in items:
            adapter = ItemAdapter(item)
            writer.writerow(
                [_encode_cell(adapter.get(name, _MISSING), safe=safe) for name in field_names]
            )
    metadata = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "item_class": f"{item_class.__module__}.{item_class.__qualname__}",
        "safe": bool(safe),
        "columns": list(field_names),
        "schema": ItemAdapter.get_json_schema(item_class),
    }
    schema_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return schema_path


def import_csv(
    csv_path: str | Path,
    *,
    item_class: type | None = None,
    schema_path: str | Path | None = None,
) -> list[Any]:
    """Import items from *csv_path*, validating every row against the schema.

    The companion schema file (``<name>.schema.json`` next to the CSV file,
    or *schema_path*) provides the expected columns and the JSON Schema used
    for validation. *item_class* is taken from the schema file when not
    given, and is required if the schema file does not declare one.

    Every cell is decoded and validated before any item is created: if any
    problem is found, a :class:`TabularImportError` listing all errors (with
    row and column positions) is raised and nothing is imported, so batches
    are never partially written.
    """
    csv_path = Path(csv_path)
    schema_path = Path(schema_path) if schema_path is not None else _default_schema_path(csv_path)
    metadata = None
    if schema_path.exists():
        metadata = json.loads(schema_path.read_text(encoding="utf-8"))
        if metadata.get("format") != FORMAT_NAME:
            raise ValueError(f"{schema_path} is not an {FORMAT_NAME} schema file")
        if metadata.get("version") != FORMAT_VERSION:
            raise ValueError(
                f"Unsupported schema version {metadata.get('version')!r} in {schema_path}"
            )
    if item_class is None:
        declared = (metadata or {}).get("item_class")
        if not declared:
            raise ValueError("item_class is required when the schema file does not declare one")
        item_class = _import_item_class(declared)
    json_schema = (metadata or {}).get("schema") or ItemAdapter.get_json_schema(item_class)
    expected_columns = (metadata or {}).get("columns")

    errors: list[CellError] = []
    records: list[tuple[int, dict[str, Any]]] = []
    with csv_path.open(newline="", encoding="utf-8") as csv_file:
        reader = csv.reader(csv_file)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"{csv_path} is empty") from None
        if len(set(header)) != len(header):
            errors.append(CellError(1, None, None, "the CSV header contains duplicate columns"))
        if expected_columns is not None and (
            set(header) != set(expected_columns) or len(header) != len(expected_columns)
        ):
            errors.append(
                CellError(
                    1,
                    None,
                    None,
                    f"CSV header {header!r} does not match the schema columns "
                    f"{expected_columns!r}",
                )
            )
        column_of = {name: index for index, name in enumerate(header, start=1)}
        properties = json_schema.get("properties", {})
        if properties and json_schema.get("additionalProperties") is False:
            errors.extend(
                CellError(1, column_of[name], name, f"unknown column {name!r}")
                for name in header
                if name not in properties
            )
        for line_number, cells in enumerate(reader, start=2):
            if len(cells) != len(header):
                errors.append(
                    CellError(
                        line_number,
                        None,
                        None,
                        f"expected {len(header)} cells, found {len(cells)}",
                    )
                )
                continue
            record: dict[str, Any] = {}
            for name, text in zip(header, cells, strict=True):
                if text == "":
                    continue  # missing value: the field is left unset
                try:
                    record[name] = _decode_cell(text)
                except ValueError:
                    errors.append(
                        CellError(
                            line_number,
                            column_of[name],
                            name,
                            f"invalid cell value {text!r}",
                        )
                    )
            _validate_value(
                record, json_schema, row=line_number, column_of=column_of, errors=errors, path=""
            )
            records.append((line_number, record))
    if errors:
        raise TabularImportError(errors)
    items = []
    for line_number, record in records:
        try:
            items.append(_build_item(item_class, record))
        except Exception as ex:
            errors.append(
                CellError(
                    line_number,
                    None,
                    None,
                    f"could not build {item_class.__name__}: {ex}",
                )
            )
    if errors:
        raise TabularImportError(errors)
    return items
