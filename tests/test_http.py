from collections import OrderedDict

from w3lib.http import (
    ContentDispositionError,
    HeadersDictInput,
    basic_auth_header,
    content_disposition_filename,
    headers_dict_to_raw,
    headers_raw_to_dict,
)


class TestHttp:
    def test_basic_auth_header(self):
        assert (
            basic_auth_header("someuser", "somepass")
            == b"Basic c29tZXVzZXI6c29tZXBhc3M="
        )
        # Check url unsafe encoded header
        assert (
            basic_auth_header("someuser", "@<yu9>&o?Q")
            == b"Basic c29tZXVzZXI6QDx5dTk+Jm8/UQ=="
        )

    def test_basic_auth_header_encoding(self):
        assert (
            basic_auth_header("somæusèr", "sømepäss", encoding="utf8")
            == b"Basic c29tw6Z1c8Oocjpzw7htZXDDpHNz"
        )
        # default encoding (ISO-8859-1)
        assert (
            basic_auth_header("somæusèr", "sømepäss")
            == b"Basic c29t5nVz6HI6c/htZXDkc3M="
        )

    def test_headers_raw_dict_none(self):
        assert headers_raw_to_dict(None) is None
        assert headers_dict_to_raw(None) is None

    def test_headers_raw_dict_empty(self):
        assert headers_raw_to_dict(b"") == {}
        assert headers_dict_to_raw({}) == b""

    def test_headers_raw_to_dict(self):
        raw = b"Content-type: text/html\n\rAccept: gzip\n\r\
                Cache-Control: no-cache\n\rCache-Control: no-store\n\n"
        dct = {
            b"Content-type": [b"text/html"],
            b"Accept": [b"gzip"],
            b"Cache-Control": [b"no-cache", b"no-store"],
        }
        assert headers_raw_to_dict(raw) == dct

    def test_headers_dict_to_raw(self):
        dct = OrderedDict([(b"Content-type", b"text/html"), (b"Accept", b"gzip")])
        assert headers_dict_to_raw(dct) == b"Content-type: text/html\r\nAccept: gzip"

    def test_headers_dict_to_raw_listtuple(self):
        dct: HeadersDictInput = OrderedDict(
            [(b"Content-type", [b"text/html"]), (b"Accept", [b"gzip"])]
        )
        assert headers_dict_to_raw(dct) == b"Content-type: text/html\r\nAccept: gzip"

        dct = OrderedDict([(b"Content-type", (b"text/html",)), (b"Accept", (b"gzip",))])
        assert headers_dict_to_raw(dct) == b"Content-type: text/html\r\nAccept: gzip"

        dct = OrderedDict([(b"Cookie", (b"val001", b"val002")), (b"Accept", b"gzip")])
        assert (
            headers_dict_to_raw(dct)
            == b"Cookie: val001\r\nCookie: val002\r\nAccept: gzip"
        )

        dct = OrderedDict([(b"Cookie", [b"val001", b"val002"]), (b"Accept", b"gzip")])
        assert (
            headers_dict_to_raw(dct)
            == b"Cookie: val001\r\nCookie: val002\r\nAccept: gzip"
        )

    def test_headers_dict_to_raw_wrong_values(self):
        dct: HeadersDictInput = OrderedDict(
            [
                (b"Content-type", 0),
            ]
        )
        assert headers_dict_to_raw(dct) == b""

        dct = OrderedDict([(b"Content-type", 1), (b"Accept", [b"gzip"])])
        assert headers_dict_to_raw(dct) == b"Accept: gzip"


# RFC 5987 percent-encoding of "报告" then ".pdf".
_REPORT = "%E6%8A%A5%E5%91%8A"


