import csv
import json
from dataclasses import dataclass, field

import pytest

from itemadapter.tabular import CellError, TabularImportError, export_csv, import_csv


@dataclass
class Address:
    street: str
    city: str
    zip_code: str | None = None


@dataclass
class CrawlRecord:
    url: str
    title: str
    address: Address
    tags: list[str] = field(default_factory=list)
    score: float | None = None
    retries: int = 0


def sample_records():
    return [
        CrawlRecord(
            url="https://example.com/a",
            title="",
            address=Address(street="Main St. 1", city="Berlin"),
            tags=[],
            score=None,
        ),
        CrawlRecord(
            url="https://example.com/b",
            title="Example, Inc.",
            address=Address(street="Hauptstr. 2", city="München", zip_code="80331"),
            tags=["news", "de", "priority"],
            score=9.5,
            retries=2,
        ),
        CrawlRecord(
            url="https://example.com/c",
            title="null",
            address=Address(street="Odenwaldstr. 3", city="Heidelberg", zip_code="69115"),
            tags=["archive"],
            score=0.0,
            retries=1,
        ),
    ]


class TestRoundTrip:
    def test_nested_and_variable_length_tags(self, tmp_path):
        records = sample_records()
        csv_path = tmp_path / "records.csv"
        schema_path = export_csv(records, csv_path)

        assert schema_path == tmp_path / "records.schema.json"
        metadata = json.loads(schema_path.read_text(encoding="utf-8"))
        assert metadata["format"] == "itemadapter-tabular"
        assert metadata["columns"] == ["url", "title", "address", "tags", "score", "retries"]
        assert metadata["schema"]["properties"]["address"]["type"] == "object"

        imported = import_csv(csv_path, item_class=CrawlRecord)
        assert imported == records

        for original, restored in zip(records, imported, strict=True):
            assert type(restored) is CrawlRecord
            assert type(restored.address) is Address
            assert type(restored.tags) is list
            assert all(type(tag) is str for tag in restored.tags)
            assert restored.tags == original.tags
            assert restored.score == original.score
            assert restored.title == original.title

        assert imported[0].address.zip_code is None
        assert imported[0].tags == []
        assert imported[1].tags == ["news", "de", "priority"]
        assert type(imported[1].score) is float
        assert type(imported[2].retries) is int

    def test_item_class_inferred_from_schema(self, tmp_path):
        records = sample_records()
        csv_path = tmp_path / "records.csv"
        export_csv(records, csv_path)
        imported = import_csv(csv_path)
        assert imported == records
        assert all(type(item) is CrawlRecord for item in imported)

    def test_explicit_schema_path(self, tmp_path):
        records = sample_records()
        csv_path = tmp_path / "records.csv"
        schema_path = tmp_path / "custom" / "schema.json"
        schema_path.parent.mkdir()
        result = export_csv(records, csv_path, schema_path=schema_path)
        assert result == schema_path
        assert import_csv(csv_path, item_class=CrawlRecord, schema_path=schema_path) == records


class TestCellRepresentation:
    def test_none_empty_string_and_missing(self, tmp_path):
        csv_path = tmp_path / "records.csv"
        items = [
            {"a": None, "b": "", "c": 1},
            {"a": 2, "c": 3},  # b is missing entirely
        ]
        export_csv(items, csv_path, item_class=dict)
        with csv_path.open(newline="", encoding="utf-8") as csv_file:
            rows = list(csv.reader(csv_file))
        assert rows[0] == ["a", "b", "c"]
        assert rows[1] == ["null", '""', "1"]  # None, empty string
        assert rows[2] == ["2", "", "3"]  # missing b: empty cell

        imported = import_csv(csv_path, item_class=dict)
        assert imported[0] == {"a": None, "b": "", "c": 1}
        assert imported[1] == {"a": 2, "c": 3}
        assert "b" not in imported[1]

    def test_ambiguous_strings_are_quoted(self, tmp_path):
        csv_path = tmp_path / "records.csv"
        items = [
            {"value": "null"},
            {"value": "true"},
            {"value": "42"},
            {"value": "1e5"},
            {"value": "[1, 2]"},
            {"value": '{"a": 1}'},
            {"value": "plain"},
        ]
        export_csv(items, csv_path, item_class=dict)
        with csv_path.open(newline="", encoding="utf-8") as csv_file:
            rows = list(csv.reader(csv_file))
        assert [row[0] for row in rows[1:]] == [
            '"null"',
            '"true"',
            '"42"',
            '"1e5"',
            '"[1, 2]"',
            '"{\\"a\\": 1}"',
            "plain",
        ]
        imported = import_csv(csv_path, item_class=dict)
        assert [item["value"] for item in imported] == [item["value"] for item in items]

    def test_field_names_with_delimiters(self, tmp_path):
        csv_path = tmp_path / "records.csv"
        items = [
            {
                "a,b": 1,
                'x"y': 2,
                "addr.city": "literal field, not nesting",
                "addr": {"city": "nested"},
            }
        ]
        export_csv(items, csv_path, item_class=dict)
        imported = import_csv(csv_path, item_class=dict)
        assert imported == items
        with csv_path.open(newline="", encoding="utf-8") as csv_file:
            header = next(csv.reader(csv_file))
        assert header == ["a,b", 'x"y', "addr.city", "addr"]


