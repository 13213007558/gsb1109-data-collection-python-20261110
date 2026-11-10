"""
Utilities dedicated to URL sampling.

In addition to the classic per-domain :func:`sample_urls`, this module provides
:func:`stratified_sample`, which splits a total budget across strata defined by
site, path category and source. Every draw uses seed-derived, content-based
hashing, so a run only depends on the input set and the seed: input order,
split batches and incremental imports all produce the same result.
"""

import hashlib
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from random import sample
from typing import Any, cast

from .clean import normalize_and_split
from .filters import lang_filter, validate_url
from .urlstore import UrlStore

LOGGER = logging.getLogger(__name__)

# sources that cannot be mapped to a stratum fall back to this label
DEFAULT_SOURCE = "unknown"


def _make_sample(
    urlstore: UrlStore,
    samplesize: int,
    exclude_min: int | None,
    exclude_max: int | None,
    verbose: bool = False,
) -> list[str]:
    "Iterate through the hosts in store and draw samples."
    output_urls = []
    # WARNING shows without logging setup
    level = logging.WARNING if verbose else logging.INFO
    for domain in urlstore.urldict:  # key=cmp_to_key(locale.strcoll)
        urlpaths = [p.path() for p in urlstore._load_urls(domain) if p.urlpath != b"/"]
        # too few or too many URLs
        if (
            not urlpaths
            or exclude_min is not None
            and len(urlpaths) < exclude_min
            or exclude_max is not None
            and len(urlpaths) > exclude_max
        ):
            LOGGER.log(level, "discarded (size): %s\t\turls: %s", domain, len(urlpaths))
            continue
        # sample
        if len(urlpaths) > samplesize:
            mysample = sorted(sample(urlpaths, k=samplesize))
        else:
            mysample = urlpaths
        output_urls.extend([domain + p for p in mysample])
        LOGGER.debug(
            "%s\t\turls: %s\tprop.: %s",
            domain,
            len(mysample),
            len(mysample) / len(urlpaths),
        )
    return output_urls


def sample_urls(
    input_urls: list[str],
    samplesize: int,
    exclude_min: int | None = None,
    exclude_max: int | None = None,
    strict: bool = False,
    verbose: bool = False,
) -> list[str]:
    """Sample a list of URLs by domain name, optionally using constraints on their number"""
    # store
    urlstore = UrlStore(compressed=True, language=None, strict=strict, verbose=verbose)
    urlstore.add_urls(input_urls)
    # return gathered URLs
    return _make_sample(urlstore, samplesize, exclude_min, exclude_max, verbose)


# ----------------------------------------------------------------------
# Stratified sampling
# ----------------------------------------------------------------------


PathCategoryFn = Callable[[str], str]
SourceFn = Callable[[str], str]


def path_category(path: str) -> str:
    """Default path category: the first non-empty segment of the URL path.

    Root and empty paths map to ``"root"`` so a category is always returned.
    The mapping is order-independent and safe on normalized URL paths.
    """
    segments = path.split("/")
    for segment in segments:
        if segment:
            return segment
    return "root"


def _source_label(source: object) -> str:
    "Turn a source value into a stable, hashable stratum label."
    if source is None:
        return DEFAULT_SOURCE
    return str(source)


@dataclass(frozen=True)
class StratumKey:
    "Identifier of a stratum: site, path category and source."

    site: str
    category: str
    source: str

    def as_tuple(self) -> tuple[str, str, str]:
        "Return the key as a plain tuple, in stratum order."
        return (self.site, self.category, self.source)


@dataclass
class Stratum:
    "One stratum: the URLs it contains, its pool size and the quota it finally gets."

    key: StratumKey
    urls: list[str] = field(default_factory=list)
    quota: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def pool_size(self) -> int:
        "Number of candidate URLs in the stratum."
        return len(self.urls)

    @property
    def selected(self) -> int:
        "Number of URLs actually selected, never above the quota or the pool."
        return min(self.quota, self.pool_size)


@dataclass(frozen=True)
class StratifiedResult:
    """Outcome of a stratified sampling run.

    ``quotas``, ``selected_counts`` and ``reasons`` are keyed by the same
    ``(site, category, source)`` tuples, sorted for stable reporting.
    ``notes`` carries run-level messages such as an unused global budget.
    """

    urls: list[str]
    budget: int
    total_selected: int
    quotas: dict[tuple[str, str, str], int]
    selected_counts: dict[tuple[str, str, str], int]
    reasons: dict[tuple[str, str, str], list[str]]
    seed: int
    manifest: dict[str, object]
    notes: list[str] = field(default_factory=list)

    def counts_by_stratum(self) -> dict[tuple[str, str, str], int]:
        "Return the number of selected URLs per stratum key."
        return dict(self.selected_counts)