class TestContentDispositionFilename:
    def test_none_and_empty(self):
        result = content_disposition_filename(None)
        assert result.filename is None
        assert result.source is None
        assert result.errors == ()

        result = content_disposition_filename("attachment")
        assert result.filename is None
        assert result.candidates == ()

    def test_basic_filename(self):
        result = content_disposition_filename('attachment; filename="report.pdf"')
        assert result.filename == "report.pdf"
        assert result.source == "basic"
        assert result.basic == "report.pdf"
        assert result.extended is None
        assert result.candidates == ("report.pdf",)
        assert result.errors == ()

    def test_basic_unquoted_and_bytes(self):
        result = content_disposition_filename(b"attachment; filename=report.pdf")
        assert result.filename == "report.pdf"
        assert result.source == "basic"

    def test_semicolon_inside_quotes_is_not_a_separator(self):
        result = content_disposition_filename('attachment; filename="a;b.pdf"; size=10')
        assert result.filename == "a;b.pdf"
        assert result.candidates == ("a;b.pdf",)

    def test_quoted_pair_backslash_removed(self):
        result = content_disposition_filename('attachment; filename="q\\"name;x.pdf"')
        assert result.filename == 'q"name;x.pdf'

    def test_extended_single(self):
        result = content_disposition_filename(
            f"attachment; filename*=utf-8''{_REPORT}.pdf"
        )
        assert result.filename == "报告.pdf"
        assert result.source == "extended"
        assert result.extended == "报告.pdf"
        assert result.basic is None
        assert result.errors == ()

    def test_extended_takes_precedence_over_basic(self):
        result = content_disposition_filename(
            f"attachment; filename=\"fallback.pdf\"; filename*=utf-8''{_REPORT}.pdf"
        )
        assert result.filename == "报告.pdf"
        assert result.source == "extended"
        assert result.basic == "fallback.pdf"
        assert result.candidates == ("fallback.pdf",)

    def test_valid_multisegment_continuation(self):
        result = content_disposition_filename(
            f"attachment; filename*0*=utf-8''{_REPORT}; filename*1*=utf-8''.pdf"
        )
        assert result.filename == "报告.pdf"
        assert result.source == "extended-continued"
        assert result.extended == "报告.pdf"
        assert result.errors == ()

    def test_continuation_language_may_differ_charset_may_not(self):
        ok = content_disposition_filename(
            f"attachment; filename*0*=utf-8'zh'{_REPORT}; filename*1*=utf-8'en'.pdf"
        )
        assert ok.filename == "报告.pdf"

        conflict = content_disposition_filename(
            f"attachment; filename*0*=utf-8''{_REPORT}; filename*1*=iso-8859-1''.pdf"
        )
        assert conflict.filename is None
        assert conflict.extended is None
        assert len(conflict.errors) == 1
        error = conflict.errors[0]
        assert error.position == "filename*1*"
        assert "charset conflict" in str(error)

    def test_missing_segment_never_builds_another_name(self):
        # Sections 0 and 2 with 1 missing: no name may be assembled.
        result = content_disposition_filename(
            f"attachment; filename*0*=utf-8''{_REPORT}; filename*2*=utf-8''.pdf"
        )
        assert result.filename is None
        assert result.extended is None
        assert result.source is None
        assert len(result.errors) == 1
        error = result.errors[0]
        assert isinstance(error, ContentDispositionError)
        assert error.position == "filename*"
        assert "missing indexes: [1]" in str(error)

    def test_missing_segment_vs_legal_multisegment_proof(self):
        # The legal header joins into the real Chinese name.
        legal = content_disposition_filename(
            f"attachment; filename*0*=utf-8''{_REPORT}; filename*1*=utf-8''.pdf"
        )
        assert legal.filename == "报告.pdf"

        # Dropping section 1 (gap) must not collapse 0+2 into that same name,
        # nor into any joined name: the truncated header yields no filename.
        broken = content_disposition_filename(
            f"attachment; filename*0*=utf-8''{_REPORT}; filename*2*=utf-8''.pdf"
        )
        assert broken.filename is None
        assert broken.filename != legal.filename
        assert broken.extended is None

    def test_continuation_must_start_at_zero(self):
        result = content_disposition_filename(
            "attachment; filename*1*=utf-8''a; filename*2*=utf-8''b"
        )
        assert result.filename is None
        assert result.errors[0].position == "filename*"
        assert "missing indexes: [0]" in str(result.errors[0])

    def test_duplicate_index_invalidates_continuation(self):
        result = content_disposition_filename(
            "attachment; "
            "filename*0*=utf-8''a; filename*0*=utf-8''b; "
            "filename*1*=utf-8''c"
        )
        assert result.filename is None
        assert result.extended is None
        positions = {error.position for error in result.errors}
        assert "filename*0*" in positions

    def test_invalid_extended_shape_reports_position(self):
        result = content_disposition_filename(
            'attachment; filename="f.pdf"; filename*=garbage'
        )
        assert result.filename is None
        assert result.extended is None
        assert result.basic == "f.pdf"
        assert result.errors[0].position == "filename*"

    def test_invalid_extended_charset_reports_position(self):
        result = content_disposition_filename(
            "attachment; filename*=unknown-8bit''%FF.pdf"
        )
        assert result.filename is None
        assert result.errors[0].position == "filename*"

    def test_fallback_to_basic_choice(self):
        header = 'attachment; filename="f.pdf"; filename*=garbage'

        strict = content_disposition_filename(header)
        assert strict.filename is None
        assert strict.source is None

        permissive = content_disposition_filename(header, fallback_to_basic=True)
        assert permissive.filename == "f.pdf"
        assert permissive.source == "basic"
        # The extended failure is still surfaced even when falling back.
        assert permissive.errors[0].position == "filename*"

    def test_basic_used_without_extended_parameter(self):
        # No extended parameter at all: the ordinary name applies regardless of
        # the fallback flag.
        header = 'attachment; filename="plain.pdf"'
        assert content_disposition_filename(header).filename == "plain.pdf"
        assert (
            content_disposition_filename(header, fallback_to_basic=True).filename
            == "plain.pdf"
        )

    def test_multiple_basic_values_are_all_candidates(self):
        result = content_disposition_filename(
            'attachment; filename="one.pdf"; filename="two.pdf"'
        )
        assert result.basic == "one.pdf"
        assert result.candidates == ("one.pdf", "two.pdf")
        assert result.filename == "one.pdf"
