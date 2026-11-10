import json
import unicodedata

import pytest

from w3lib.naming import (
    PathAllocator,
    canonical_identity,
    ensure_creatable,
    exact_identity,
    is_safe_relative_path,
    load_manifest,
    save_manifest,
)


class TestIdentityPolicies:
    def test_exact_identity_merges_nothing(self):
        allocator = PathAllocator(identity=exact_identity)
        urls = [
            "http://example.com/a?x=1&y=2",
            "http://example.com/a?y=2&x=1",
            "http://example.com/%2f",
            "http://example.com/%2F",
            "http://EXAMPLE.com/a",
            "http://example.com/a#frag",
        ]
        paths = {allocator.assign(url) for url in urls}
        assert len(paths) == len(urls)

    def test_canonical_identity_merges_cosmetic_differences(self):
        allocator = PathAllocator(identity=canonical_identity)
        assert allocator.assign("http://example.com/a?x=1&y=2") == allocator.assign(
            "http://example.com/a?y=2&x=1"
        )
        assert allocator.assign("http://example.com/%2f") == allocator.assign(
            "http://example.com/%2F"
        )
        assert allocator.assign("http://EXAMPLE.com/a") == allocator.assign(
            "http://example.com/a"
        )
        assert len(allocator.manifest) == 3

    def test_canonical_identity_keeps_real_differences(self):
        allocator = PathAllocator(identity=canonical_identity)
        assert allocator.assign("http://example.com/a?x=1") != allocator.assign(
            "http://example.com/a?x=2"
        )

    def test_default_policy_is_exact(self):
        allocator = PathAllocator()
        assert allocator.assign("http://example.com/%2f") != allocator.assign(
            "http://example.com/%2F"
        )


class TestDeterminism:
    URLS = [
        "http://example.com/",
        "http://example.com/a<b",
        "http://example.com/a>b",
        "http://example.com/a?b=1&b=2",
        "https://user:pw@example.com:8080/p/q?r=s#t",
        "http://example.com/café",
        "http://example.com/café",
        "not a url at all",
        "",
    ]

    def test_same_url_same_path(self):
        allocator = PathAllocator()
        assert allocator.assign("http://example.com/a") == allocator.assign(
            "http://example.com/a"
        )
        assert len(allocator.manifest) == 1

    def test_batches_match_single_pass(self):
        at_once = PathAllocator()
        at_once.assign_all(self.URLS)
        in_batches = PathAllocator()
        in_batches.assign_all(self.URLS[:3])
        in_batches.assign_all(self.URLS[3:6])
        in_batches.assign_all(self.URLS[6:])
        assert at_once.manifest == in_batches.manifest

    def test_path_depends_on_identity_not_arrival_order(self):
        forward = PathAllocator()
        forward_map = forward.assign_all(self.URLS)
        backward = PathAllocator()
        backward_map = backward.assign_all(list(reversed(self.URLS)))
        assert forward_map == backward_map

    def test_slug_collisions_stay_distinct(self):
        allocator = PathAllocator()
        path1 = allocator.assign("http://example.com/a<b")
        path2 = allocator.assign("http://example.com/a>b")
        assert path1 != path2

    def test_case_only_differences_stay_distinct(self):
        allocator = PathAllocator()
        assert allocator.assign("http://example.com/README") != allocator.assign(
            "http://example.com/readme"
        )

    def test_manifest_round_trip_preserves_assignments(self, tmp_path):
        allocator = PathAllocator()
        allocator.assign_all(self.URLS)
        file = tmp_path / "manifest.json"
        save_manifest(allocator.manifest, file)
        restored = PathAllocator(manifest=load_manifest(file))
        assert restored.manifest == allocator.manifest
        for url in self.URLS:
            assert restored.assign(url) == allocator.assign(url)


class TestSafeRelativePath:
    @pytest.mark.parametrize(
        "path",
        [
            "example.com/index-0123456789ab",
            "example.com/dir/file-0123456789ab",
            "example.com/café-0123456789ab",
            "a",
        ],
    )
    def test_safe(self, path):
        assert is_safe_relative_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "/abs/path",
            "a\\b",
            "a//b",
            ".",
            "..",
            "a/../b",
            "con",
            "CON",
            "aux.txt",
            "com1",
            "lpt9/xx",
            "file.",
            "file ",
            "a/b" * 150,  # too long overall
            "x" * 81,  # component too long
            "bad<name",
            "bad|name",
            "bad\tx",
            "café",  # not NFC
        ],
    )
    def test_unsafe(self, path):
        assert not is_safe_relative_path(path)

    def test_allocator_output_is_always_safe(self):
        allocator = PathAllocator()
        nasty = [
            "http://con/",
            "http://example.com/CON",
            "http://example.com/aux.txt",
            "http://example.com/file.",
            "http://example.com/file ",
            "http://example.com/a<b>:c|d?e=f*g",
            "http://example.com/" + "x" * 500,
            "http://example.com/.../...",
            "http://example.com/\x01\x02",
        ]
        for url in nasty:
            path = allocator.assign(url)
            assert is_safe_relative_path(path), path

    def test_reserved_names_are_prefixed(self):
        allocator = PathAllocator()
        for url in (
            "http://con/",
            "http://example.com/CON",
            "http://example.com/aux.txt",
        ):
            path = allocator.assign(url)
            for component in path.split("/"):
                base = component.split(".", 1)[0].rstrip(" .")
                assert base.split("-")[0].casefold() not in {
                    "con",
                    "prn",
                    "aux",
                    "nul",
                } or base.startswith("_")


