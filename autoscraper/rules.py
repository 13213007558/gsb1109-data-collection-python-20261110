"""Versioned rule-file format and migration entry points for AutoScraper.

Rule files are long-lived artifacts: upgrading AutoScraper must not require
retraining rules and silently overwriting the old file. This module therefore
owns an explicit on-disk format, strict validation and one-step migration that
writes a new file while leaving the original untouched.

Versions
--------
0
    Implicit legacy format. A bare list or ``{"stack_list": [...]}`` without a
    version marker. Accepted on load with lenient defaults (the historical
    behaviour) but never written by this module.
1
    First versioned format. Document wrapper::

        {"format": "autoscraper-rules", "version": 1, "rules": [rule, ...]}

    Each rule is the historical stack dictionary; ``content`` is a path of
    ``[tag, attrs]`` / ``[tag, attrs, position]`` entries.
2
    Current version. Same wrapper; every rule exposes its identity, alias,
    attribute matching and positional semantics explicitly::

        {
          "rule_id": "rule_xxxxxxxx",
          "alias": "",
          "hash": "<sha256>",
          "match": {
            "wanted_attr": null,
            "is_full_url": false,
            "is_non_rec_text": false,
            "url": ""
          },
          "path": [
            {"tag": "ul", "attrs": {"class": "", "style": ""}, "position": 0},
            {"tag": "li", "attrs": {"class": "", "style": ""}, "position": null}
          ]
        }

A v1 -> v2 migration is a purely structural reshape: rule identity (``hash``/
``stack_id``), aliases, attribute matching and the positional path are carried
over verbatim. Behavior equivalence against local HTML pages can be proven with
:func:`verify_behavior`; anything that cannot be proven is reported back to the
caller instead of being silently "fixed".
"""

import hashlib
import json
import os
import tempfile


FORMAT_NAME = "autoscraper-rules"

LEGACY_VERSION = 0
FIRST_VERSIONED_VERSION = 1
CURRENT_RULE_VERSION = 2

SUPPORTED_LOAD_VERSIONS = (LEGACY_VERSION, FIRST_VERSIONED_VERSION, CURRENT_RULE_VERSION)
WRITABLE_VERSIONS = (FIRST_VERSIONED_VERSION, CURRENT_RULE_VERSION)

_RULE_ID_PREFIX = "rule_"
_REQUIRED_V1_RULE_KEYS = (
    "content",
    "wanted_attr",
    "is_full_url",
    "is_non_rec_text",
    "url",
    "hash",
    "stack_id",
    "alias",
)
_REQUIRED_V2_RULE_KEYS = ("rule_id", "alias", "hash", "match", "path")
_MATCH_KEYS = ("wanted_attr", "is_full_url", "is_non_rec_text", "url")


class RuleFormatError(ValueError):
    """Raised when a rule file has an unknown version, misses fields or
    contains self-contradictory descriptions."""


class RuleDocument(object):
    """An in-memory, validated rule file."""

    def __init__(self, version, rules):
        self.version = version
        self.rules = rules


class ChangeRecord(object):
    """A single entry in a migration change manifest."""

    def __init__(self, rule_id, action, description, details=None):
        self.rule_id = rule_id
        self.action = action
        self.description = description
        self.details = details or {}

    def to_dict(self):
        return {
            "rule_id": self.rule_id,
            "action": self.action,
            "description": self.description,
            "details": self.details,
        }


class MigrationPlan(object):
    """The full manifest of a document migration."""

    def __init__(self, source_version, target_version, changes, converted_rules):
        self.source_version = source_version
        self.target_version = target_version
        self.rule_count = len(converted_rules)
        self.changes = changes
        self.converted_rules = converted_rules

    def to_dict(self):
        return {
            "source_version": self.source_version,
            "target_version": self.target_version,
            "rule_count": self.rule_count,
            "changes": [change.to_dict() for change in self.changes],
        }


class BehaviorReport(object):
    """Result of comparing two rule documents on local reference pages."""

    def __init__(self, verified, reason=None, pages=None):
        self.verified = verified
        self.reason = reason
        self.pages = pages or []

    def to_dict(self):
        return {
            "verified": self.verified,
            "reason": self.reason,
            "pages": self.pages,
        }


