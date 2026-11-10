"""
Unit tests for offline HTML snapshot comparison (htmldate.history).
"""

import io
import json
import os

from contextlib import redirect_stdout
from datetime import datetime

import pytest

from htmldate.cli import parse_args, process_args
from htmldate.history import (
    EVENT_APPEARED,
    EVENT_CHANGED,
    EVENT_DISAPPEARED,
    TRUSTED_CONFIRMED,
    TRUSTED_CONFLICT,
    TRUSTED_CORRECTED,
    TRUSTED_ROLLED_BACK,
    TRUSTED_UNDETERMINED,
    UPDATE_NO,
    UPDATE_UNDETERMINED,
    UPDATE_YES,
    analyze_snapshot,
    compare_snapshots,
)
from htmldate.settings import MIN_DATE
from htmldate.utils import Extractor

TEST_DIR = os.path.abspath(os.path.dirname(__file__))
OUTPUTFORMAT = "%Y-%m-%d"
OPTIONS = Extractor(True, datetime(2030, 12, 31), MIN_DATE, True, OUTPUTFORMAT)
T1 = datetime(2024, 1, 1)
T2 = datetime(2024, 2, 1)
T3 = datetime(2024, 3, 1)


def make_page(
    published: str | None = None,
    modified: str | None = None,
    body: str = "the article body",
) -> str:
    "Build a page with meta declarations and an article container."
    head = ""
    if published is not None:
        head += (
            '<meta property="article:published_time" '
            f'content="{published}"/>'
        )
    if modified is not None:
        head += f'<meta name="last-modified" content="{modified}"/>'
    return (
        f"<html><head>{head}</head>"
        f"<body><article><h1>Title</h1><p>{body}</p></article></body></html>"
    )


def make_jsonld_page(published: str, modified: str, body: str = "body") -> str:
    "Build a page carrying JSON-LD statements."
    return (
        '<html><head><script type="application/ld+json">'
        f'{{"datePublished":"{published}","dateModified":"{modified}"}}'
        "</script></head>"
        f"<body><article><p>{body}</p></article></body></html>"
    )


def test_analyze_snapshot_declarations_and_fingerprint():
    "Markup declarations are classified, parsed and traceable."
    page = make_page(published="2023-12-01", modified="2023-12-05")
    analysis = analyze_snapshot(page, T1, "s1", OPTIONS)
    assert analysis.parseable is True
    assert analysis.content_fingerprint is not None
    roles = {decl.role: decl for decl in analysis.declarations}
    assert set(roles) == {"original", "updated"}
    assert roles["original"].value == "2023-12-01"
    assert roles["updated"].value == "2023-12-05"
    for declaration in analysis.declarations:
        assert declaration.snapshot_id == "s1"
        assert declaration.observed_at == T1
        assert declaration.source == "meta"
        assert declaration.locator.startswith("meta/")

    # unparseable input is recorded, not raised
    broken = analyze_snapshot("", T1, "broken", OPTIONS)
    assert broken.parseable is False
    assert broken.declarations == []
    assert broken.content_fingerprint is None


def test_out_of_order_input_is_sorted():
    "Snapshots are ordered by observation time, regardless of input order."
    snapshots = [
        ("s3", T3, make_page(published="2023-11-15")),
        ("s1", T1, make_page(published="2023-12-01")),
        ("s2", T2, make_page(published="2023-11-15")),
    ]
    history = compare_snapshots(snapshots)
    assert [snapshot.snapshot_id for snapshot in history.snapshots] == [
        "s1",
        "s2",
        "s3",
    ]
    # events follow the chronological order
    assert [event.observed_at for event in history.events] == sorted(
        event.observed_at for event in history.events
    )


