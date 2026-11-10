import re

import pytest

from autoscraper import AutoScraper, IncompatibleConstraintsError

# Two structurally similar pages. The prices appear in BOTH the sidebar widget
# and the main product region; page B reverses the sidebar order while keeping
# the main region order, so a rule cannot pass by accident of sibling index.
HTML_PAGE_A = """
<div class="page">
  <aside class="sidebar">
    <div class="widget">
      <p class="price">$10</p>
      <p class="price">$20</p>
    </div>
  </aside>
  <main class="content">
    <div class="products">
      <div class="product"><h3 class="name">Apple</h3><p class="price">$10</p></div>
      <div class="product"><h3 class="name">Pear</h3><p class="price">$20</p></div>
    </div>
  </main>
</div>
"""

HTML_PAGE_B = """
<div class="page">
  <aside class="sidebar">
    <div class="widget">
      <p class="price">$20</p>
      <p class="price">$10</p>
    </div>
  </aside>
  <main class="content">
    <div class="products">
      <div class="product"><h3 class="name">Mango</h3><p class="price">$10</p></div>
      <div class="product"><h3 class="name">Fig</h3><p class="price">$20</p></div>
    </div>
  </main>
</div>
"""

SIDEBAR_SELECTOR = [{"selector": "aside.sidebar"}]
MAIN_SELECTOR = [{"selector": "main.content"}]


def _stack_regions(scraper):
    regions = []
    for stack in scraper.stack_list:
        classes = [
            " ".join(item[1].get("class", []))
            if isinstance(item[1].get("class"), list)
            else item[1].get("class", "")
            for item in stack["content"]
        ]
        names = [item[0] for item in stack["content"]]
        regions.append(list(zip(names, classes)))
    return regions


def test_without_negatives_learns_sidebar_rule_too():
    scraper = AutoScraper()
    scraper.build(html=HTML_PAGE_A, wanted_list=["$10"])
    flattened = [name for path in _stack_regions(scraper) for name, _ in path]
    assert "aside" in flattened
    assert "main" in flattened
    assert len(scraper.stack_list) == 2


def test_selector_negative_keeps_only_target_region():
    scraper = AutoScraper()
    result = scraper.build(
        html=HTML_PAGE_A, wanted_list=["$10"], negative_list=SIDEBAR_SELECTOR
    )
    assert result == ["$10", "$20"]
    assert len(scraper.stack_list) == 1
    flattened = [name for path in _stack_regions(scraper) for name, _ in path]
    assert "main" in flattened
    assert "aside" not in flattened


def test_learned_rule_follows_region_not_sidebar_index():
    # Rule trained against page A while excluding the sidebar.
    main_scraper = AutoScraper()
    main_scraper.build(
        html=HTML_PAGE_A, wanted_list=["$10"], negative_list=SIDEBAR_SELECTOR
    )

    # On page B the sidebar order is reversed but the main region order is not.
    assert (
        main_scraper.get_result_similar(
            html=HTML_PAGE_B, contain_sibling_leaves=True
        )
        == ["$10", "$20"]
    )
    assert main_scraper.get_result_exact(html=HTML_PAGE_B) == ["$10"]

    # Conversely, excluding the main region must learn the sidebar rule, whose
    # output on page B follows the reversed sidebar order.
    sidebar_scraper = AutoScraper()
    sidebar_scraper.build(
        html=HTML_PAGE_A, wanted_list=["$10"], negative_list=MAIN_SELECTOR
    )
    assert (
        sidebar_scraper.get_result_similar(
            html=HTML_PAGE_B, contain_sibling_leaves=True
        )
        == ["$20", "$10"]
    )
    assert sidebar_scraper.get_result_exact(html=HTML_PAGE_B) == ["$20"]


def test_negative_node_object_is_position_based():
    scraper = AutoScraper()
    soup = AutoScraper._get_soup(html=HTML_PAGE_A)
    sidebar_node = soup.findAll("aside", {"class": "sidebar"})[0]
    scraper.build(html=HTML_PAGE_A, wanted_list=["$10"], negative_list=[sidebar_node])
    flattened = [name for path in _stack_regions(scraper) for name, _ in path]
    assert "aside" not in flattened
    assert (
        scraper.get_result_similar(html=HTML_PAGE_B, contain_sibling_leaves=True)
        == ["$10", "$20"]
    )


def test_negative_node_selector_dict_with_name_and_attrs():
    scraper = AutoScraper()
    scraper.build(
        html=HTML_PAGE_A,
        wanted_list=["$10"],
        negative_list=[
            {
                "name": "p",
                "attrs": {"class": "price"},
                "selector": "aside.sidebar",
            }
        ],
    )
    assert len(scraper.stack_list) == 1
    flattened = [name for path in _stack_regions(scraper) for name, _ in path]
    assert "aside" not in flattened
    assert "main" in flattened


def test_regex_negative_resolves_nodes_by_position():
    scraper = AutoScraper()
    scraper.build(
        html=HTML_PAGE_A,
        wanted_list=["$10"],
        negative_list=[
            {"text": re.compile(r"\$\d+"), "selector": "aside.sidebar"}
        ],
    )
    assert (
        scraper.get_result_similar(html=HTML_PAGE_B, contain_sibling_leaves=True)
        == ["$10", "$20"]
    )


def test_negative_dict_scopes_constraints_per_alias():
    scraper = AutoScraper()
    scraper.build(
        html=HTML_PAGE_A,
        wanted_dict={"main_price": ["$10"]},
        negative_dict={"main_price": SIDEBAR_SELECTOR},
    )
    grouped = scraper.get_result_similar(
        html=HTML_PAGE_B,
        group_by_alias=True,
        contain_sibling_leaves=True,
    )
    assert grouped == {"main_price": ["$10", "$20"]}


def test_conflicting_string_negative_raises_instead_of_empty_rules():
    scraper = AutoScraper()
    # "$10" exists in both regions: marking every matching node as negative
    # leaves no rule that can cover the positive target.
    with pytest.raises(IncompatibleConstraintsError) as exc_info:
        scraper.build(html=HTML_PAGE_A, wanted_list=["$10"], negative_list=["$10"])

    assert issubclass(IncompatibleConstraintsError, ValueError)
    assert "$10" in str(exc_info.value)
    assert exc_info.value.conflicts[0][0] == ""
    # No empty rule set is produced to fake a successful build.
    assert scraper.stack_list == []


def test_conflict_does_not_discard_previously_learned_rules():
    scraper = AutoScraper()
    scraper.build(html="<ul><li>Banana</li><li>Apple</li></ul>", wanted_list=["Banana"])
    existing = list(scraper.stack_list)

    with pytest.raises(IncompatibleConstraintsError):
        scraper.build(
            html=HTML_PAGE_A,
            wanted_list=["$10"],
            negative_list=["$10"],
            update=True,
        )
    assert scraper.stack_list == existing


def test_single_region_target_and_negative_conflict():
    html = "<div><aside class='sidebar'><p class='price'>$10</p></aside></div>"
    scraper = AutoScraper()
    with pytest.raises(IncompatibleConstraintsError):
        scraper.build(
            html=html,
            wanted_list=["$10"],
            negative_list=SIDEBAR_SELECTOR,
        )
    assert scraper.stack_list == []


def test_unsupported_negative_sample_type():
    scraper = AutoScraper()
    with pytest.raises(TypeError):
        scraper.build(
            html=HTML_PAGE_A, wanted_list=["$10"], negative_list=[12345]
        )
