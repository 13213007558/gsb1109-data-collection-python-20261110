"""Demo: extracting one field while old and new templates coexist.

Run with:  python examples/relaunch_demo.py

The page (relaunch_page.html) contains a hidden legacy block and the new
visible block.  Three candidate selectors are configured for the price.
The rule group validates each candidate and explains why it was selected
or rejected.  No page scripts are executed at any point.
"""

from pathlib import Path

from parsel.rulegroup import ExtractionRule, RuleGroup

PAGE = Path(__file__).with_name("relaunch_page.html").read_text(encoding="utf-8")

PRICE_PATTERN = r"[1-9]\d*,\d{2}\s*€"  # non-zero price; rejects "0,00 €" placeholders

price_rules = [
    ExtractionRule(
        "#legacy-product .price::text",
        name="legacy hidden price",
        pattern=PRICE_PATTERN,
    ),
    ExtractionRule(
        "#product .price::text",
        name="new template price",
        pattern=PRICE_PATTERN,
    ),
    ExtractionRule(
        "meta[itemprop=price]::attr(content)",
        name="microdata fallback",
        pattern=PRICE_PATTERN,
    ),
]


def show(group: RuleGroup) -> None:
    result = group.extract(PAGE)
    print(result.explain())
    if result.ok:
        print(f"-> adopted value: {result.value}\n")
    else:
        print("-> no value adopted; callers can inspect result.candidates\n")


def main() -> None:
    print("=== mode='first' (first valid candidate wins) ===")
    show(RuleGroup(price_rules, mode="first"))

    print("=== mode='consensus' (valid candidates must agree) ===")
    show(RuleGroup(price_rules[:2], mode="consensus"))


if __name__ == "__main__":
    main()
