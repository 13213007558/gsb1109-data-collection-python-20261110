import hashlib
import json
from collections import defaultdict
from html import unescape
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from autoscraper.utils import (
    FuzzyText,
    ResultItem,
    get_non_rec_text,
    normalize,
    text_match,
    unique_hashable,
    unique_stack_list,
)


class IncompatibleConstraintsError(ValueError):
    """
    Raised when the positive samples (wanted_list/wanted_dict) and the
    negative samples (negative_list/negative_dict) cannot be satisfied at the
    same time: every rule covering a wanted node also matches one of the
    designated non-target nodes.
    """

    def __init__(self, conflicts):
        self.conflicts = conflicts
        messages = [
            "alias {!r}, wanted {!r}: every rule matching this target also "
            "matches negative node(s) {}".format(alias, wanted, ", ".join(nodes))
            for alias, wanted, nodes in conflicts
        ]
        super().__init__("; ".join(messages))


class AutoScraper(object):
    """
    AutoScraper : A Smart, Automatic, Fast and Lightweight Web Scraper for Python.
    AutoScraper automatically learns a set of rules required to extract the needed content
        from a web page. So the programmer doesn't need to explicitly construct the rules.

    Attributes
    ----------
    stack_list: list
        List of rules learned by AutoScraper

    Methods
    -------
    build() - Learns a set of rules represented as stack_list based on the wanted_list,
        which can be reused for scraping similar elements from other web pages in the future.
    get_result_similar() - Gets similar results based on the previously learned rules.
    get_result_exact() - Gets exact results based on the previously learned rules.
    get_results() - Gets exact and similar results based on the previously learned rules.
    save() - Serializes the stack_list as JSON and saves it to disk.
    load() - De-serializes the JSON representation of the stack_list and loads it back.
    remove_rules() - Removes one or more learned rule[s] from the stack_list.
    keep_rules() - Keeps only the specified learned rules in the stack_list and removes the others.
    """

    request_headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_5) AppleWebKit/537.36 \
            (KHTML, like Gecko) Chrome/84.0.4147.135 Safari/537.36"
    }

    def __init__(self, stack_list=None):
        self.stack_list = stack_list or []

    def save(self, file_path):
        """
        Serializes the stack_list as JSON and saves it to the disk.

        Parameters
        ----------
        file_path: str
            Path of the JSON output

        Returns
        -------
        None
        """

        data = dict(stack_list=self.stack_list)
        with open(file_path, "w") as f:
            json.dump(data, f)

    def load(self, file_path):
        """
        De-serializes the JSON representation of the stack_list and loads it back.

        Parameters
        ----------
        file_path: str
            Path of the JSON file to load stack_list from.

        Returns
        -------
        None
        """

        with open(file_path, "r") as f:
            data = json.load(f)

        # for backward compatibility
        if isinstance(data, list):
            self.stack_list = data
            return

        self.stack_list = data["stack_list"]

    @classmethod
    def _fetch_html(cls, url, request_args=None):
        request_args = request_args or {}
        headers = dict(cls.request_headers)
        if url:
            headers["Host"] = urlparse(url).netloc

        user_headers = request_args.pop("headers", {})
        headers.update(user_headers)
        res = requests.get(url, headers=headers, **request_args)
        if res.encoding == "ISO-8859-1" and not "ISO-8859-1" in res.headers.get(
            "Content-Type", ""
        ):
            res.encoding = res.apparent_encoding
        html = res.text
        return html

    @classmethod
    def _get_soup(cls, url=None, html=None, request_args=None):
        if html:
            html = normalize(unescape(html))
            return BeautifulSoup(html, "lxml")

        html = cls._fetch_html(url, request_args)
        html = normalize(unescape(html))

        return BeautifulSoup(html, "lxml")

    @staticmethod
    def _get_valid_attrs(item):
        key_attrs = {"class", "style"}
        attrs = {
            k: v if v != [] else "" for k, v in item.attrs.items() if k in key_attrs
        }

        for attr in key_attrs:
            if attr not in attrs:
                attrs[attr] = ""
        return attrs

    @staticmethod
    def _child_has_text(child, text, url, text_fuzz_ratio):
        child_text = child.getText().strip()

        if text_match(text, child_text, text_fuzz_ratio):
            parent_text = child.parent.getText().strip()
            if child_text == parent_text and child.parent.parent:
                return False

            child.wanted_attr = None
            return True

        if text_match(text, get_non_rec_text(child), text_fuzz_ratio):
            child.is_non_rec_text = True
            child.wanted_attr = None
            return True

        for key, value in child.attrs.items():
            if not isinstance(value, str):
                continue

            value = value.strip()
            if text_match(text, value, text_fuzz_ratio):
                child.wanted_attr = key
                return True

            if key in {"href", "src"}:
                full_url = urljoin(url, value)
                if text_match(text, full_url, text_fuzz_ratio):
                    child.wanted_attr = key
                    child.is_full_url = True
                    return True

        return False

    def _get_children(self, soup, text, url, text_fuzz_ratio):
        children = reversed(soup.findChildren())
        children = [
            x for x in children if self._child_has_text(x, text, url, text_fuzz_ratio)
        ]
        return children

    @staticmethod
    def _node_text_candidates(node):
        values = []
        text = node.getText().strip()
        if text:
            values.append(text)
        non_rec_text = get_non_rec_text(node)
        if non_rec_text and non_rec_text != text:
            values.append(non_rec_text)
        for value in node.attrs.values():
            if isinstance(value, str):
                if value.strip():
                    values.append(value.strip())
            elif isinstance(value, (list, tuple)):
                values.extend(v.strip() for v in value if isinstance(v, str) and v.strip())
        return unique_hashable(values)

    def _node_matches_sample(self, node, sample, url, text_fuzz_ratio):
        for value in self._node_text_candidates(node):
            if text_match(sample, value, text_fuzz_ratio):
                return True
            if url and text_match(sample, urljoin(url, value), text_fuzz_ratio):
                return True
        return False

    @staticmethod
    def _find_nodes_by_selector(soup, selector):
        roots = [soup]
        for part in selector.split():
            tag, _, class_part = part.partition(".")
            wanted_classes = {cls for cls in class_part.split(".") if cls}
            next_roots = []
            for root in roots:
                for node in root.findAll(tag, recursive=True):
                    node_classes = node.attrs.get("class", "")
                    if isinstance(node_classes, str):
                        node_classes = node_classes.split()
                    if wanted_classes.issubset(set(node_classes)):
                        next_roots.append(node)
            roots = next_roots
        return roots

    @staticmethod
    def _node_selector_parts(node):
        parts = []
        current = node
        while current is not None and getattr(current, "name", None) not in (
            None,
            "[document]",
        ):
            classes = current.attrs.get("class", "")
            if isinstance(classes, str):
                classes = classes.split()
            parts.append(current.name + "".join("." + cls for cls in classes))
            current = current.findParent()
        return " ".join(reversed(parts))

    @staticmethod
    def _unique_nodes(nodes):
        seen = set()
        unique = []
        for node in nodes:
            node_id = id(node)
            if node_id in seen:
                continue
            seen.add(node_id)
            unique.append(node)
        return unique

    def _resolve_one_negative(self, soup, sample, url, text_fuzz_ratio):
        if hasattr(sample, "findAll") and hasattr(sample, "getText"):
            if any(node is sample for node in soup.findChildren()):
                return [sample]
            nodes = self._find_nodes_by_selector(soup, self._node_selector_parts(sample))
            return nodes or [sample]

        if isinstance(sample, dict):
            selector = sample.get("selector")
            roots = self._find_nodes_by_selector(soup, selector) if selector else [soup]
            name = sample.get("name")
            attrs = sample.get("attrs") or {}
            text = sample.get("text")

            if selector and name is None and not attrs and text is None:
                return roots

            nodes = []
            for root in roots:
                if name is None and not attrs:
                    candidates = root.findChildren(recursive=True)
                else:
                    candidates = root.findAll(name, attrs, recursive=True)
                for node in candidates:
                    if text is None or self._node_matches_sample(
                        node, text, url, text_fuzz_ratio
                    ):
                        nodes.append(node)
            return nodes

        if isinstance(sample, str) or hasattr(sample, "fullmatch"):
            return [
                node
                for node in soup.findChildren()
                if self._node_matches_sample(node, sample, url, text_fuzz_ratio)
            ]

        raise TypeError("Unsupported negative sample: {!r}".format(sample))

    def _resolve_negative_nodes(
        self, soup, negative_list, negative_dict, url, text_fuzz_ratio
    ):
        def resolve(samples):
            nodes = []
            for sample in samples:
                nodes.extend(
                    self._resolve_one_negative(soup, sample, url, text_fuzz_ratio)
                )
            return self._unique_nodes(nodes)

        global_nodes = resolve(negative_list) if negative_list else []
        alias_nodes = {}
        if negative_dict:
            for alias, samples in negative_dict.items():
                if samples:
                    alias_nodes[alias] = resolve(samples)
        return global_nodes, alias_nodes

    def _get_matched_nodes(self, stack, soup):
        parents = [soup]
        stack_content = stack["content"]
        for index, item in enumerate(stack_content):
            if item[0] == "[document]":
                continue
            children = []
            for parent in parents:
                found = parent.findAll(item[0], item[1], recursive=False)
                if not found:
                    continue
                if index == len(stack_content) - 1:
                    idx = min(len(found) - 1, stack_content[index - 1][2])
                    found = [found[idx]]
                children += found
            parents = children
        return parents

    @staticmethod
    def _node_touches(node, negative_nodes):
        negative_ids = {id(negative) for negative in negative_nodes}

        current = node
        while current is not None:
            if id(current) in negative_ids:
                return True
            current = current.findParent()

        for descendant in node.findChildren(recursive=True):
            if id(descendant) in negative_ids:
                return True
        return False

    def _stack_hits_nodes(self, stack, soup, negative_nodes):
        if not negative_nodes:
            return False
        for node in self._get_matched_nodes(stack, soup):
            if self._node_touches(node, negative_nodes):
                return True
        return False

    @staticmethod
    def _describe_node(node):
        classes = node.attrs.get("class", "")
        if isinstance(classes, list):
            classes = " ".join(classes)
        label = node.name
        if classes:
            label += "." + ".".join(classes.split())
        text = node.getText().strip()
        if text:
            label += "={!r}".format(text if len(text) <= 40 else text[:37] + "...")
        return "<{}>".format(label)

    def build(
        self,
        url=None,
        wanted_list=None,
        wanted_dict=None,
        html=None,
        request_args=None,
        update=False,
        text_fuzz_ratio=1.0,
        negative_list=None,
        negative_dict=None,
    ):
        """
        Automatically constructs a set of rules to scrape the specified target[s] from a web page.
            The rules are represented as stack_list.

        Parameters:
        ----------
        url: str, optional
            URL of the target web page. You should either pass url or html or both.

        wanted_list: list of strings or compiled regular expressions, optional
            A list of needed contents to be scraped.
                AutoScraper learns a set of rules to scrape these targets. If specified,
                wanted_dict will be ignored.

        wanted_dict: dict, optional
            A dict of needed contents to be scraped. Keys are aliases and values
                are list of target texts or compiled regular expressions.
                AutoScraper learns a set of rules to scrape these targets and sets its aliases.

        html: str, optional
            An HTML string can also be passed instead of URL.
                You should either pass url or html or both.

        request_args: dict, optional
            A dictionary used to specify a set of additional request parameters used by requests
                module. You can specify proxy URLs, custom headers etc.

        update: bool, optional, defaults to False
            If True, new learned rules will be added to the previous ones.
            If False, all previously learned rules will be removed.

        text_fuzz_ratio: float in range [0, 1], optional, defaults to 1.0
            The fuzziness ratio threshold for matching the wanted contents.

        negative_list: list, optional
            A list of explicit non-target samples used to keep learned rules away
                from nodes that must not be scraped (e.g. sidebar prices that share
                their text with the main content). Each entry may be:
                - a string or compiled regular expression, resolved to every node
                  whose text or attribute value matches;
                - a parsed element (BeautifulSoup tag / node), used as-is;
                - a node selector dict such as {"name": "p", "attrs": {"class": "price"},
                  "selector": "aside.widget"} where selector is an optional CSS-like
                  ancestor path restricting which matching nodes are negatives.
            Rules are filtered by node position, so identical text in two regions is
                distinguished instead of being globally excluded by string.

        negative_dict: dict, optional
            Like negative_list but keyed by alias. A negative rule only filters rules
                learned for the matching alias; the empty-string key applies to
                wanted_list targets. Entries are applied in addition to negative_list.

        Returns:
        --------
        List of similar results

        Raises:
        --------
        IncompatibleConstraintsError
            When every rule learned for a wanted target also matches one of the
                supplied negative nodes, i.e. the positive and negative constraints
                cannot be satisfied together. No empty rule set is produced.
        """

        if not wanted_list and not (wanted_dict and any(wanted_dict.values())):
            raise ValueError("No targets were supplied")

        soup = self._get_soup(url=url, html=html, request_args=request_args)

        global_negative_nodes, alias_negative_nodes = self._resolve_negative_nodes(
            soup, negative_list, negative_dict, url, text_fuzz_ratio
        )

        result_list = []
        conflicts = []
        original_stack_list = list(self.stack_list)

        if update is False:
            self.stack_list = []

        if wanted_list:
            wanted_dict = {"": wanted_list}

        wanted_list = []

        for alias, wanted_items in wanted_dict.items():
            wanted_items = [normalize(w) for w in wanted_items]
            wanted_list += wanted_items

            negative_nodes = (
                global_negative_nodes + alias_negative_nodes.get(alias, [])
            )

            for wanted in wanted_items:
                children = self._get_children(soup, wanted, url, text_fuzz_ratio)

                accepted_any = False
                for child in children:
                    result, stack = self._get_result_for_child(child, soup, url)
                    if self._stack_hits_nodes(stack, soup, negative_nodes):
                        continue
                    stack["alias"] = alias
                    result_list += result
                    self.stack_list.append(stack)
                    accepted_any = True

                if children and not accepted_any:
                    conflicts.append(
                        (alias, wanted, [self._describe_node(n) for n in negative_nodes])
                    )

        if conflicts:
            self.stack_list = original_stack_list
            raise IncompatibleConstraintsError(conflicts)

        result_list = [item.text for item in result_list]
        result_list = unique_hashable(result_list)

        self.stack_list = unique_stack_list(self.stack_list)
        return result_list

    @classmethod
    def _build_stack(cls, child, url):
        content = [(child.name, cls._get_valid_attrs(child))]

        parent = child
        while True:
            grand_parent = parent.findParent()
            if not grand_parent:
                break

            children = grand_parent.findAll(
                parent.name, cls._get_valid_attrs(parent), recursive=False
            )
            for i, c in enumerate(children):
                if c == parent:
                    content.insert(
                        0, (grand_parent.name, cls._get_valid_attrs(grand_parent), i)
                    )
                    break

            if not grand_parent.parent:
                break

            parent = grand_parent

        wanted_attr = getattr(child, "wanted_attr", None)
        is_full_url = getattr(child, "is_full_url", False)
        is_non_rec_text = getattr(child, "is_non_rec_text", False)
        stack = dict(
            content=content,
            wanted_attr=wanted_attr,
            is_full_url=is_full_url,
            is_non_rec_text=is_non_rec_text,
        )
        stack["url"] = url if is_full_url else ""
        stack["hash"] = hashlib.sha256(str(stack).encode("utf-8")).hexdigest()
        stack["stack_id"] = "rule_" + stack["hash"][:8]
        return stack

    def _get_result_for_child(self, child, soup, url):
        stack = self._build_stack(child, url)
        result = self._get_result_with_stack(stack, soup, url, 1.0)
        return result, stack

    @staticmethod
    def _fetch_result_from_child(child, wanted_attr, is_full_url, url, is_non_rec_text):
        if wanted_attr is None:
            if is_non_rec_text:
                return get_non_rec_text(child)
            return child.getText().strip()

        if wanted_attr not in child.attrs:
            return None

        if is_full_url:
            return urljoin(url, child.attrs[wanted_attr])

        return child.attrs[wanted_attr]

    @staticmethod
    def _get_fuzzy_attrs(attrs, attr_fuzz_ratio):
        attrs = dict(attrs)
        for key, val in attrs.items():
            if isinstance(val, str) and val:
                val = FuzzyText(val, attr_fuzz_ratio)
            elif isinstance(val, (list, tuple)):
                val = [FuzzyText(x, attr_fuzz_ratio) if x else x for x in val]
            attrs[key] = val
        return attrs

    def _get_result_with_stack(self, stack, soup, url, attr_fuzz_ratio, **kwargs):
        parents = [soup]
        stack_content = stack["content"]
        contain_sibling_leaves = kwargs.get("contain_sibling_leaves", False)
        for index, item in enumerate(stack_content):
            children = []
            if item[0] == "[document]":
                continue
            for parent in parents:

                attrs = item[1]
                if attr_fuzz_ratio < 1.0:
                    attrs = self._get_fuzzy_attrs(attrs, attr_fuzz_ratio)

                found = parent.findAll(item[0], attrs, recursive=False)
                if not found:
                    continue

                if not contain_sibling_leaves and index == len(stack_content) - 1:
                    idx = min(len(found) - 1, stack_content[index - 1][2])
                    found = [found[idx]]

                children += found

            parents = children

        wanted_attr = stack["wanted_attr"]
        is_full_url = stack["is_full_url"]
        is_non_rec_text = stack.get("is_non_rec_text", False)
        result = [
            ResultItem(
                self._fetch_result_from_child(
                    i, wanted_attr, is_full_url, url, is_non_rec_text
                ),
                getattr(i, "child_index", 0),
            )
            for i in parents
        ]
        if not kwargs.get("keep_blank", False):
            result = [x for x in result if x.text]
        return result

    def _get_result_with_stack_index_based(
        self, stack, soup, url, attr_fuzz_ratio, **kwargs
    ):
        p = soup.findChildren(recursive=False)[0]
        stack_content = stack["content"]
        for index, item in enumerate(stack_content[:-1]):
            if item[0] == "[document]":
                continue
            content = stack_content[index + 1]
            attrs = content[1]
            if attr_fuzz_ratio < 1.0:
                attrs = self._get_fuzzy_attrs(attrs, attr_fuzz_ratio)
            p = p.findAll(content[0], attrs, recursive=False)
            if not p:
                return []
            idx = min(len(p) - 1, item[2])
            p = p[idx]

        result = [
            ResultItem(
                self._fetch_result_from_child(
                    p,
                    stack["wanted_attr"],
                    stack["is_full_url"],
                    url,
                    stack["is_non_rec_text"],
                ),
                getattr(p, "child_index", 0),
            )
        ]
        if not kwargs.get("keep_blank", False):
            result = [x for x in result if x.text]
        return result

    def _get_result_by_func(
        self,
        func,
        url,
        html,
        soup,
        request_args,
        grouped,
        group_by_alias,
        unique,
        attr_fuzz_ratio,
        **kwargs
    ):
        if not soup:
            soup = self._get_soup(url=url, html=html, request_args=request_args)

        keep_order = kwargs.get("keep_order", False)

        if group_by_alias or (keep_order and not grouped):
            for index, child in enumerate(soup.findChildren()):
                setattr(child, "child_index", index)

        result_list = []
        grouped_result = defaultdict(list)
        for stack in self.stack_list:
            if not url:
                url = stack.get("url", "")

            result = func(stack, soup, url, attr_fuzz_ratio, **kwargs)

            if not grouped and not group_by_alias:
                result_list += result
                continue

            group_id = stack.get("alias", "") if group_by_alias else stack["stack_id"]
            grouped_result[group_id] += result

        return self._clean_result(
            result_list, grouped_result, grouped, group_by_alias, unique, keep_order
        )

    @staticmethod
    def _clean_result(
        result_list, grouped_result, grouped, grouped_by_alias, unique, keep_order
    ):
        if not grouped and not grouped_by_alias:
            if unique is None:
                unique = True
            if keep_order:
                result_list = sorted(result_list, key=lambda x: x.index)
            result = [x.text for x in result_list]
            if unique:
                result = unique_hashable(result)
            return result

        for k, val in grouped_result.items():
            if grouped_by_alias:
                val = sorted(val, key=lambda x: x.index)
            val = [x.text for x in val]
            if unique:
                val = unique_hashable(val)
            grouped_result[k] = val

        return dict(grouped_result)

    def get_result_similar(
        self,
        url=None,
        html=None,
        soup=None,
        request_args=None,
        grouped=False,
        group_by_alias=False,
        unique=None,
        attr_fuzz_ratio=1.0,
        keep_blank=False,
        keep_order=False,
        contain_sibling_leaves=False,
    ):
        """
        Gets similar results based on the previously learned rules.

        Parameters:
        ----------
        url: str, optional
            URL of the target web page. You should either pass url or html or both.

        html: str, optional
            An HTML string can also be passed instead of URL.
                You should either pass url or html or both.

        request_args: dict, optional
            A dictionary used to specify a set of additional request parameters used by requests
                module. You can specify proxy URLs, custom headers etc.

        grouped: bool, optional, defaults to False
            If set to True, the result will be a dictionary with the rule_ids as keys
                and a list of scraped data per rule as values.

        group_by_alias: bool, optional, defaults to False
            If set to True, the result will be a dictionary with the rule alias as keys
                and a list of scraped data per alias as values.

        unique: bool, optional, defaults to True for non grouped results and
                False for grouped results.
            If set to True, will remove duplicates from returned result list.

        attr_fuzz_ratio: float in range [0, 1], optional, defaults to 1.0
            The fuzziness ratio threshold for matching html tag attributes.

        keep_blank: bool, optional, defaults to False
            If set to True, missing values will be returned as empty strings.

        keep_order: bool, optional, defaults to False
            If set to True, the results will be ordered as they are present on the web page.

        contain_sibling_leaves: bool, optional, defaults to False
            If set to True, the results will also contain the sibling leaves of the wanted elements.

        Returns:
        --------
        List of similar results scraped from the web page.
        Dictionary if grouped=True or group_by_alias=True.
        """

        func = self._get_result_with_stack
        return self._get_result_by_func(
            func,
            url,
            html,
            soup,
            request_args,
            grouped,
            group_by_alias,
            unique,
            attr_fuzz_ratio,
            keep_blank=keep_blank,
            keep_order=keep_order,
            contain_sibling_leaves=contain_sibling_leaves,
        )

    def get_result_exact(
        self,
        url=None,
        html=None,
        soup=None,
        request_args=None,
        grouped=False,
        group_by_alias=False,
        unique=None,
        attr_fuzz_ratio=1.0,
        keep_blank=False,
    ):
        """
        Gets exact results based on the previously learned rules.

        Parameters:
        ----------
        url: str, optional
            URL of the target web page. You should either pass url or html or both.

        html: str, optional
            An HTML string can also be passed instead of URL.
                You should either pass url or html or both.

        request_args: dict, optional
            A dictionary used to specify a set of additional request parameters used by requests
                module. You can specify proxy URLs, custom headers etc.

        grouped: bool, optional, defaults to False
            If set to True, the result will be a dictionary with the rule_ids as keys
                and a list of scraped data per rule as values.

        group_by_alias: bool, optional, defaults to False
            If set to True, the result will be a dictionary with the rule alias as keys
                and a list of scraped data per alias as values.

        unique: bool, optional, defaults to True for non grouped results and
                False for grouped results.
            If set to True, will remove duplicates from returned result list.

        attr_fuzz_ratio: float in range [0, 1], optional, defaults to 1.0
            The fuzziness ratio threshold for matching html tag attributes.

        keep_blank: bool, optional, defaults to False
            If set to True, missing values will be returned as empty strings.

        Returns:
        --------
        List of exact results scraped from the web page.
        Dictionary if grouped=True or group_by_alias=True.
        """

        func = self._get_result_with_stack_index_based
        return self._get_result_by_func(
            func,
            url,
            html,
            soup,
            request_args,
            grouped,
            group_by_alias,
            unique,
            attr_fuzz_ratio,
            keep_blank=keep_blank,
        )

    def get_result(
        self,
        url=None,
        html=None,
        request_args=None,
        grouped=False,
        group_by_alias=False,
        unique=None,
        attr_fuzz_ratio=1.0,
    ):
        """
        Gets similar and exact results based on the previously learned rules.

        Parameters:
        ----------
        url: str, optional
            URL of the target web page. You should either pass url or html or both.

        html: str, optional
            An HTML string can also be passed instead of URL.
                You should either pass url or html or both.

        request_args: dict, optional
            A dictionary used to specify a set of additional request parameters used by requests
                module. You can specify proxy URLs, custom headers etc.

        grouped: bool, optional, defaults to False
            If set to True, the result will be dictionaries with the rule_ids as keys
                and a list of scraped data per rule as values.

        group_by_alias: bool, optional, defaults to False
            If set to True, the result will be a dictionary with the rule alias as keys
                and a list of scraped data per alias as values.

        unique: bool, optional, defaults to True for non grouped results and
                False for grouped results.
            If set to True, will remove duplicates from returned result list.

        attr_fuzz_ratio: float in range [0, 1], optional, defaults to 1.0
            The fuzziness ratio threshold for matching html tag attributes.

        Returns:
        --------
        Pair of (similar, exact) results.
        See get_result_similar and get_result_exact methods.
        """

        soup = self._get_soup(url=url, html=html, request_args=request_args)
        args = dict(
            url=url,
            soup=soup,
            grouped=grouped,
            group_by_alias=group_by_alias,
            unique=unique,
            attr_fuzz_ratio=attr_fuzz_ratio,
        )
        similar = self.get_result_similar(**args)
        exact = self.get_result_exact(**args)
        return similar, exact

    def remove_rules(self, rules):
        """
        Removes a list of learned rules from stack_list.

        Parameters:
        ----------
        rules : list
            A list of rules to be removed

        Returns:
        --------
        None
        """

        self.stack_list = [x for x in self.stack_list if x["stack_id"] not in rules]

    def keep_rules(self, rules):
        """
        Removes all other rules except the specified ones.

        Parameters:
        ----------
        rules : list
            A list of rules to keep in stack_list and removing the rest.

        Returns:
        --------
        None
        """

        self.stack_list = [x for x in self.stack_list if x["stack_id"] in rules]

    def set_rule_aliases(self, rule_aliases):
        """
        Sets the specified alias for each rule

        Parameters:
        ----------
        rule_aliases : dict
            A dictionary with keys of rule_id and values of alias

        Returns:
        --------
        None
        """

        id_to_stack = {stack["stack_id"]: stack for stack in self.stack_list}
        for rule_id, alias in rule_aliases.items():
            id_to_stack[rule_id]["alias"] = alias

    def generate_python_code(self):
        # deprecated
        print("This function is deprecated. Please use save() and load() instead.")