def test_same_observation_time_keeps_input_order_and_conflict():
    "Conflicting snapshots at one observation time are both retained."
    snapshots = [
        ("x", T1, make_page(published="2023-12-01", body="A")),
        ("y", T1, make_page(published="2023-11-20", body="B")),
    ]
    history = compare_snapshots(snapshots)
    assert [snapshot.snapshot_id for snapshot in history.snapshots] == ["x", "y"]
    values = {
        declaration.value
        for snapshot in history.snapshots
        for declaration in snapshot.declarations
    }
    assert values == {"2023-12-01", "2023-11-20"}
    assert history.trusted_dates["original"].status == TRUSTED_CONFLICT


def test_declaration_lifecycle_events():
    "A declaration can appear, change and disappear, always citing a snapshot."
    snapshots = [
        ("s1", T1, make_page(published="2023-12-01")),
        ("s2", T2, "<html><body><article><p>no date here</p></article></body></html>"),
        ("s3", T3, make_page(published="2023-11-20")),
    ]
    history = compare_snapshots(snapshots)
    events = [
        (event.event, event.snapshot_id, event.previous_value, event.value)
        for event in history.events
    ]
    assert (EVENT_APPEARED, "s1", None, "2023-12-01") in events
    assert (EVENT_DISAPPEARED, "s2", "2023-12-01", None) in events
    assert (EVENT_APPEARED, "s3", None, "2023-11-20") in events
    # every event points at an existing snapshot
    snapshot_ids = {snapshot.snapshot_id for snapshot in history.snapshots}
    assert all(event.snapshot_id in snapshot_ids for event in history.events)


def test_change_event_on_value_correction():
    "Backward corrections show up as changed events and a corrected trusted date."
    snapshots = [
        ("s1", T1, make_page(published="2023-12-01")),
        ("s2", T2, make_page(published="2023-11-15")),
    ]
    history = compare_snapshots(snapshots)
    change = next(event for event in history.events if event.event == EVENT_CHANGED)
    assert change.previous_value == "2023-12-01"
    assert change.value == "2023-11-15"
    trusted = history.trusted_dates["original"]
    assert trusted.status == TRUSTED_CORRECTED
    assert trusted.value == "2023-11-15"
    assert trusted.evidence == ["s1", "s2"]
    assert trusted.first_seen == T1
    assert trusted.last_seen == T2


def test_rollback_is_flagged_not_overwritten():
    "A modified date moving back to a previously seen value is a rollback."
    snapshots = [
        ("s1", T1, make_page(modified="2024-01-05")),
        ("s2", T2, make_page(modified="2024-01-10")),
        ("s3", T3, make_page(modified="2024-01-05")),
    ]
    history = compare_snapshots(snapshots)
    trusted = history.trusted_dates["updated"]
    assert trusted.status == TRUSTED_ROLLED_BACK
    # the current value and the whole history remain available
    assert trusted.value == "2024-01-05"
    changes = [event for event in history.events if event.event == EVENT_CHANGED]
    assert [event.value for event in changes] == ["2024-01-10", "2024-01-05"]


def test_forward_change_without_corroboration_is_undetermined():
    "A first-time forward value cannot be trusted blindly."
    snapshots = [
        ("s1", T1, make_page(modified="2024-01-05")),
        ("s2", T2, make_page(modified="2024-02-20")),
    ]
    history = compare_snapshots(snapshots)
    assert history.trusted_dates["updated"].status == TRUSTED_UNDETERMINED


def test_custom_output_format_compares_chronologically():
    "Correction detection must not rely on lexicographic value ordering."
    snapshots = [
        ("s1", T1, make_page(published="2023-12-01")),
        ("s2", T2, make_page(published="2023-11-15")),
    ]
    history = compare_snapshots(snapshots, outputformat="%d.%m.%Y")
    trusted = history.trusted_dates["original"]
    assert trusted.value == "15.11.2023"
    assert trusted.status == TRUSTED_CORRECTED


def test_stable_declaration_is_confirmed():
    "Identical declarations across observations confirm the trusted date."
    snapshots = [
        ("s1", T1, make_page(published="2023-12-01")),
        ("s2", T2, make_page(published="2023-12-01")),
    ]
    history = compare_snapshots(snapshots)
    assert history.trusted_dates["original"].status == TRUSTED_CONFIRMED
    assert history.trusted_dates["original"].value == "2023-12-01"