def _split_url(
    url: str,
    strict: bool,
    language: str | None,
    trailing_slash: bool,
) -> tuple[str, str, str] | None:
    "Return the normalized (url, site key, path) triple, None if rejected."
    _, parsed = validate_url(url)
    if parsed is None:
        return None
    try:
        return normalize_and_split(parsed, strict, language, trailing_slash)
    except ValueError:
        return None


def _url_score(seed: int, key: StratumKey, url: str) -> int:
    """Assign a stable pseudo-random score to a URL within a stratum.

    The score depends only on the seed, the stratum and the URL itself, so it
    does not move when other URLs are added or the input is reordered.
    """
    digest = hashlib.sha256(
        repr((seed,) + key.as_tuple() + (url,)).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:16], "big")


def _largest_remainder(
    weights: dict[StratumKey, int], budget: int
) -> dict[StratumKey, int]:
    "Allocate an integer budget proportionally to weights, remainders first."
    total = sum(weights.values())
    if total == 0 or budget <= 0:
        return {key: 0 for key in weights}
    floors = {key: budget * weight // total for key, weight in weights.items()}
    remainder = budget - sum(floors.values())
    # deterministic tie breaking: largest remainder, then lexicographic key
    order = sorted(
        weights,
        key=lambda key: (-(budget * weights[key] % total), key.as_tuple()),
    )
    allocations = dict(floors)
    for key in order[:remainder]:
        allocations[key] += 1
    return allocations


def _assign_quotas(
    strata: dict[StratumKey, Stratum],
    budget: int,
    minimum: int,
    weights: dict[StratumKey, int] | None,
) -> list[str]:
    """Allocate the budget across strata, honoring per-stratum minima and pools.

    Quotas never exceed the available pool, so the total stays within the
    budget. Unfulfilled minima are explained per stratum and an unused global
    budget is returned as a run-level note.
    """
    keys = sorted(strata, key=lambda key: key.as_tuple())
    for key in keys:
        strata[key].quota = 0

    notes: list[str] = []
    if not keys:
        if budget:
            notes.append(f"no stratum available: budget {budget} unused")
        return notes
    if budget <= 0:
        for key in keys:
            strata[key].reasons.append("empty budget: no quota assigned")
        return notes

    pool = {key: strata[key].pool_size for key in keys}

    # phase 1: guaranteed floor for every stratum, capped by its pool and by
    # the remaining budget; every reduction is recorded with its cause
    spent = 0
    if minimum > 0:
        for key in keys:
            available = budget - spent
            granted = min(minimum, pool[key], available)
            strata[key].quota = granted
            spent += granted
            if granted < minimum:
                if available < minimum and available <= pool[key]:
                    strata[key].reasons.append(
                        f"minimum {minimum} reduced to {granted}: budget exhausted"
                    )
                else:
                    strata[key].reasons.append(
                        f"minimum {minimum} reduced to {granted}: "
                        f"only {pool[key]} URL(s) available"
                    )

    # phase 2: share the remaining budget proportionally to the requested
    # weights, or to the pool sizes when no weights are supplied; strata that
    # cannot take their share give the slots back in a bounded number of rounds
    remaining = budget - spent
    while remaining > 0:
        candidates = [key for key in keys if strata[key].quota < pool[key]]
        if not candidates:
            break
        demand = {
            key: (weights[key] if weights and key in weights else pool[key])
            for key in candidates
        }
        shares = _largest_remainder(demand, remaining)
        progressed = False
        for key in candidates:
            headroom = pool[key] - strata[key].quota
            granted = min(shares.get(key, 0), headroom)
            if granted:
                strata[key].quota += granted
                remaining -= granted
                progressed = True
        if not progressed:
            break

    total = sum(strata[key].quota for key in keys)
    if total < budget:
        notes.append(
            f"budget {budget} not fully used: {budget - total} slot(s) left, "
            "all strata reached their pool size"
        )
    return notes


def _build_strata(
    records: Iterable[tuple[str, object]],
    strict: bool,
    language: str | None,
    trailing_slash: bool,
    category_fn: PathCategoryFn,
) -> dict[StratumKey, Stratum]:
    "Normalize the input once and group the URLs into strata."
    strata: dict[StratumKey, Stratum] = {}
    # sets keep the grouping independent of the input order and duplicates
    seen: dict[StratumKey, set[str]] = {}
    for url, source in records:
        split = _split_url(url, strict, language, trailing_slash)
        if split is None:
            LOGGER.warning("Discarding URL: %s", url)
            continue
        normalized, site, path = split
        if (
            language is not None
            and lang_filter(normalized, language, strict, trailing_slash) is False
        ):
            LOGGER.warning("Discarding URL: %s", url)
            continue
        key = StratumKey(site, category_fn(path), _source_label(source))
        stratum = strata.get(key)
        if stratum is None:
            stratum = Stratum(key)
            strata[key] = stratum
            seen[key] = set()
        if normalized not in seen[key]:
            seen[key].add(normalized)
            stratum.urls.append(normalized)
    return strata


def _draw_stratum(stratum: Stratum, seed: int) -> list[str]:
    """Select the stratum quota from fixed per-URL scores.

    Keeping the URLs with the lowest scores means an added URL can only enter
    the sample by out-scoring a currently selected one: existing selections are
    preserved unless the pool itself forces a change.
    """
    pool = sorted(stratum.urls)
    if stratum.quota >= len(pool):
        return list(pool)
    ranked = sorted(
        pool,
        key=lambda url: (_url_score(seed, stratum.key, url), url),
    )
    return sorted(ranked[: stratum.quota])


def _build_manifest(
    strata: dict[StratumKey, Stratum],
    seed: int,
    budget: int,
    minimum: int,
    weights: dict[StratumKey, int] | None,
    notes: Sequence[str],
) -> dict[str, object]:
    "Assemble a rerunnable description of the whole sampling run."
    entries = []
    for key in sorted(strata, key=lambda item: item.as_tuple()):
        stratum = strata[key]
        entry: dict[str, object] = {
            "site": key.site,
            "category": key.category,
            "source": key.source,
            "pool_size": stratum.pool_size,
            "quota": stratum.quota,
            "selected": stratum.selected,
            "weight": weights.get(key, stratum.pool_size)
            if weights
            else stratum.pool_size,
            "reasons": list(stratum.reasons),
            "urls": sorted(stratum.urls),
        }
        entries.append(entry)
    return {
        "courlan_sampling": "stratified",
        "version": 1,
        "seed": seed,
        "budget": budget,
        "minimum_coverage": minimum,
        "total_selected": sum(stratum.selected for stratum in strata.values()),
        "notes": list(notes),
        "strata": entries,
    }


def stratified_sample(
    input_urls: Sequence[str] | Sequence[tuple[str, object]],
    budget: int,
    *,
    seed: int = 0,
    minimum: int = 0,
    weights: dict[tuple[str, str, str], int] | None = None,
    category_fn: PathCategoryFn = path_category,
    source_fn: SourceFn | None = None,
    strict: bool = False,
    language: str | None = None,
    trailing_slash: bool = True,
) -> StratifiedResult:
    """Sample a URL collection under a global, stratified budget.

    Args:
        input_urls: URLs as plain strings, or ``(url, source)`` pairs. Sources
            can also be derived from each URL through ``source_fn``; explicit
            pair sources take precedence.
        budget: maximum total number of selected URLs. The result never
            exceeds it, even when the strata cannot fill their quotas.
        seed: integer seed; the same input set and seed always yield the same
            sample, regardless of input order or batch boundaries.
        minimum: minimum coverage attempted for every stratum. It is capped by
            the stratum pool and by the remaining budget; every reduction is
            explained in ``reasons`` instead of silently overspending.
        weights: optional relative budget shares keyed by
            ``(site, category, source)``. Missing strata fall back to their
            pool size; zero and negative values are rejected.
        category_fn: maps a normalized URL path to a category,
            :func:`path_category` (first path segment) by default.
        source_fn: optional callable deriving a source label from a raw URL.
        strict: stricter URL filtering, as in :func:`sample_urls`.
        language: optional ISO 639-1 language filter.
        trailing_slash: preserve trailing slashes while normalizing.

    Returns:
        A :class:`StratifiedResult` with the selected URLs (sorted), the quota
        and selected count per stratum, human-readable reasons for unmet
        constraints, and a rerunnable manifest.
    """
    if budget < 0:
        raise ValueError("budget must be a non-negative integer")
    if minimum < 0:
        raise ValueError("minimum must be a non-negative integer")
    if not isinstance(seed, int):
        raise TypeError("seed must be an integer")

    weight_map: dict[StratumKey, int] | None = None
    if weights is not None:
        weight_map = {}
        for raw_key, weight in weights.items():
            if not isinstance(weight, int) or weight <= 0:
                raise ValueError(f"weight for {raw_key!r} must be a positive integer")
            weight_map[StratumKey(*raw_key)] = weight

    records: list[tuple[str, object]] = []
    for item in input_urls:
        if isinstance(item, (tuple, list)):
            url, source = item
        else:
            url = item
            source = source_fn(item) if source_fn is not None else None
        records.append((url, source))

    strata = _build_strata(records, strict, language, trailing_slash, category_fn)
    notes = _assign_quotas(strata, budget, minimum, weight_map)

    selected: list[str] = []
    for key in sorted(strata, key=lambda item: item.as_tuple()):
        selected.extend(_draw_stratum(strata[key], seed))
    selected.sort()

    ordered_keys = sorted(strata, key=lambda item: item.as_tuple())
    quotas = {key.as_tuple(): strata[key].quota for key in ordered_keys}
    counts = {key.as_tuple(): strata[key].selected for key in ordered_keys}
    reasons = {
        key.as_tuple(): list(strata[key].reasons)
        for key in ordered_keys
        if strata[key].reasons
    }
    manifest = _build_manifest(strata, seed, budget, minimum, weight_map, notes)

    return StratifiedResult(
        urls=selected,
        budget=budget,
        total_selected=len(selected),
        quotas=quotas,
        selected_counts=counts,
        reasons=reasons,
        seed=seed,
        manifest=manifest,
        notes=notes,
    )


def _manifest_int(manifest: dict[str, object], key: str) -> int:
    "Read an integer field from a manifest, turning type errors into ValueError."
    try:
        return int(cast(Any, manifest[key]))
    except (KeyError, TypeError, ValueError) as err:
        raise ValueError("invalid stratified sampling manifest") from err


def resample_stratified(
    manifest: dict[str, object],
    *,
    seed: int | None = None,
    budget: int | None = None,
    minimum: int | None = None,
) -> StratifiedResult:
    """Rebuild a stratified sample from a manifest, by default identically.

    The manifest stores every candidate URL per stratum, so a run can be
    replayed without the original input. Overriding ``seed`` draws again from
    the same pools; overriding ``budget`` or ``minimum`` recomputes the quotas
    with the recorded pool sizes and weights.
    """
    try:
        entries = cast(list[dict[str, Any]], manifest["strata"])
    except (KeyError, TypeError) as err:
        raise ValueError("invalid stratified sampling manifest") from err
    old_seed = _manifest_int(manifest, "seed")
    old_budget = _manifest_int(manifest, "budget")
    old_minimum = _manifest_int(manifest, "minimum_coverage")

    chosen_seed = old_seed if seed is None else seed
    chosen_budget = old_budget if budget is None else budget
    chosen_minimum = old_minimum if minimum is None else minimum
    if chosen_budget < 0 or chosen_minimum < 0:
        raise ValueError("budget and minimum must be non-negative integers")

    strata: dict[StratumKey, Stratum] = {}
    weight_map: dict[StratumKey, int] = {}
    for entry in entries:
        try:
            key = StratumKey(
                str(entry["site"]), str(entry["category"]), str(entry["source"])
            )
            urls = [str(url) for url in entry["urls"]]
            weight = entry.get("weight", len(urls))
        except (KeyError, TypeError) as err:
            raise ValueError("invalid stratified sampling manifest") from err
        stratum = Stratum(key)
        stratum.urls = urls
        strata[key] = stratum
        if isinstance(weight, int) and weight > 0:
            weight_map[key] = weight

    notes = _assign_quotas(strata, chosen_budget, chosen_minimum, weight_map)
    selected: list[str] = []
    for key in sorted(strata, key=lambda item: item.as_tuple()):
        selected.extend(_draw_stratum(strata[key], chosen_seed))
    selected.sort()

    ordered_keys = sorted(strata, key=lambda item: item.as_tuple())
    quotas = {key.as_tuple(): strata[key].quota for key in ordered_keys}
    counts = {key.as_tuple(): strata[key].selected for key in ordered_keys}
    reasons = {
        key.as_tuple(): list(strata[key].reasons)
        for key in ordered_keys
        if strata[key].reasons
    }
    new_manifest = _build_manifest(
        strata, chosen_seed, chosen_budget, chosen_minimum, weight_map, notes
    )
    return StratifiedResult(
        urls=selected,
        budget=chosen_budget,
        total_selected=len(selected),
        quotas=quotas,
        selected_counts=counts,
        reasons=reasons,
        seed=chosen_seed,
        manifest=new_manifest,
        notes=notes,
    )
