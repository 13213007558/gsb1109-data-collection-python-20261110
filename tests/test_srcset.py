import pytest

from w3lib.srcset import (
    SrcsetCandidate,
    SrcsetParseError,
    choose_srcset,
    evaluate_sizes,
    parse_srcset,
)


class TestParseSrcset:
    def test_width_descriptors(self):
        candidates = parse_srcset("a.jpg 480w, b.jpg 960w , c.jpg 1440w")
        assert [(c.url, c.width, c.density) for c in candidates] == [
            ("a.jpg", 480, None),
            ("b.jpg", 960, None),
            ("c.jpg", 1440, None),
        ]

    def test_density_descriptors(self):
        candidates = parse_srcset("a.jpg, b.jpg 1.5x, c.jpg 2x")
        assert [(c.url, c.width, c.density) for c in candidates] == [
            ("a.jpg", None, None),
            ("b.jpg", None, 1.5),
            ("c.jpg", None, 2.0),
        ]
        assert candidates[0].effective_density == 1.0

    def test_raw_candidate_text_is_kept(self):
        candidates = parse_srcset("  a.jpg   480w  ,\tb.jpg 960w")
        assert candidates[0].raw == "a.jpg   480w"
        assert candidates[1].raw == "b.jpg 960w"

    def test_base_url_resolution(self):
        candidates = parse_srcset(
            "img/small.jpg 100w, /img/big.jpg 200w, https://cdn.example.com/x.jpg 300w",
            base_url="https://example.com/page/index.html",
        )
        assert [c.resolved_url for c in candidates] == [
            "https://example.com/page/img/small.jpg",
            "https://example.com/img/big.jpg",
            "https://cdn.example.com/x.jpg",
        ]
        assert candidates[0].base_url == "https://example.com/page/index.html"
        without_base = parse_srcset("a.jpg 1x")[0]
        assert without_base.resolved_url == "a.jpg"

    def test_data_url_with_commas_is_not_split(self):
        srcset = (
            "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAw"
            "MC9zdmciPjwvc3ZnPg== 1x, fallback.png 2x"
        )
        candidates = parse_srcset(srcset)
        assert len(candidates) == 2
        assert candidates[0].url.startswith("data:image/svg+xml;base64,PHN2Zy")
        assert candidates[0].url.endswith("==")
        assert candidates[0].density == 1.0
        assert candidates[1].url == "fallback.png"

    def test_data_url_with_commas_and_width_descriptor(self):
        candidates = parse_srcset("data:image/gif;base64,R0lGODlhAQABAAAAACw= 100w")
        assert len(candidates) == 1
        assert candidates[0].url == "data:image/gif;base64,R0lGODlhAQABAAAAACw="
        assert candidates[0].width == 100

    def test_comma_handling(self):
        # A comma right after a URL ends the candidate and is not part of it.
        candidates = parse_srcset("a.jpg, b.jpg 2x")
        assert [(c.url, c.density) for c in candidates] == [
            ("a.jpg", None),
            ("b.jpg", 2.0),
        ]
        # Without whitespace the comma is URL text (as in data URLs), so the
        # whole run is a single candidate URL.
        (candidate,) = parse_srcset("a.jpg,b.jpg 2x")
        assert candidate.url == "a.jpg,b.jpg"
        assert candidate.density == 2.0

    def test_empty_and_separator_only(self):
        assert parse_srcset("") == []
        assert parse_srcset("  , ,\t") == []

    @pytest.mark.parametrize(
        "srcset",
        [
            "a.jpg 100w 200w",  # duplicate width descriptor
            "a.jpg 1x 2x",  # duplicate density descriptor
            "a.jpg 100w 2x",  # width and density in one candidate
            "a.jpg 100w, b.jpg 2x",  # width and density across candidates
            "a.jpg 100w, b.jpg",  # a bare candidate is a 1x density candidate
            "a.jpg 0w",  # zero width
            "a.jpg 1.5w",  # non-integer width
            "a.jpg -1w",  # negative width
            "a.jpg w",  # missing width number
            "a.jpg 100W",  # unknown uppercase descriptor
            "a.jpg 0x",  # zero density
            "a.jpg -2x",  # negative density
            "a.jpg 1.x",  # malformed density number
            "a.jpg x",  # missing density number
            "a.jpg 100h",  # unsupported h descriptor
            "a.jpg abc",  # unknown descriptor
        ],
    )
    def test_invalid_srcset_is_not_silently_accepted(self, srcset):
        with pytest.raises(SrcsetParseError):
            parse_srcset(srcset)

    def test_error_is_a_value_error(self):
        with pytest.raises(ValueError, match=r"[Dd]escriptor"):
            parse_srcset("a.jpg 0w")