def _reject(condition, message):
    if condition:
        raise RuleFormatError(message)


def _is_real_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_attrs(attrs, where):
    _reject(not isinstance(attrs, dict), "%s: attrs must be an object" % where)
    for key, value in attrs.items():
        _reject(not isinstance(key, str), "%s: attr names must be strings" % where)
        _reject(
            not isinstance(value, (str, list)),
            "%s: attr %r must be a string or list of strings" % (where, key),
        )
        if isinstance(value, list):
            for item in value:
                _reject(
                    not isinstance(item, str),
                    "%s: attr %r contains a non-string value" % (where, key),
                )


def _validate_path_entries(entries, where):
    """Shared validation for the historical ``content`` path (v0/v1)."""
    _reject(
        not isinstance(entries, list) or not entries,
        "%s: path must be a non-empty list" % where,
    )
    last_index = len(entries) - 1
    for index, entry in enumerate(entries):
        _reject(
            not isinstance(entry, (list, tuple)) or len(entry) not in (2, 3),
            "%s: path entry %d must be [tag, attrs] or [tag, attrs, position]"
            % (where, index),
        )
        tag, attrs = entry[0], entry[1]
        _reject(not isinstance(tag, str) or not tag, "%s: missing tag at path[%d]" % (where, index))
        _validate_attrs(attrs, "%s path[%d]" % (where, index))
        if index == last_index:
            _reject(
                len(entry) != 2,
                "%s: leaf path entry must not carry a position" % where,
            )
        else:
            _reject(len(entry) != 3, "%s: non-leaf path[%d] needs a position" % (where, index))
            position = entry[2]
            _reject(
                not _is_real_int(position) or position < 0,
                "%s: position at path[%d] must be a non-negative integer" % (where, index),
            )


def _validate_match(match, where):
    _reject(not isinstance(match, dict), "%s: match section must be an object" % where)
    missing = [key for key in _MATCH_KEYS if key not in match]
    _reject(missing, "%s: match section misses fields %s" % (where, sorted(missing)))

    wanted_attr = match["wanted_attr"]
    _reject(
        wanted_attr is not None and not isinstance(wanted_attr, str),
        "%s: wanted_attr must be a string or null" % where,
    )
    for flag in ("is_full_url", "is_non_rec_text"):
        _reject(
            not isinstance(match[flag], bool),
            "%s: %s must be a boolean" % (where, flag),
        )
    _reject(not isinstance(match["url"], str), "%s: url must be a string" % where)

    if match["is_full_url"]:
        _reject(not wanted_attr, "%s: is_full_url=true requires wanted_attr" % where)
        _reject(not match["url"], "%s: is_full_url=true requires url" % where)


def _validate_identity(rule_id, rule_hash, where):
    _reject(not isinstance(rule_hash, str), "%s: hash must be a string" % where)
    _reject(len(rule_hash) != 64, "%s: hash must be a 64-character sha256 digest" % where)
    try:
        int(rule_hash, 16)
    except ValueError:
        _reject(True, "%s: hash is not a hexadecimal digest" % where)
    expected_id = _RULE_ID_PREFIX + rule_hash[:8]
    _reject(
        rule_id != expected_id,
        "%s: rule id %r contradicts hash %r (expected %r)"
        % (where, rule_id, rule_hash, expected_id),
    )


def _ensure_unique_identities(identities, where):
    seen_ids = set()
    seen_hashes = set()
    for rule_id, rule_hash in identities:
        _reject(rule_id in seen_ids, "%s: duplicate rule id %r" % (where, rule_id))
        _reject(rule_hash in seen_hashes, "%s: duplicate rule hash %r" % (where, rule_hash))
        seen_ids.add(rule_id)
        seen_hashes.add(rule_hash)


