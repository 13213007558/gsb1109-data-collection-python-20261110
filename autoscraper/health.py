"""Optional rule health checks.

Rules learned by :class:`autoscraper.AutoScraper` describe the *structure*
of a page. They keep matching as long as that structure stays the same, even
when the textual content changes completely. This module records what the
rules looked like on the training pages (their explainable structure, how
often they matched and what kind of values they returned) and compares new
pages against that baseline, flagging rules worth a manual review.

The checks are deliberately read-only: they never retrain, add, remove or
replace any rule. A page that merely shows different products is healthy;
content drift on its own is not reported as a failure. An unknown page is
never treated as a success either -- every flag needs evidence, and every
threshold has to be supplied explicitly by the caller.
"""

from collections import Counter
from statistics import median

from autoscraper.utils import unique_hashable


# Drift kinds. They are constants instead of free-form strings so callers can
# branch on them programmatically.
DRIFT_MISSING = "missing"
DRIFT_EXPANSION = "expansion"
DRIFT_STRUCTURE = "structure_change"
DRIFT_UNKNOWN_PAGE = "unknown_page"
DRIFT_LOW_COVERAGE = "low_coverage"
DRIFT_RESULT_SHAPE = "result_shape_change"


class RuleHealthCheck(object):
    """Compares runtime rule behaviour against a recorded baseline.

    Parameters
    ----------
    scraper: AutoScraper
        The scraper whose ``stack_list`` is monitored. The check never
        mutates it.
    thresholds: dict
        Caller-supplied thresholds. All of them are optional and nothing is
        reported for a dimension whose threshold is not provided:

        - ``min_match_count`` (int): report ``missing`` when a rule matches
          fewer than this many elements on a sample page.
        - ``max_match_count`` (int): report ``expansion`` when a rule matches
          more than this many elements.
        - ``min_coverage`` (float, 0..1): report ``low_coverage`` when fewer
          than this fraction of baseline rules produce any result.
        - ``max_unknown_fraction`` (float, 0..1): report ``unknown_page``
          when at least this fraction of rules match no element at all.
        - ``max_depth_change`` (int): for a rule that no longer resolves,
          report ``structure_change`` when the recorded path breaks more than
          this many levels above the leaf (the element moved in the tree).
        - ``max_tag_jaccard_distance`` (float, 0..1): for a rule that no
          longer resolves, report ``structure_change`` when the closest
          surviving leaf-tag path differs from the baseline by more than this
          Jaccard distance. If the leaf tag is gone from the page altogether
          no structure flag is raised; that is an unknown page instead.
        - ``max_digit_fraction_change`` (float, 0..1): report
          ``result_shape_change`` when the share of digit characters in the
          matched values shifts by more than this value. This distinguishes a
          product title (mostly letters) from a price or promo banner (mostly
          digits/symbols) without caring about the exact wording.
        - ``max_blank_fraction_change`` (float, 0..1): report
          ``result_shape_change`` when the fraction of blank matches changes
          by more than this value.
        - ``max_length_ratio_change`` (float): report ``result_shape_change``
          when the median result length ratio (observed / baseline) is outside
          the interval ``[1 - change, 1 + change]``.
    """

    def __init__(self, scraper, thresholds=None):
        self.scraper = scraper
        self.thresholds = dict(thresholds or {})
        self._validate_thresholds(self.thresholds)
        # rule_id -> baseline descriptor, see _describe_rule
        self.baselines = {}
        # aggregate stats over every page used for recording
        self.baseline_summary = {}

    # ------------------------------------------------------------------
    # threshold helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_thresholds(thresholds):
        ratio_keys = (
            "min_coverage",
            "max_unknown_fraction",
            "max_tag_jaccard_distance",
            "max_digit_fraction_change",
            "max_blank_fraction_change",
        )
        for key in ratio_keys:
            if key in thresholds and not 0.0 <= thresholds[key] <= 1.0:
                raise ValueError("%s must be within [0, 1]" % key)
        for key in ("min_match_count", "max_match_count", "max_depth_change"):
            if key in thresholds and thresholds[key] < 0:
                raise ValueError("%s must be non-negative" % key)
        if "max_length_ratio_change" in thresholds and thresholds[
            "max_length_ratio_change"
        ] < 0:
            raise ValueError("max_length_ratio_change must be non-negative")

    def _threshold(self, key):
        return self.thresholds.get(key)

    # ------------------------------------------------------------------
    # structural description of a rule
    # ------------------------------------------------------------------
    @staticmethod
    def _rule_path(stack):
        """The explainable tag path of a rule, document root excluded."""
        return [item[0] for item in stack["content"] if item[0] != "[document]"]

    @staticmethod
    def _rule_attributes(stack):
        """Tag name -> sorted (attribute, value-pattern) pairs on the path."""
        description = []
        for item in stack["content"]:
            if item[0] == "[document]":
                continue
            attrs = item[1] or {}
            description.append(
                (item[0], tuple(sorted((k, v) for k, v in attrs.items() if v)))
            )
        return tuple(description)

    def _describe_rule(self, stack):
        path = self._rule_path(stack)
        return {
            "stack_id": stack["stack_id"],
            "alias": stack.get("alias", ""),
            "path": path,
            "depth": len(path),
            "tag_set": unique_hashable(path),
            "attributes": self._rule_attributes(stack),
            "wanted_attr": stack.get("wanted_attr"),
            "is_full_url": bool(stack.get("is_full_url")),
            "is_non_rec_text": bool(stack.get("is_non_rec_text")),
        }

    # ------------------------------------------------------------------
    # observation of a rule against a page
    # ------------------------------------------------------------------
    def _observe_rule(self, stack, soup, url, attr_fuzz_ratio):
        """Run one rule on one parsed page and describe what happened."""
        if not url:
            url = stack.get("url", "")
        # sibling leaves are included on purpose: the health check wants the
        # full set of elements the structural rule selects, so that bleeding
        # into extra promo elements shows up as an expanded match count
        items = self.scraper._get_result_with_stack(
            stack,
            soup,
            url,
            attr_fuzz_ratio,
            keep_blank=True,
            contain_sibling_leaves=True,
        )
        values = [item.text for item in items]
        non_blank = [v for v in values if v]
        path = self._observed_path(stack, soup, url, attr_fuzz_ratio)
        observation = {
            "stack_id": stack["stack_id"],
            "match_count": len(items),
            "non_blank_count": len(non_blank),
            "values": non_blank,
            "path": path,
        }
        if path is None:
            observation["diagnosis"] = self._diagnose_unmatched(
                stack, soup, attr_fuzz_ratio
            )
        return observation

    def _observed_path(self, stack, soup, url, attr_fuzz_ratio):
        """Tag path of the first element the rule currently matches.

        Returns ``None`` when the rule matches nothing, which is itself the
        signal for a missing rule; callers then use
        ``_diagnose_unmatched`` to explain why.
        """
        parents = [soup]
        for index, item in enumerate(stack["content"]):
            if item[0] == "[document]":
                continue
            children = []
            attrs = item[1]
            if attr_fuzz_ratio < 1.0:
                attrs = self.scraper._get_fuzzy_attrs(attrs, attr_fuzz_ratio)
            for parent in parents:
                children += parent.findAll(item[0], attrs, recursive=False)
            if not children:
                return None
            parents = children
        leaf = parents[0]
        path = []
        node = leaf
        while node is not None and getattr(node, "name", None) not in (
            None,
            "[document]",
        ):
            path.append(node.name)
            node = getattr(node, "parent", None)
        path.reverse()
        return path

    def _diagnose_unmatched(self, stack, soup, attr_fuzz_ratio):
        """Explain why a rule matches nothing.

        The evidence is structural and explainable for a human reviewer: how
        far the recorded tag path still resolves from the document root,
        whether the leaf tag (with or without its attributes) still exists
        somewhere on the page, and what the paths of the surviving leaf tags
        look like. A redesign usually keeps the leaf tag while moving or
        re-classing it; an unknown page loses the tag altogether.
        """
        resolved_depth = 0
        parents = [soup]
        levels = [
            item for item in stack["content"] if item[0] != "[document]"
        ]
        for item in levels:
            attrs = item[1]
            if attr_fuzz_ratio < 1.0:
                attrs = self.scraper._get_fuzzy_attrs(attrs, attr_fuzz_ratio)
            children = []
            for parent in parents:
                children += parent.findAll(item[0], attrs, recursive=False)
            if not children:
                break
            resolved_depth += 1
            parents = children

        leaf = levels[-1]
        leaf_name = leaf[0]
        leaf_attrs = leaf[1]
        if attr_fuzz_ratio < 1.0:
            leaf_attrs = self.scraper._get_fuzzy_attrs(
                leaf_attrs, attr_fuzz_ratio
            )
        leaf_anywhere = soup.findAll(leaf_name, recursive=True)
        leaf_exact = soup.findAll(leaf_name, leaf_attrs, recursive=True)
        candidate_paths = [
            self._element_path(element) for element in leaf_anywhere[:2]
        ]
        return {
            "resolved_depth": resolved_depth,
            "baseline_depth": len(levels),
            "leaf_tag": leaf_name,
            "leaf_tag_count": len(leaf_anywhere),
            "leaf_exact_match_count": len(leaf_exact),
            "candidate_paths": candidate_paths,
        }

    @staticmethod
    def _element_path(element):
        path = []
        node = element
        while node is not None and getattr(node, "name", None) not in (
            None,
            "[document]",
        ):
            path.append(node.name)
            node = getattr(node, "parent", None)
        path.reverse()
        return path

    # ------------------------------------------------------------------
    # baseline recording
    # ------------------------------------------------------------------
    def record_baseline(self, samples):
        """Record structural and result-shape stats from training samples.

        Parameters
        ----------
        samples: list[dict]
            One dict per known-good training page. Each dict accepts the same
            keys as ``check`` (``url``/``html``/``soup``/``request_args`` and
            optional ``attr_fuzz_ratio``). Content text is expected to differ
            between pages and is only summarised by shape, never stored.
        Returns
        -------
        dict
            The recorded baseline summary. The same object is kept on
            ``self.baseline_summary`` and is used by later ``check`` calls.
        """
        if not self.scraper.stack_list:
            raise ValueError("scraper has no learned rules to record a baseline for")
        if not samples:
            raise ValueError("at least one training sample is required")

        descriptors = {
            stack["stack_id"]: self._describe_rule(stack)
            for stack in self.scraper.stack_list
        }

        observations = {rule_id: [] for rule_id in descriptors}
        for sample in samples:
            soup = sample.get("soup")
            if soup is None:
                soup = self.scraper._get_soup(
                    url=sample.get("url"),
                    html=sample.get("html"),
                    request_args=sample.get("request_args"),
                )
            url = sample.get("url")
            ratio = sample.get("attr_fuzz_ratio", 1.0)
            for stack in self.scraper.stack_list:
                observations[stack["stack_id"]].append(
                    self._observe_rule(stack, soup, url, ratio)
                )

        baselines = {}
        for rule_id, descriptor in descriptors.items():
            rule_observations = observations[rule_id]
            match_counts = [obs["match_count"] for obs in rule_observations]
            non_blank_counts = [obs["non_blank_count"] for obs in rule_observations]
            lengths = [
                len(value)
                for obs in rule_observations
                for value in obs["values"]
            ]
            digit_fractions = [
                self._digit_fraction(obs["values"]) for obs in rule_observations
            ]
            blank_fractions = [
                (obs["match_count"] - obs["non_blank_count"]) / obs["match_count"]
                if obs["match_count"]
                else 0.0
                for obs in rule_observations
            ]
            sample_matches = sum(1 for count in match_counts if count > 0)
            baselines[rule_id] = {
                "rule": descriptor,
                "sample_count": len(rule_observations),
                "matched_samples": sample_matches,
                "match_rate": sample_matches / len(rule_observations),
                "min_match_count": min(match_counts),
                "max_match_count": max(match_counts),
                "typical_match_count": int(median(match_counts)),
                "typical_non_blank_count": int(median(non_blank_counts)),
                "median_length": int(median(lengths)) if lengths else 0,
                "typical_digit_fraction": median(digit_fractions),
                "typical_blank_fraction": median(blank_fractions),
                "text_like": descriptor["wanted_attr"] is None,
            }

        self.baselines = baselines
        self.baseline_summary = {
            "rule_count": len(baselines),
            "sample_count": len(samples),
            "thresholds": dict(self.thresholds),
            "rules": baselines,
        }
        return self.baseline_summary

    # ------------------------------------------------------------------
    # runtime check
    # ------------------------------------------------------------------
    def check(self, url=None, html=None, soup=None, request_args=None,
              attr_fuzz_ratio=1.0):
        """Check every learned rule against one runtime page.

        The method is read-only: it runs the existing rules and compares
        their behaviour with the recorded baseline. It never retrains or
        modifies ``stack_list``. A page whose text merely changed (different
        product names/prices) while structure and result shapes hold is not
        flagged. A page on which most rules match nothing is reported as
        unknown rather than silently treated as a success.

        Returns
        -------
        dict
            ``healthy`` (bool), ``flags`` (list of evidence dicts, each with
            ``rule_id``/``alias``/``kind``/``evidence``), plus an
            ``observations`` section with the raw per-rule counts and paths
            that support the flags.
        """
        if not self.baselines:
            raise ValueError(
                "no baseline recorded; call record_baseline with training "
                "samples before check"
            )
        if soup is None:
            soup = self.scraper._get_soup(
                url=url, html=html, request_args=request_args
            )

        observations = {}
        for stack in self.scraper.stack_list:
            observations[stack["stack_id"]] = self._observe_rule(
                stack, soup, url, attr_fuzz_ratio
            )

        flags = []
        flags.extend(self._check_match_counts(observations))
        flags.extend(self._check_structure(observations))
        flags.extend(self._check_result_shape(observations))
        flags.extend(self._check_page_level(observations))

        return {
            "healthy": not flags,
            "flags": flags,
            "observations": self._public_observations(observations),
            "thresholds": dict(self.thresholds),
        }

    def _public_observations(self, observations):
        public = {}
        for rule_id, obs in observations.items():
            entry = {
                "match_count": obs["match_count"],
                "non_blank_count": obs["non_blank_count"],
                "sample_values": obs["values"][:3],
                "observed_path": obs["path"],
                "baseline_path": self.baselines[rule_id]["rule"]["path"],
            }
            if "diagnosis" in obs:
                entry["unmatched_diagnosis"] = obs["diagnosis"]
            public[rule_id] = entry
        return public

    # ------------------------------------------------------------------
    # individual drift dimensions
    # ------------------------------------------------------------------
    def _alias_for(self, rule_id):
        return self.baselines[rule_id]["rule"].get("alias", "")

    def _check_match_counts(self, observations):
        flags = []
        min_count = self._threshold("min_match_count")
        max_count = self._threshold("max_match_count")
        for rule_id, obs in observations.items():
            baseline = self.baselines[rule_id]
            if min_count is not None and obs["match_count"] < min_count:
                flags.append(
                    self._flag(
                        rule_id,
                        DRIFT_MISSING,
                        {
                            "match_count": obs["match_count"],
                            "threshold": min_count,
                            "baseline_min_match_count": baseline[
                                "min_match_count"
                            ],
                            "baseline_typical_match_count": baseline[
                                "typical_match_count"
                            ],
                        },
                    )
                )
            if max_count is not None and obs["match_count"] > max_count:
                flags.append(
                    self._flag(
                        rule_id,
                        DRIFT_EXPANSION,
                        {
                            "match_count": obs["match_count"],
                            "threshold": max_count,
                            "baseline_max_match_count": baseline[
                                "max_match_count"
                            ],
                            "baseline_typical_match_count": baseline[
                                "typical_match_count"
                            ],
                            "sample_values": obs["values"][:3],
                        },
                    )
                )
        return flags

    def _check_structure(self, observations):
        """Flag redesigned pages, distinguishing them from unknown pages.

        Only evaluated when a structure threshold is configured. A rule whose
        exact path no longer resolves but whose leaf tag still exists
        somewhere on the page is the signature of a redesign: the element
        moved or its wrapper changed. When the leaf tag disappears altogether
        there is no recognizable structure left, so the rule is reported only
        by the missing/unknown-page dimensions, never guessed back into place.
        """
        flags = []
        max_depth_change = self._threshold("max_depth_change")
        max_distance = self._threshold("max_tag_jaccard_distance")
        if max_depth_change is None and max_distance is None:
            return flags

        for rule_id, obs in observations.items():
            if obs["path"] is not None:
                continue
            diagnosis = obs.get("diagnosis")
            if not diagnosis or diagnosis["leaf_tag_count"] == 0:
                continue

            baseline_path = self.baselines[rule_id]["rule"]["path"]
            candidate_paths = diagnosis["candidate_paths"]
            if candidate_paths:
                closest_distance = min(
                    self._jaccard_distance(baseline_path, candidate)
                    for candidate in candidate_paths
                )
            else:
                closest_distance = 1.0
            depth_change = (
                diagnosis["baseline_depth"] - diagnosis["resolved_depth"]
            )

            changed = False
            if max_depth_change is not None and depth_change > max_depth_change:
                changed = True
            if max_distance is not None and closest_distance > max_distance:
                changed = True
            if not changed:
                continue

            flags.append(
                self._flag(
                    rule_id,
                    DRIFT_STRUCTURE,
                    {
                        "baseline_path": baseline_path,
                        "resolved_depth": diagnosis["resolved_depth"],
                        "depth_change": depth_change,
                        "leaf_tag": diagnosis["leaf_tag"],
                        "leaf_tag_count": diagnosis["leaf_tag_count"],
                        "leaf_exact_match_count": diagnosis[
                            "leaf_exact_match_count"
                        ],
                        "candidate_paths": candidate_paths,
                        "closest_tag_jaccard_distance": round(
                            closest_distance, 4
                        ),
                    },
                )
            )
        return flags

    def _check_result_shape(self, observations):
        flags = []
        max_digit_change = self._threshold("max_digit_fraction_change")
        max_blank_change = self._threshold("max_blank_fraction_change")
        max_length_change = self._threshold("max_length_ratio_change")
        if (
            max_digit_change is None
            and max_blank_change is None
            and max_length_change is None
        ):
            return flags

        for rule_id, obs in observations.items():
            baseline = self.baselines[rule_id]
            if obs["match_count"] == 0:
                continue
            evidence = {}
            changed = False

            if max_digit_change is not None:
                digit_fraction = self._digit_fraction(obs["values"])
                digit_change = abs(
                    digit_fraction - baseline["typical_digit_fraction"]
                )
                evidence["digit_fraction_change"] = round(digit_change, 4)
                if digit_change > max_digit_change:
                    changed = True

            if max_blank_change is not None:
                blank_fraction = (
                    obs["match_count"] - obs["non_blank_count"]
                ) / obs["match_count"]
                blank_change = abs(
                    blank_fraction - baseline["typical_blank_fraction"]
                )
                evidence["blank_fraction_change"] = round(blank_change, 4)
                if blank_change > max_blank_change:
                    changed = True

            if max_length_change is not None and baseline["median_length"]:
                observed_lengths = [len(v) for v in obs["values"]] or [0]
                length_ratio = (
                    median(observed_lengths) / baseline["median_length"]
                )
                evidence["median_length_ratio"] = round(length_ratio, 4)
                evidence["baseline_median_length"] = baseline["median_length"]
                if not (
                    1.0 - max_length_change
                    <= length_ratio
                    <= 1.0 + max_length_change
                ):
                    changed = True

            if changed:
                evidence["sample_values"] = obs["values"][:3]
                flags.append(self._flag(rule_id, DRIFT_RESULT_SHAPE, evidence))
        return flags

    @staticmethod
    def _digit_fraction(values):
        total_chars = sum(len(value) for value in values)
        if not total_chars:
            return 0.0
        digit_chars = sum(
            char.isdigit() for value in values for char in value
        )
        return digit_chars / total_chars

    def _check_page_level(self, observations):
        flags = []
        total = len(observations)
        zero_match = [
            rule_id for rule_id, obs in observations.items()
            if obs["match_count"] == 0
        ]
        coverage = (total - len(zero_match)) / total if total else 0.0
        unknown_fraction = len(zero_match) / total if total else 0.0

        min_coverage = self._threshold("min_coverage")
        if min_coverage is not None and coverage < min_coverage:
            flags.append(
                self._flag(
                    None,
                    DRIFT_LOW_COVERAGE,
                    {
                        "matched_rules": total - len(zero_match),
                        "total_rules": total,
                        "coverage": round(coverage, 4),
                        "threshold": min_coverage,
                        "unmatched_rules": zero_match,
                    },
                )
            )

        max_unknown = self._threshold("max_unknown_fraction")
        if max_unknown is not None and unknown_fraction >= max_unknown:
            flags.append(
                self._flag(
                    None,
                    DRIFT_UNKNOWN_PAGE,
                    {
                        "unknown_fraction": round(unknown_fraction, 4),
                        "threshold": max_unknown,
                        "unmatched_rules": zero_match,
                    },
                )
            )
        return flags

    def _flag(self, rule_id, kind, evidence):
        return {
            "rule_id": rule_id,
            "alias": self._alias_for(rule_id) if rule_id else None,
            "kind": kind,
            "evidence": evidence,
        }

    # ------------------------------------------------------------------
    # serialization
    # ------------------------------------------------------------------
    def to_dict(self):
        """Serialize the recorded baseline (JSON-safe tuples become lists)."""
        return {
            "thresholds": dict(self.thresholds),
            "baseline_summary": self.baseline_summary,
        }

    @classmethod
    def from_dict(cls, scraper, data):
        """Rebuild a check with a previously serialized baseline."""
        instance = cls(scraper, thresholds=data.get("thresholds"))
        summary = data.get("baseline_summary") or {}
        instance.baseline_summary = summary
        instance.baselines = summary.get("rules", {})
        return instance

    @staticmethod
    def _jaccard_distance(seq_a, seq_b):
        counter_a = Counter(seq_a)
        counter_b = Counter(seq_b)
        intersection = sum((counter_a & counter_b).values())
        union = sum((counter_a | counter_b).values())
        if union == 0:
            return 0.0
        return 1.0 - intersection / union