def test_original_and_updated_kept_separate():
    "Raw declarations and inferred dates never merge both roles into one value."
    snapshots = [
        ("s1", T1, make_jsonld_page("2023-12-01", "2023-12-05")),
        ("s2", T2, make_jsonld_page("2023-11-20", "2023-12-05")),
    ]
    history = compare_snapshots(snapshots)
    assert set(history.trusted_dates) == {"original", "updated"}
    assert history.trusted_dates["original"].value == "2023-11-20"
    assert history.trusted_dates["updated"].value == "2023-12-05"
    sources = {
        declaration.source
        for snapshot in history.snapshots
        for declaration in snapshot.declarations
    }
    assert sources == {"json-ld"}
    # raw declarations for both roles survive on each snapshot
    for snapshot in history.snapshots:
        assert {decl.role for decl in snapshot.declarations} == {
            "original",
            "updated",
        }


def test_date_only_change_with_same_body_is_not_an_update():
    "Rolling a modified date back without body changes must not claim an update."
    snapshots = [
        ("s1", T1, make_page(modified="2024-01-10", body="identical text")),
        ("s2", T2, make_page(modified="2024-01-05", body="identical text")),
    ]
    history = compare_snapshots(snapshots)
    assert history.content_changed is False
    assert history.article_updated == UPDATE_NO
    # the declaration change itself is still documented
    assert any(event.event == EVENT_CHANGED for event in history.events)


def test_content_change_is_detected():
    "Different body text yields an explicit update verdict."
    snapshots = [
        ("s1", T1, make_page(published="2023-12-01", body="version one")),
        ("s2", T2, make_page(published="2023-12-01", body="version two")),
    ]
    history = compare_snapshots(snapshots)
    assert history.content_changed is True
    assert history.article_updated == UPDATE_YES


def test_unparseable_snapshot_makes_verdict_undetermined():
    "Missing evidence must not be read as disappearance or as an update."
    snapshots = [
        ("s1", T1, make_page(published="2023-12-01")),
        ("s2", T2, ""),
        ("s3", T3, make_page(published="2023-12-01")),
    ]
    history = compare_snapshots(snapshots)
    assert [snapshot.parseable for snapshot in history.snapshots] == [
        True,
        False,
        True,
    ]
    assert history.content_changed is None
    assert history.article_updated == UPDATE_UNDETERMINED
    assert [event.event for event in history.events] == [EVENT_APPEARED]
    assert history.trusted_dates["original"].status == TRUSTED_CONFIRMED


def test_heuristic_fallback_for_unmarked_dates():
    "Pages without structured markup still feed a clearly labeled heuristic source."
    snapshots = [
        ("s1", T1, "<html><body><article><p>2023-12-01</p></article></body></html>"),
    ]
    history = compare_snapshots(snapshots)
    declaration = history.snapshots[0].declarations[0]
    assert declaration.source == "heuristic"
    assert declaration.value == "2023-12-01"


def test_heuristic_fallback_when_markup_is_unparseable():
    "An undatable meta statement does not suppress a body-text date."
    page = (
        '<html><head><meta property="article:published_time" content="soon"/></head>'
        "<body><article><p>2023-12-01</p></article></body></html>"
    )
    history = compare_snapshots([("s1", T1, page)])
    sources = {decl.source for decl in history.snapshots[0].declarations}
    assert "heuristic" in sources
    assert history.trusted_dates["original"].value == "2023-12-01"
    # the raw, unparseable statement is still retained for traceability
    meta = next(
        decl
        for decl in history.snapshots[0].declarations
        if decl.source == "meta"
    )
    assert meta.value is None and meta.raw == "soon"


