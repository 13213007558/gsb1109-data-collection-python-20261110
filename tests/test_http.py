from collections import OrderedDict

import pytest

from w3lib.http import (
    HeadersDictInput,
    RawHeaders,
    basic_auth_header,
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


class TestRawHeaders:
    def test_roundtrip_byte_identical(self):
        raw = (
            b"Server: nginx\r\n"
            b"Set-Cookie: a=1; Path=/\r\n"
            b"set-cookie: b=2; Path=/\r\n"
            b"X-Weird :  spaced value  \r\n"
            b"Trailing-No-Newline: yes"
        )
        assert RawHeaders(raw).to_bytes() == raw

    def test_roundtrip_lf_only(self):
        raw = b"A: 1\nB: 2\n\n"
        assert RawHeaders(raw).to_bytes() == raw

    def test_empty(self):
        headers = RawHeaders()
        assert headers.to_bytes() == b""
        assert headers.to_dict() == {}
        assert len(headers) == 0

    def test_case_insensitive_lookup(self):
        headers = RawHeaders(b"Content-TYPE: text/html\r\n\r\n")
        assert b"content-type" in headers
        assert headers.get(b"CONTENT-TYPE") == b"text/html"
        assert headers.get(b"missing") is None
        assert headers.get(b"missing", b"fallback") == b"fallback"

    def test_duplicates_preserved(self):
        headers = RawHeaders(b"Set-Cookie: a=1\r\nSet-Cookie: b=2\r\n\r\n")
        assert headers.get_all(b"set-cookie") == [b"a=1", b"b=2"]
        assert headers.items() == [(b"Set-Cookie", b"a=1"), (b"Set-Cookie", b"b=2")]
        assert len(headers) == 2

    def test_set_replaces_only_target_field(self):
        raw = (
            b"Server: nginx\r\n"
            b"Set-Cookie: a=1\r\n"
            b"Set-Cookie: b=2\r\n"
            b"X-Weird :  spaced  \r\n"
            b"\r\n"
        )
        headers = RawHeaders(raw)
        headers.set(b"set-cookie", b"cleared=1")
        assert headers.to_bytes() == (
            b"Server: nginx\r\nSet-Cookie: cleared=1\r\nX-Weird :  spaced  \r\n\r\n"
        )

    def test_set_keeps_original_spelling_and_ending(self):
        headers = RawHeaders(b"x-mixed-CASE: old\nOther: 1\n")
        headers.set(b"X-Mixed-Case", b"new")
        assert headers.to_bytes() == b"x-mixed-CASE: new\nOther: 1\n"

    def test_set_appends_missing_field(self):
        headers = RawHeaders(b"Server: nginx\r\n\r\n")
        headers.set(b"X-Archive", b"yes")
        assert headers.to_bytes() == b"Server: nginx\r\n\r\nX-Archive: yes\r\n"

    def test_delete(self):
        headers = RawHeaders(b"A: 1\r\nB: 2\r\na: 3\r\n\r\n")
        headers.delete(b"A")
        assert headers.to_bytes() == b"B: 2\r\n\r\n"
        assert b"a" not in headers

    def test_set_rejects_newline_injection(self):
        headers = RawHeaders(b"A: 1\r\n")
        with pytest.raises(ValueError, match="CR or LF"):
            headers.set(b"A", b"ok\r\nInjected: evil")
        with pytest.raises(ValueError, match="CR or LF"):
            headers.set(b"A", b"ok\nInjected: evil")
        with pytest.raises(ValueError, match="Invalid header name"):
            headers.set(b"Bad Name", b"v")
        with pytest.raises(ValueError, match="Invalid header name"):
            headers.set(b"", b"v")
        # Failed edits must not corrupt the stored data.
        assert headers.to_bytes() == b"A: 1\r\n"

    def test_obs_fold_read_and_flagged(self):
        raw = b"X-Long: first\r\n\tsecond  \r\nOther: 1\r\n"
        headers = RawHeaders(raw)
        assert headers.get(b"x-long") == b"first second"
        assert headers.folded_fields == [b"X-Long"]
        assert headers.to_bytes() == raw

    def test_invalid_lines_kept_not_merged(self):
        raw = (
            b"Good: 1\r\n"
            b"garbage line without colon\r\n"
            b" continuation-after-invalid\r\n"
            b"Bad Name!: x\r\n"
            b"Next: 2\r\n"
        )
        headers = RawHeaders(raw)
        assert headers.items() == [(b"Good", b"1"), (b"Next", b"2")]
        assert headers.invalid_lines == [
            b"garbage line without colon\r\n",
            b" continuation-after-invalid\r\n",
            b"Bad Name!: x\r\n",
        ]
        # The invalid line must not be glued onto the previous field.
        assert headers.get(b"good") == b"1"
        assert headers.to_bytes() == raw

    def test_orphan_continuation_not_attached(self):
        raw = b" leading-continuation\r\nGood: 1\r\n"
        headers = RawHeaders(raw)
        assert headers.items() == [(b"Good", b"1")]
        assert headers.invalid_lines == [b" leading-continuation\r\n"]
        assert headers.to_bytes() == raw

    def test_to_dict_compatible(self):
        raw = b"Content-type: text/html\r\nCache-Control: no-cache\r\nCache-Control: no-store\r\n\r\n"
        headers = RawHeaders(raw)
        assert headers.to_dict() == headers_raw_to_dict(raw)
        # The exported bytes still work with the existing dict helpers.
        headers.set(b"Cache-Control", b"no-store")
        assert headers_raw_to_dict(headers.to_bytes()) == {
            b"Content-type": [b"text/html"],
            b"Cache-Control": [b"no-store"],
        }