class TestEdgeCases:
    def test_overlong_url(self):
        allocator = PathAllocator()
        url1 = "http://example.com/" + "a" * 5000 + "?" + "q" * 5000
        url2 = "http://example.com/" + "a" * 5000 + "?" + "q" * 4999 + "r"
        path1, path2 = allocator.assign(url1), allocator.assign(url2)
        assert path1 != path2
        for path in (path1, path2):
            assert len(path) <= 200
            assert is_safe_relative_path(path)

    def test_unicode_normalization_equivalents_are_preserved(self):
        allocator = PathAllocator()
        composed = "http://example.com/café"
        decomposed = "http://example.com/café"
        assert unicodedata.normalize("NFC", composed) == unicodedata.normalize(
            "NFC", decomposed
        )
        path1, path2 = allocator.assign(composed), allocator.assign(decomposed)
        assert path1 != path2
        assert allocator.manifest[path1] == composed
        assert allocator.manifest[path2] == decomposed

    def test_existing_paths_are_avoided(self):
        first = PathAllocator()
        taken = first.assign("http://example.com/page")
        second = PathAllocator(existing=[taken])
        path = second.assign("http://example.com/page")
        assert path != taken
        assert second.manifest[path] == "http://example.com/page"

    def test_loaded_manifest_paths_are_not_reassigned(self):
        first = PathAllocator()
        first.assign("http://example.com/a")
        second = PathAllocator(manifest=first.manifest)
        # A different URL whose slug collides must not reuse the loaded path.
        path = second.assign("http://example.com/a?x=1")
        assert (
            path not in first.manifest
            or first.manifest[path] == "http://example.com/a?x=1"
        )

    def test_invalid_limits_rejected(self):
        with pytest.raises(ValueError, match="hash_length"):
            PathAllocator(hash_length=4)
        with pytest.raises(ValueError, match="max_component_length"):
            PathAllocator(hash_length=12, max_component_length=10)
        with pytest.raises(ValueError, match="max_path_length"):
            PathAllocator(max_component_length=80, max_path_length=40)


class TestManifestIO:
    def test_round_trip(self, tmp_path):
        manifest = {"example.com/café-0123456789ab": "http://example.com/café"}
        file = tmp_path / "m.json"
        save_manifest(manifest, file)
        assert load_manifest(file) == manifest
        # Kept human-readable and recoverable.
        assert "café" in file.read_text(encoding="utf-8")

    def test_load_rejects_non_manifest(self, tmp_path):
        file = tmp_path / "m.json"
        file.write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")
        with pytest.raises(ValueError, match="manifest"):
            load_manifest(file)


class TestRealFileCreation:
    URLS = [
        "http://example.com/",
        "http://example.com/a<b",
        "http://example.com/a>b",
        "http://example.com/README",
        "http://example.com/readme",
        "http://example.com/café",
        "http://example.com/café",
        "http://example.com/" + "long " * 100,
        "http://example.com/CON",
        "http://example.com/file.",
        "https://例え.jp/パス?クエリ=値",
        "http://example.com/a?x=" + "1" * 3000,
    ]

    def test_assigned_paths_are_creatable(self, tmp_path):
        allocator = PathAllocator()
        allocator.assign_all(self.URLS)
        manifest = allocator.manifest
        assert len(set(manifest)) == len(self.URLS)
        created = ensure_creatable(manifest, tmp_path)
        assert len(created) == len(manifest)
        for relpath, url in manifest.items():
            target = tmp_path.joinpath(*relpath.split("/"))
            assert target.is_file()
            # The manifest recovers the original address for the file.
            assert url in self.URLS

    def test_creation_is_case_and_normalization_safe(self, tmp_path):
        allocator = PathAllocator()
        allocator.assign_all(self.URLS)
        folded = [
            unicodedata.normalize("NFC", path).casefold() for path in allocator.manifest
        ]
        assert len(set(folded)) == len(folded)

    def test_existing_file_is_never_clobbered(self, tmp_path):
        allocator = PathAllocator()
        path = allocator.assign("http://example.com/page")
        target = tmp_path.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True)
        target.write_text("original", encoding="utf-8")
        with pytest.raises(FileExistsError):
            ensure_creatable([path], tmp_path)
        assert target.read_text(encoding="utf-8") == "original"

    def test_unsafe_path_rejected_before_creation(self, tmp_path):
        with pytest.raises(ValueError, match="unsafe"):
            ensure_creatable(["../escape"], tmp_path)
        with pytest.raises(ValueError, match="unsafe"):
            ensure_creatable(["con"], tmp_path)