def test_validation_errors():
    "Bad input is rejected before any analysis runs."
    with pytest.raises(ValueError, match="at least one"):
        compare_snapshots([])
    with pytest.raises(ValueError, match="unique"):
        compare_snapshots(
            [
                ("s1", T1, make_page()),
                ("s1", T2, make_page()),
            ]
        )
    with pytest.raises(TypeError, match="datetime"):
        compare_snapshots([("s1", "2024-01-01", make_page())])
    with pytest.raises(ValueError, match="output format"):
        compare_snapshots([("s1", T1, make_page())], outputformat="not-a-format")


def test_min_max_dates_are_applied():
    "Date boundaries are forwarded to declaration parsing."
    page = make_page(published="2010-06-01")
    history = compare_snapshots([("s1", T1, page)], min_date="2020-01-01")
    # declaration still listed for traceability, but with no parsed value
    declaration = history.snapshots[0].declarations[0]
    assert declaration.raw == "2010-06-01"
    assert declaration.value is None
    assert "original" not in history.trusted_dates


def test_serialization_roundtrip():
    "The result exports to plain JSON-friendly data without losing traceability."
    snapshots = [
        ("s1", T1, make_page(published="2023-12-01", modified="2023-12-05")),
        ("s2", T2, make_page(published="2023-11-20", modified="2023-12-05")),
    ]
    history = compare_snapshots(snapshots)
    payload = json.dumps(history.as_dict())
    decoded = json.loads(payload)
    assert {snapshot["snapshot_id"] for snapshot in decoded["snapshots"]} == {
        "s1",
        "s2",
    }
    event = decoded["events"][0]
    assert {"event", "observed_at", "snapshot_id", "value"} <= set(event)
    assert decoded["trusted_dates"]["original"]["evidence"] == ["s1", "s2"]
    assert decoded["article_updated"] in {
        UPDATE_YES,
        UPDATE_NO,
        UPDATE_UNDETERMINED,
    }


def test_cli_compare_from_file(tmp_path):
    "The --compare entry point writes the full history as JSON."
    first = tmp_path / "a.html"
    second = tmp_path / "b.html"
    first.write_text(make_page(published="2023-12-01"), encoding="utf-8")
    second.write_text(make_page(published="2023-11-20"), encoding="utf-8")
    spec = tmp_path / "snapshots.json"
    spec.write_text(
        json.dumps(
            [
                {"id": "b", "observed_at": T2.isoformat(), "file": str(second)},
                {"id": "a", "observed_at": T1.isoformat(), "file": str(first)},
            ]
        ),
        encoding="utf-8",
    )
    args = parse_args(["--compare", str(spec)])
    output = io.StringIO()
    with redirect_stdout(output):
        process_args(args)
    decoded = json.loads(output.getvalue())
    assert [snapshot["snapshot_id"] for snapshot in decoded["snapshots"]] == [
        "a",
        "b",
    ]
    assert decoded["trusted_dates"]["original"]["status"] == TRUSTED_CORRECTED


def test_cli_compare_inline_html_via_stdin(monkeypatch):
    "Inline HTML specs can be piped through standard input."
    spec = json.dumps(
        [
            {
                "id": "s1",
                "observed_at": T1.isoformat(),
                "html": make_page(published="2023-12-01"),
            }
        ]
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(spec))
    args = parse_args(["--compare", "-"])
    output = io.StringIO()
    with redirect_stdout(output):
        process_args(args)
    decoded = json.loads(output.getvalue())
    assert decoded["snapshots"][0]["declarations"][0]["value"] == "2023-12-01"


def test_cli_compare_rejects_bad_spec(tmp_path):
    "Malformed descriptions and missing files terminate with a clear message."
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        process_args(parse_args(["--compare", str(bad_json)]))

    missing_html = tmp_path / "missing.json"
    missing_html.write_text(
        json.dumps(
            [
                {
                    "id": "s1",
                    "observed_at": T1.isoformat(),
                    "file": str(tmp_path / "nope.html"),
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        process_args(parse_args(["--compare", str(missing_html)]))

    missing_time = tmp_path / "notime.json"
    missing_time.write_text(
        json.dumps([{"id": "s1", "html": make_page()}]),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        process_args(parse_args(["--compare", str(missing_time)]))