def _validate_v1_rule(rule, index):
    where = "rule[%d]" % index
    _reject(not isinstance(rule, dict), "%s: rule must be an object" % where)
    missing = [key for key in _REQUIRED_V1_RULE_KEYS if key not in rule]
    _reject(missing, "%s: missing fields %s" % (where, sorted(missing)))
    _reject(not isinstance(rule["alias"], str), "%s: alias must be a string" % where)
    _reject(
        not isinstance(rule["stack_id"], str) or not rule["stack_id"],
        "%s: stack_id must be a non-empty string" % where,
    )
    _validate_path_entries(rule["content"], where)
    _validate_match(rule, where)
    _validate_identity(rule["stack_id"], rule["hash"], where)
    return rule["stack_id"], rule["hash"]


def _validate_v2_rule(rule, index):
    where = "rule[%d]" % index
    _reject(not isinstance(rule, dict), "%s: rule must be an object" % where)
    missing = [key for key in _REQUIRED_V2_RULE_KEYS if key not in rule]
    _reject(missing, "%s: missing fields %s" % (where, sorted(missing)))
    _reject(not isinstance(rule["alias"], str), "%s: alias must be a string" % where)
    _reject(
        not isinstance(rule["rule_id"], str) or not rule["rule_id"],
        "%s: rule_id must be a non-empty string" % where,
    )
    _validate_identity(rule["rule_id"], rule["hash"], where)
    _validate_match(rule["match"], where)

    path = rule["path"]
    _reject(not isinstance(path, list) or not path, "%s: path must be a non-empty list" % where)
    last_index = len(path) - 1
    for path_index, entry in enumerate(path):
        entry_where = "%s path[%d]" % (where, path_index)
        _reject(not isinstance(entry, dict), "%s: entry must be an object" % entry_where)
        _reject(
            not isinstance(entry.get("tag"), str) or not entry["tag"],
            "%s: tag must be a non-empty string" % entry_where,
        )
        _validate_attrs(entry.get("attrs"), entry_where)
        position = entry.get("position")
        if path_index == last_index:
            _reject(
                position is not None,
                "%s: leaf entry must have position null" % entry_where,
            )
        else:
            _reject(
                not _is_real_int(position) or position < 0,
                "%s: position must be a non-negative integer" % entry_where,
            )
    return rule["rule_id"], rule["hash"]


def _parse_legacy(data):
    """Lenient ingestion of the unversioned historical format (version 0)."""
    stack_list = data.get("stack_list") if isinstance(data, dict) else data
    _reject(not isinstance(stack_list, list), "legacy file: stack_list must be a list")

    normalized = []
    for index, stack in enumerate(stack_list):
        where = "legacy rule[%d]" % index
        _reject(not isinstance(stack, dict), "%s: rule must be an object" % where)
        _reject("content" not in stack, "%s: missing content" % where)
        _validate_path_entries(stack["content"], where)

        rule = dict(stack)
        rule.setdefault("alias", "")
        rule.setdefault("wanted_attr", None)
        # Historical files may persist None for these tri-state flags.
        rule["is_full_url"] = bool(rule.get("is_full_url"))
        rule["is_non_rec_text"] = bool(rule.get("is_non_rec_text"))
        rule.setdefault("url", "")
        if "hash" not in rule:
            rule["hash"] = hashlib.sha256(str(rule).encode("utf-8")).hexdigest()
        rule.setdefault("stack_id", _RULE_ID_PREFIX + str(rule["hash"])[:8])
        _validate_match(rule, where)
        _validate_identity(rule["stack_id"], rule["hash"], where)
        normalized.append(rule)

    _ensure_unique_identities(
        [(rule["stack_id"], rule["hash"]) for rule in normalized], "legacy file"
    )
    return RuleDocument(LEGACY_VERSION, normalized)