class TestEvaluateSizes:
    @pytest.mark.parametrize(
        ("sizes", "viewport_width", "expected"),
        [
            ("100vw", 500, 500.0),
            ("50vw", 1280, 640.0),
            ("400px", 1280, 400.0),
            ("(max-width: 600px) 100vw, 400px", 375, 375.0),
            ("(max-width: 600px) 100vw, 400px", 1280, 400.0),
            ("(min-width: 600px) 50vw, 100vw", 1280, 640.0),
            ("(min-width: 600px) 50vw, 100vw", 320, 320.0),
            # First matching condition wins.
            ("(max-width: 600px) 480px, (max-width: 1200px) 25vw, 800px", 500, 480.0),
            ("(max-width: 600px) 480px, (max-width: 1200px) 25vw, 800px", 1000, 250.0),
            ("(max-width: 600px) 480px, (max-width: 1200px) 25vw, 800px", 1600, 800.0),
            # Boundary conditions are inclusive.
            ("(max-width: 600px) 300px, 900px", 600, 300.0),
            ("(min-width: 600px) 300px, 900px", 600, 300.0),
            # Whitespace and case variations.
            ("( max-width:600px ) 100vw , 50VW", 500, 500.0),
            ("(MAX-WIDTH: 600px) 10PX, 20px", 500, 10.0),
        ],
    )
    def test_supported_subset(self, sizes, viewport_width, expected):
        assert evaluate_sizes(sizes, viewport_width) == expected

    @pytest.mark.parametrize(
        "sizes",
        [
            "",
            " ",
            "(orientation: landscape) 50vw, 100px",  # unsupported media feature
            "(min-width: 600px) and (max-width: 900px) 50vw, 100vw",  # media query
            "calc(100vw - 20px)",  # calc() is not supported
            "(max-width: 600px) 50%, 100vw",  # percentage length
            "(max-width: 600px) 10em, 100vw",  # em length
            "(max-width: 600px) 100vw",  # no default length
            "(max-width: 600px)",  # condition without a length
            "(max-width: abcpx) 100vw, 50vw",  # malformed condition number
            "(max-width: 600px) vw, 100vw",  # malformed length number
            "100",  # unitless length
            ",",
        ],
    )
    def test_unknown_expressions_are_undetermined(self, sizes):
        assert evaluate_sizes(sizes, 800) is None


class TestChooseSrcset:
    srcset = "small.jpg 400w, medium.jpg 800w, large.jpg 1200w"
    sizes = "(max-width: 600px) 100vw, 400px"

    def test_same_image_two_viewports(self):
        candidates = parse_srcset(self.srcset, base_url="https://example.com/")
        phone = choose_srcset(
            candidates, viewport_width=375, device_pixel_ratio=2, sizes=self.sizes
        )
        assert phone.candidate is not None
        assert phone.candidate.resolved_url == "https://example.com/medium.jpg"
        assert phone.source_size == 375.0
        assert "2.13x" in phone.reason
        desktop = choose_srcset(
            candidates, viewport_width=1280, device_pixel_ratio=1, sizes=self.sizes
        )
        assert desktop.candidate is not None
        assert desktop.candidate.resolved_url == "https://example.com/small.jpg"
        assert desktop.source_size == 400.0

    def test_no_sizes_defaults_to_100vw(self):
        candidates = parse_srcset(self.srcset)
        selection = choose_srcset(candidates, viewport_width=500, device_pixel_ratio=1)
        assert selection.candidate is not None
        assert selection.source_size == 500.0
        # 400/500 = 0.8x < 1x, so the smallest sufficient is 800w (1.6x).
        assert selection.candidate.url == "medium.jpg"

    def test_densest_available_when_none_suffices(self):
        candidates = parse_srcset(self.srcset)
        selection = choose_srcset(candidates, viewport_width=100, device_pixel_ratio=30)
        assert selection.candidate is not None
        assert selection.candidate.url == "large.jpg"
        assert "densest available" in selection.reason

    def test_density_candidates(self):
        candidates = parse_srcset("a.jpg, b.jpg 2x, c.jpg 3x")
        assert choose_srcset(candidates, 800, 1).candidate.url == "a.jpg"
        assert choose_srcset(candidates, 800, 1.5).candidate.url == "b.jpg"
        assert choose_srcset(candidates, 800, 4).candidate.url == "c.jpg"

    def test_ties_keep_document_order(self):
        candidates = parse_srcset("a.jpg 2x, b.jpg 2x")
        assert choose_srcset(candidates, 800, 2).candidate.url == "a.jpg"

    def test_undetermined_sizes(self):
        candidates = parse_srcset(self.srcset)
        selection = choose_srcset(
            candidates, viewport_width=800, sizes="calc(100vw - 2rem)"
        )
        assert selection.candidate is None
        assert selection.source_size is None
        assert "cannot be determined" in selection.reason

    def test_empty_candidates(self):
        with pytest.raises(ValueError, match="No srcset candidates"):
            choose_srcset([], viewport_width=800)

    @pytest.mark.parametrize(
        ("viewport_width", "device_pixel_ratio"),
        [(0, 1), (-1, 1), (800, 0), (800, -2)],
    )
    def test_non_positive_environment(self, viewport_width, device_pixel_ratio):
        candidates = parse_srcset(self.srcset)
        with pytest.raises(ValueError, match="must be positive"):
            choose_srcset(candidates, viewport_width, device_pixel_ratio)

    def test_mixed_candidates_are_rejected(self):
        candidates = [
            SrcsetCandidate(raw="a.jpg 100w", url="a.jpg", width=100),
            SrcsetCandidate(raw="b.jpg 2x", url="b.jpg", density=2.0),
        ]
        with pytest.raises(SrcsetParseError, match="cannot be mixed"):
            choose_srcset(candidates, viewport_width=800)