class TestSafeMode:
    def test_dangerous_strings_are_quoted(self, tmp_path):
        csv_path = tmp_path / "records.csv"
        items = [{"cmd": "=2+5"}, {"cmd": "+SUM(A1)"}, {"cmd": "-x"}, {"cmd": "@run"}]
        export_csv(items, csv_path, item_class=dict, safe=True)
        with csv_path.open(newline="", encoding="utf-8") as csv_file:
            rows = list(csv.reader(csv_file))
        for row in rows[1:]:
            assert row[0].startswith('"'), row
        metadata = json.loads((tmp_path / "records.schema.json").read_text(encoding="utf-8"))
        assert metadata["safe"] is True

        imported = import_csv(csv_path, item_class=dict)
        assert imported == items

    def test_unsafe_mode_keeps_verbatim(self, tmp_path):
        csv_path = tmp_path / "records.csv"
        items = [{"cmd": "=2+5"}]
        export_csv(items, csv_path, item_class=dict)
        with csv_path.open(newline="", encoding="utf-8") as csv_file:
            rows = list(csv.reader(csv_file))
        assert rows[1] == ["=2+5"]
        assert import_csv(csv_path, item_class=dict) == items


class TestImportValidation:
    def _write_csv(self, tmp_path, header, rows):
        csv_path = tmp_path / "records.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(header)
            writer.writerows(rows)
        return csv_path

    def test_type_error_reports_row_and_column(self, tmp_path):
        header = ["url", "title", "address", "tags", "score", "retries"]
        good_row = [
            "https://example.com",
            "ok",
            '{"street": "s", "city": "c"}',
            '["a"]',
            "1.5",
            "0",
        ]
        bad_row = [*good_row[:5], "not-a-number"]
        csv_path = self._write_csv(tmp_path, header, [good_row, bad_row])
        with pytest.raises(TabularImportError) as excinfo:
            import_csv(csv_path, item_class=CrawlRecord)
        errors = excinfo.value.errors
        assert len(errors) == 1
        error = errors[0]
        assert error.row == 3
        assert error.column == 6
        assert error.field == "retries"
        assert "integer" in error.message
        assert "row 3" in str(error)
        assert "column 6" in str(error)

    def test_nested_type_error_points_to_top_level_cell(self, tmp_path):
        header = ["url", "title", "address", "tags", "score", "retries"]
        row = [
            "https://example.com",
            "ok",
            '{"street": "s", "city": 5}',
            "[]",
            "",
            "0",
        ]
        csv_path = self._write_csv(tmp_path, header, [row])
        with pytest.raises(TabularImportError) as excinfo:
            import_csv(csv_path, item_class=CrawlRecord)
        error = excinfo.value.errors[0]
        assert error.row == 2
        assert error.column == 3
        assert error.field == "address.city"

    def test_missing_required_field(self, tmp_path):
        header = ["url", "title", "address", "tags", "score", "retries"]
        row = ["", "ok", '{"street": "s", "city": "c"}', "[]", "", "0"]
        csv_path = self._write_csv(tmp_path, header, [row])
        with pytest.raises(TabularImportError) as excinfo:
            import_csv(csv_path, item_class=CrawlRecord)
        error = excinfo.value.errors[0]
        assert error.row == 2
        assert error.column == 1
        assert error.field == "url"
        assert "missing required field" in error.message

    def test_invalid_cell_reports_position(self, tmp_path):
        header = ["url", "title", "address", "tags", "score", "retries"]
        row = ["https://example.com", "ok", "{not json}", "[]", "", "0"]
        csv_path = self._write_csv(tmp_path, header, [row])
        with pytest.raises(TabularImportError) as excinfo:
            import_csv(csv_path, item_class=CrawlRecord)
        error = excinfo.value.errors[0]
        assert error.row == 2
        assert error.column == 3
        assert error.field == "address"
        assert "invalid cell value" in error.message

    def test_unknown_column(self, tmp_path):
        header = ["url", "title", "address", "extra"]
        row = ["https://example.com", "ok", '{"street": "s", "city": "c"}', "x"]
        csv_path = self._write_csv(tmp_path, header, [row])
        with pytest.raises(TabularImportError) as excinfo:
            import_csv(csv_path, item_class=CrawlRecord)
        assert any(error.field == "extra" for error in excinfo.value.errors)

    def test_atomic_import(self, tmp_path):
        """No item is created at all when any row is invalid."""
        header = ["url", "title", "address", "tags", "score", "retries"]
        good_row = [
            "https://example.com",
            "ok",
            '{"street": "s", "city": "c"}',
            "[]",
            "",
            "0",
        ]
        bad_row = [*good_row[:5], "oops"]
        csv_path = self._write_csv(tmp_path, header, [good_row, bad_row, good_row])
        with pytest.raises(TabularImportError):
            import_csv(csv_path, item_class=CrawlRecord)
        # Fix the bad cell: the whole batch then imports at once.
        csv_path = self._write_csv(tmp_path, header, [good_row, good_row, good_row])
        assert len(import_csv(csv_path, item_class=CrawlRecord)) == 3

    def test_header_mismatch_with_schema(self, tmp_path):
        records = sample_records()
        csv_path = tmp_path / "records.csv"
        export_csv(records, csv_path)
        lines = csv_path.read_text(encoding="utf-8").splitlines()
        lines[0] = lines[0].replace("retries", "attempts")
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(TabularImportError) as excinfo:
            import_csv(csv_path, item_class=CrawlRecord)
        assert any(
            "does not match the schema columns" in error.message for error in excinfo.value.errors
        )

    def test_cell_error_str(self):
        assert str(CellError(2, 3, "tags", "boom")) == "row 2, column 3, field 'tags': boom"
        assert str(CellError(None, None, None, "boom")) == "boom"