def parse_document(data):
    """Validates a decoded JSON payload and returns a :class:`RuleDocument`."""
    if isinstance(data, list):
        return _parse_legacy(data)

    _reject(not isinstance(data, dict), "rule file: top-level value must be an object")

    if "version" not in data:
        _reject(
            "stack_list" not in data,
            "rule file: neither 'version' nor legacy 'stack_list' marker found",
        )
        return _parse_legacy(data)

    version = data["version"]
    _reject(
        version not in (FIRST_VERSIONED_VERSION, CURRENT_RULE_VERSION),
        "unknown rule file version: %r (supported: %s)"
        % (version, ", ".join(str(v) for v in SUPPORTED_LOAD_VERSIONS)),
    )
    _reject(
        data.get("format") != FORMAT_NAME,
        "rule file: format marker must be %r" % FORMAT_NAME,
    )
    rules = data.get("rules")
    _reject(not isinstance(rules, list), "rule file: 'rules' must be a list")

    if version == FIRST_VERSIONED_VERSION:
        identities = [_validate_v1_rule(rule, index) for index, rule in enumerate(rules)]
    else:
        identities = [_validate_v2_rule(rule, index) for index, rule in enumerate(rules)]
    _ensure_unique_identities(identities, "rule file")
    return RuleDocument(version, rules)


