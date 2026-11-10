"""
Tests for the stratified URL sampling features.
"""

import json
import random

import pytest

from courlan import (
    StratifiedResult,
    path_category,
    resample_stratified,
    sample_urls,
    stratified_sample,
)


def _urls(host, prefix, count):
    "Build a deterministic collection of URLs for one host/category."
    return [f"https://{host}/{prefix}/page-{i:04d}" for i in range(count)]


def test_path_category():
    "The first non-empty segment defines the default category, root otherwise."
    assert path_category("/news/story") == "news"
    assert path_category("/shop/") == "shop"
    assert path_category("/") == "root"
    assert path_category("") == "root"


def test_basic_stratification_counts():
    "One stratum per (site, category, source) with per-stratum counts."
    urls = _urls("a.example", "news", 30) + _urls("b.example", "shop", 20)
    result = stratified_sample(urls, budget=25, seed=11)
    assert isinstance(result, StratifiedResult)
    assert result.total_selected == 25
    assert len(result.urls) == 25
    assert len(result.urls) == len(set(result.urls))
    assert sum(result.selected_counts.values()) == 25
    assert sum(result.quotas.values()) == 25
    # proportional to the pools: 30/50 and 20/50 of 25
    assert result.selected_counts[("https://a.example", "news", "unknown")] == 15
    assert result.selected_counts[("https://b.example", "shop", "unknown")] == 10


def test_large_site_does_not_drown_small_one():
    "Without a minimum the big site takes nearly everything; with it, the small site survives."
    big = _urls("big.example", "news", 500)
    small = _urls("small.example", "news", 4)
    urls = big + small

    unfair = stratified_sample(urls, budget=10, seed=3)
    assert unfair.selected_counts[("https://small.example", "news", "unknown")] == 0

    fair = stratified_sample(urls, budget=10, seed=3, minimum=2)
    assert fair.selected_counts[("https://small.example", "news", "unknown")] == 2
    assert fair.total_selected == 10
    # every selected URL really comes from its declared stratum
    for url in fair.urls:
        assert url.startswith(("https://big.example/", "https://small.example/"))


def test_budget_is_never_exceeded():
    "Pools smaller than the budget simply leave unused slots, with a note."
    result = stratified_sample(_urls("a.example", "news", 3), budget=100, seed=1)
    assert result.total_selected == 3
    assert all(count <= 3 for count in result.selected_counts.values())
    assert result.notes and "not fully used" in result.notes[0]
    assert result.manifest["notes"] == result.notes


def test_minimum_capped_by_pool_reports_reason():
    "A stratum with too few URLs gets what exists and a reason, never an overshoot."
    urls = _urls("a.example", "news", 2) + _urls("b.example", "news", 40)
    result = stratified_sample(urls, budget=20, seed=2, minimum=10)
    small_key = ("https://a.example", "news", "unknown")
    assert result.quotas[small_key] == 2
    assert result.reasons[small_key]
    assert "only 2 URL(s) available" in result.reasons[small_key][0]
    assert result.total_selected == 20


def test_minimum_capped_by_budget_reports_reason():
    "When the budget runs out first, the reason says so instead of overspending."
    urls = _urls("a.example", "news", 40) + _urls("b.example", "news", 40)
    result = stratified_sample(urls, budget=3, seed=2, minimum=5)
    assert result.total_selected == 3
    reasons = " ".join(
        reason for values in result.reasons.values() for reason in values
    )
    assert "budget exhausted" in reasons
    assert all(count <= 5 for count in result.selected_counts.values())


def test_zero_and_empty_budget():
    urls = _urls("a.example", "news", 10)
    assert stratified_sample(urls, budget=0).urls == []
    empty = stratified_sample([], budget=10)
    assert empty.urls == []
    assert empty.total_selected == 0
    assert empty.notes and "unused" in empty.notes[0]


def test_order_independence():
    "The same set and seed give the same output in any input order."
    urls = _urls("a.example", "news", 50) + _urls("b.example", "shop", 30)
    reference = stratified_sample(urls, budget=30, seed=99, minimum=2)
    shuffled = urls[:]
    random.Random(2026).shuffle(shuffled)
    shuffled_result = stratified_sample(shuffled, budget=30, seed=99, minimum=2)
    assert shuffled_result.urls == reference.urls
    assert shuffled_result.quotas == reference.quotas
    assert shuffled_result.selected_counts == reference.selected_counts


def test_batch_import_reproducibility():
    "Feeding the set whole or in arbitrary batches is equivalent."
    urls = _urls("a.example", "news", 40) + _urls("b.example", "shop", 25)
    reference = stratified_sample(urls, budget=20, seed=7)
    batches = [urls[i::3] for i in range(3)]
    batched = stratified_sample(
        [url for batch in batches for url in batch], budget=20, seed=7
    )
    assert batched.urls == reference.urls