def load_document(file_path):
    """Reads and validates a rule file from disk."""
    with open(file_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return parse_document(data)


def _runtime_to_v1_wire(stack, index):
    """Converts a runtime stack dict (tuple path) into a JSON-safe v1 rule.

    Runtime flag attributes are tri-state (True/False/None) because bs4's
    Tag.__getattr__ returns None for unset attributes; only truthiness matters
    at runtime, so they are normalized to strict booleans here.
    """
    where = "rule[%d]" % index
    _reject(not isinstance(stack, dict), "%s: rule must be an object" % where)
    missing = [key for key in _REQUIRED_V1_RULE_KEYS if key not in stack]
    _reject(missing, "%s: missing fields %s" % (where, sorted(missing)))

    content = [list(entry) for entry in stack["content"]]
    return {
        "content": content,
        "wanted_attr": stack["wanted_attr"],
        "is_full_url": bool(stack["is_full_url"]),
        "is_non_rec_text": bool(stack["is_non_rec_text"]),
        "url": stack["url"],
        "hash": stack["hash"],
        "stack_id": stack["stack_id"],
        "alias": stack["alias"],
    }


def _v1_to_v2_rule(rule):
    path = []
    for index, entry in enumerate(rule["content"]):
        position = entry[2] if len(entry) == 3 else None
        path.append(
            {
                "tag": entry[0],
                "attrs": dict(entry[1]),
                "position": position,
            }
        )
    return {
        "rule_id": rule["stack_id"],
        "alias": rule["alias"],
        "hash": rule["hash"],
        "match": {key: rule[key] for key in _MATCH_KEYS},
        "path": path,
    }


def _v1_wire_to_runtime(rule):
    content = []
    entries = rule["content"]
    for index, entry in enumerate(entries):
        if index == len(entries) - 1:
            content.append((entry[0], dict(entry[1])))
        else:
            content.append((entry[0], dict(entry[1]), entry[2]))
    return {
        "content": content,
        "wanted_attr": rule["wanted_attr"],
        "is_full_url": rule["is_full_url"],
        "is_non_rec_text": rule["is_non_rec_text"],
        "url": rule["url"],
        "hash": rule["hash"],
        "stack_id": rule["stack_id"],
        "alias": rule["alias"],
    }


def _v2_to_runtime(rule):
    content = []
    path = rule["path"]
    for index, entry in enumerate(path):
        if index == len(path) - 1:
            content.append((entry["tag"], dict(entry["attrs"])))
        else:
            content.append((entry["tag"], dict(entry["attrs"]), entry["position"]))
    match = rule["match"]
    return {
        "content": content,
        "wanted_attr": match["wanted_attr"],
        "is_full_url": match["is_full_url"],
        "is_non_rec_text": match["is_non_rec_text"],
        "url": match["url"],
        "hash": rule["hash"],
        "stack_id": rule["rule_id"],
        "alias": rule["alias"],
    }


def runtime_rules(document):
    """Converts any validated document into the stack_list consumed at runtime."""
    if document.version == CURRENT_RULE_VERSION:
        return [_v2_to_runtime(rule) for rule in document.rules]
    return [_v1_wire_to_runtime(rule) for rule in document.rules]


def build_document(stack_list, version=CURRENT_RULE_VERSION):
    """Validates runtime rules and builds the JSON-safe document payload."""
    _reject(
        version not in WRITABLE_VERSIONS,
        "cannot write rule file version %r; writable versions: %s"
        % (version, ", ".join(str(v) for v in WRITABLE_VERSIONS)),
    )
    _reject(not isinstance(stack_list, list), "stack_list must be a list")

    v1_rules = [_runtime_to_v1_wire(stack, index) for index, stack in enumerate(stack_list)]
    identities = [_validate_v1_rule(rule, index) for index, rule in enumerate(v1_rules)]
    _ensure_unique_identities(identities, "rule file")

    if version == FIRST_VERSIONED_VERSION:
        return {"format": FORMAT_NAME, "version": version, "rules": v1_rules}

    v2_rules = [_v1_to_v2_rule(rule) for rule in v1_rules]
    v2_identities = [_validate_v2_rule(rule, index) for index, rule in enumerate(v2_rules)]
    _ensure_unique_identities(v2_identities, "rule file")
    return {"format": FORMAT_NAME, "version": version, "rules": v2_rules}


def _atomic_write(file_path, payload):
    path = os.fspath(file_path)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, tmp_path = tempfile.mkstemp(prefix=".autoscraper-rules-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def write_document(file_path, document_payload):
    _atomic_write(file_path, document_payload)


def plan_migration(document, target_version=CURRENT_RULE_VERSION):
    """Produces a :class:`MigrationPlan` without touching the disk."""
    _reject(
        target_version not in WRITABLE_VERSIONS,
        "unknown target rule file version: %r" % target_version,
    )
    _reject(
        document.version >= target_version,
        "document is already at version %s; nothing to migrate" % document.version,
    )

    changes = [
        ChangeRecord(
            rule_id=None,
            action="wrapper",
            description="add versioned document wrapper and migrate rules",
            details={
                "format": FORMAT_NAME,
                "source_version": document.version,
                "target_version": target_version,
            },
        )
    ]

    v1_rules = document.rules
    if document.version == LEGACY_VERSION:
        normalized = []
        for index, rule in enumerate(document.rules):
            wire_rule = _runtime_to_v1_wire(rule, index)
            normalized.append(wire_rule)
            changes.append(
                ChangeRecord(
                    rule_id=wire_rule["stack_id"],
                    action="normalize",
                    description="ingest unversioned rule; identity and path preserved",
                    details={
                        "alias": wire_rule["alias"],
                        "hash": wire_rule["hash"],
                        "path_entries": len(wire_rule["content"]),
                    },
                )
            )
        v1_rules = normalized

    identities = [_validate_v1_rule(rule, index) for index, rule in enumerate(v1_rules)]
    _ensure_unique_identities(identities, "rule file")

    converted_rules = v1_rules
    if target_version == CURRENT_RULE_VERSION:
        converted_rules = []
        for rule in v1_rules:
            v2_rule = _v1_to_v2_rule(rule)
            converted_rules.append(v2_rule)
            changes.append(
                ChangeRecord(
                    rule_id=v2_rule["rule_id"],
                    action="restructure",
                    description=(
                        "identity, alias, attribute matching and positional path "
                        "carried over verbatim; path entries made explicit"
                    ),
                    details={
                        "alias": v2_rule["alias"],
                        "hash": v2_rule["hash"],
                        "path_entries": len(v2_rule["path"]),
                        "wanted_attr": v2_rule["match"]["wanted_attr"],
                        "is_full_url": v2_rule["match"]["is_full_url"],
                        "is_non_rec_text": v2_rule["match"]["is_non_rec_text"],
                        "url": v2_rule["match"]["url"],
                    },
                )
            )
        v2_identities = [
            _validate_v2_rule(rule, index) for index, rule in enumerate(converted_rules)
        ]
        _ensure_unique_identities(v2_identities, "rule file")

    return MigrationPlan(document.version, target_version, changes, converted_rules)


def _normalize_page(page):
    if isinstance(page, str):
        html, url = page, None
    elif isinstance(page, dict):
        html, url = page.get("html"), page.get("url")
    else:
        raise RuleFormatError(
            "reference pages must be HTML strings or {'html': ..., 'url': ...} dicts"
        )
    if not isinstance(html, str) or not html.strip():
        raise RuleFormatError("reference page html must be a non-empty string")
    if url is not None and not isinstance(url, str):
        raise RuleFormatError("reference page url must be a string or null")
    return html, url


def verify_behavior(source_document, target_document, pages):
    """Runs both documents through exact/similar extraction on local pages.

    Returns a :class:`BehaviorReport`. Equivalent behavior is proven per page
    and per mode; mismatches (or the absence of reference pages) are reported
    for the caller to handle instead of being papered over.
    """
    if not pages:
        return BehaviorReport(
            verified=False,
            reason="no reference pages supplied; behavior equivalence left to the caller",
        )

    from autoscraper import AutoScraper

    source_scraper = AutoScraper(runtime_rules(source_document))
    target_scraper = AutoScraper(runtime_rules(target_document))

    modes = {
        "similar": lambda scraper, html, url: scraper.get_result_similar(
            html=html, url=url
        ),
        "similar_sibling_leaves": lambda scraper, html, url: scraper.get_result_similar(
            html=html, url=url, contain_sibling_leaves=True
        ),
        "similar_grouped": lambda scraper, html, url: scraper.get_result_similar(
            html=html, url=url, grouped=True, contain_sibling_leaves=True
        ),
        "similar_by_alias": lambda scraper, html, url: scraper.get_result_similar(
            html=html, url=url, group_by_alias=True, contain_sibling_leaves=True
        ),
        "similar_keep_order": lambda scraper, html, url: scraper.get_result_similar(
            html=html, url=url, contain_sibling_leaves=True, keep_order=True
        ),
        "exact": lambda scraper, html, url: scraper.get_result_exact(html=html, url=url),
        "exact_grouped": lambda scraper, html, url: scraper.get_result_exact(
            html=html, url=url, grouped=True
        ),
    }

    page_reports = []
    all_equivalent = True
    for page_index, page in enumerate(pages):
        html, url = _normalize_page(page)
        mode_results = {}
        mismatches = []
        for mode, runner in modes.items():
            source_result = runner(source_scraper, html, url)
            target_result = runner(target_scraper, html, url)
            equivalent = source_result == target_result
            mode_results[mode] = {
                "equivalent": equivalent,
                "source": source_result,
                "migrated": target_result,
            }
            if not equivalent:
                all_equivalent = False
                mismatches.append(mode)
        page_reports.append(
            {
                "page": page_index,
                "url": url,
                "equivalent": not mismatches,
                "mismatched_modes": mismatches,
                "modes": mode_results,
            }
        )

    reason = None
    if not all_equivalent:
        reason = "behavior mismatch detected; caller must review mismatched modes"
    return BehaviorReport(verified=all_equivalent, reason=reason, pages=page_reports)


def migrate_file(
    src_path, dst_path, *, target_version=CURRENT_RULE_VERSION, overwrite=False, pages=None
):
    """Migrates a rule file to a target versioned format.

    The source file is never modified or overwritten: ``dst_path`` must differ
    from ``src_path`` and must not exist unless ``overwrite=True``. A change
    manifest is always returned. When ``pages`` are supplied, exact/similar
    behavior is compared on the local pages and the report is included.
    """
    source_document = load_document(src_path)
    plan = plan_migration(source_document, target_version)

    src_absolute = os.path.abspath(os.fspath(src_path))
    dst_absolute = os.path.abspath(os.fspath(dst_path))
    _reject(
        src_absolute == dst_absolute,
        "dst_path must differ from src_path; the original rule file is never overwritten",
    )
    if os.path.exists(dst_absolute) and not overwrite:
        raise FileExistsError("target rule file already exists: %s" % dst_absolute)

    payload = {
        "format": FORMAT_NAME,
        "version": plan.target_version,
        "rules": plan.converted_rules,
    }
    _atomic_write(dst_absolute, payload)

    report = {
        "plan": plan.to_dict(),
        "output_path": dst_absolute,
    }
    if pages is not None:
        target_document = RuleDocument(plan.target_version, plan.converted_rules)
        report["verification"] = verify_behavior(
            source_document, target_document, pages
        ).to_dict()
    return report