def test_incremental_addition_keeps_existing_sample():
    "A few new URLs must not replace selections that still qualify."
    urls = _urls("a.example", "news", 100)
    base = stratified_sample(urls, budget=20, seed=5)
    grown = stratified_sample(
        urls + ["https://a.example/news/extra-1", "https://a.example/news/extra-2"],
        budget=20,
        seed=5,
    )
    retained = set(base.urls) & set(grown.urls)
    assert len(retained) >= 18
    assert set(grown.urls).issubset(
        set(urls)
        | {
            "https://a.example/news/extra-1",
            "https://a.example/news/extra-2",
        }
    )


def test_seed_controls_randomness():
    "Same seed is stable, another seed draws a different but valid sample."
    urls = _urls("a.example", "news", 100)
    first = stratified_sample(urls, budget=20, seed=4)
    second = stratified_sample(urls, budget=20, seed=4)
    other = stratified_sample(urls, budget=20, seed=8)
    assert first.urls == second.urls
    assert first.urls != other.urls
    assert len(other.urls) == 20


def test_sources_and_deduplication():
    "Explicit pairs, derived sources, duplicates and unknown sources all behave."
    pairs = [
        ("https://a.example/news/1", "sitemap"),
        ("https://a.example/news/1", "sitemap"),  # duplicate within a stratum
        ("https://a.example/news/2", "crawl"),
    ]
    result = stratified_sample(pairs, budget=10, seed=0)
    keys = set(result.selected_counts)
    assert ("https://a.example", "news", "sitemap") in keys
    assert ("https://a.example", "news", "crawl") in keys
    # the duplicate counts once: two unique URLs over two strata
    assert result.total_selected == 2
    sitemap_pool = next(
        entry["pool_size"]
        for entry in result.manifest["strata"]
        if entry["source"] == "sitemap"
    )
    assert sitemap_pool == 1

    derived = stratified_sample(
        ["https://a.example/news/1"], budget=5, source_fn=lambda url: "feed"
    )
    assert ("https://a.example", "news", "feed") in derived.quotas


def test_custom_category_function():
    urls = ["https://a.example/en/story", "https://a.example/de/meldung"]
    result = stratified_sample(
        urls, budget=10, category_fn=lambda path: path.split("/")[1] or "root"
    )
    categories = {key[1] for key in result.selected_counts}
    assert categories == {"de", "en"}


def test_weights_prioritize_a_stratum():
    "A high relative weight redirects budget toward the chosen stratum."
    urls = _urls("a.example", "news", 50) + _urls("b.example", "news", 50)
    key = ("https://b.example", "news", "unknown")
    weighted = stratified_sample(urls, budget=10, seed=4, weights={key: 99})
    plain = stratified_sample(urls, budget=10, seed=4)
    assert weighted.quotas[key] > plain.quotas[key]
    assert weighted.manifest["strata"][1]["weight"] == 99


def test_invalid_arguments():
    urls = _urls("a.example", "news", 5)
    with pytest.raises(ValueError):
        stratified_sample(urls, budget=-1)
    with pytest.raises(ValueError):
        stratified_sample(urls, budget=5, minimum=-1)
    with pytest.raises(TypeError):
        stratified_sample(urls, budget=5, seed=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        stratified_sample(
            urls,
            budget=5,
            weights={("https://a.example", "news", "unknown"): 0},
        )
    with pytest.raises(ValueError):
        stratified_sample(
            urls,
            budget=5,
            weights={("https://a.example", "news", "unknown"): -3},
        )


def test_invalid_urls_are_dropped():
    urls = ["https://a.example/news/1", "not-a-url", "ftp://a.example/x"]
    result = stratified_sample(urls, budget=10, seed=0)
    assert result.urls == ["https://a.example/news/1"]


def test_manifest_is_rerunnable_and_serializable():
    urls = _urls("a.example", "news", 30) + _urls("b.example", "shop", 10)
    result = stratified_sample(urls, budget=15, seed=12, minimum=1)
    # plain JSON, so the manifest can be stored and shared
    encoded = json.dumps(result.manifest)
    assert json.loads(encoded)["seed"] == 12

    rerun = resample_stratified(result.manifest)
    assert rerun.urls == result.urls
    assert rerun.quotas == result.quotas
    assert rerun.selected_counts == result.selected_counts

    reseeded = resample_stratified(result.manifest, seed=123)
    assert reseeded.urls != result.urls
    assert len(reseeded.urls) == result.total_selected

    resized = resample_stratified(result.manifest, budget=5)
    assert resized.total_selected == 5
    assert resized.budget == 5


def test_manifest_rejects_garbage():
    with pytest.raises(ValueError):
        resample_stratified({"seed": 1})
    with pytest.raises(ValueError):
        resample_stratified({"strata": [{"site": "x"}]})


def test_legacy_sampling_still_works():
    "The original per-domain entry point keeps its behavior."
    urls = _urls("a.example", "news", 20) + _urls("b.example", "news", 20)
    result = sample_urls(urls, samplesize=5)
    assert len(result) == 10
    assert all(url.startswith("https://") for url in result)
