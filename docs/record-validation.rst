.. _record-validation:

Validating whole records
========================

.. versionadded:: VERSION

Input and output processors handle one field at a time, so they are a poor
fit for rules that relate several fields of the same record, such as "the
promotion price must not be higher than the original price" or "the end
time must be later than the start time". For those, an :class:`ItemLoader`
supports whole-record **validation rules**.

A validation rule runs during :meth:`~ItemLoader.load_item`, after the
output processors have converted every collected field and before the
converted values are written to the item. Rules are opt-in: a loader with
no rules behaves exactly as before.

Declaring rules
---------------

The :func:`~itemloaders.rule` decorator turns a method into a rule. The
method receives the converted values of the fields it references as
keyword arguments and returns ``None`` when the record satisfies the rule,
or an error message when it does not::

    from datetime import datetime

    from itemloaders import ItemLoader, rule
    from itemloaders.processors import Compose, TakeFirst


    class OfferLoader(ItemLoader):
        default_output_processor = TakeFirst()
        price_out = Compose(TakeFirst(), float)
        promotion_price_out = Compose(TakeFirst(), float)
        start_out = Compose(TakeFirst(), datetime.fromisoformat)
        end_out = Compose(TakeFirst(), datetime.fromisoformat)

        @rule(("price", "promotion_price"))
        def promotion_price_not_above_price(price, promotion_price):
            if promotion_price > price:
                return "promotion price must not be higher than the price"

        @rule(("start", "end"))
        def end_after_start(start, end):
            if end <= start:
                return "end time must be later than the start time"

Decorated rules are collected automatically in the
:attr:`~ItemLoader.validation_rules` attribute and accumulate through
inheritance. A rule referencing a single field may be declared with a plain
string, e.g. ``@rule("name")``.

Rules can also be built imperatively with :class:`~itemloaders.Rule`, which
is useful when the check is a shared function or a lambda::

    from itemloaders import Rule

    class OfferLoader(ItemLoader):
        validation_rules = (
            Rule(
                ("price", "promotion_price"),
                lambda price, promotion_price: None
                if promotion_price <= price
                else "promotion price must not be higher than the price",
            ),
        )

Instead of returning a message, a check may return ``False`` to use the
rule's ``message`` argument, return ``True`` for success, or raise
:class:`~itemloaders.RuleViolation` from a shared helper.

Collecting every problem
------------------------

Rules and field conversions are evaluated independently, so a single
:meth:`~ItemLoader.load_item` call reports every independent problem at
once rather than stopping at the first one. Problems are raised together
as a :class:`~itemloaders.ValidationError`, whose ``problems`` attribute is
a list of :class:`~itemloaders.ValidationProblem` objects. Each problem
exposes:

- ``fields``: the field combination involved, so the problem can be located
  on the record;
- ``message``: what is wrong;
- ``rule``: the name of the rule that reported it, or ``None`` for a field
  conversion failure;
- ``missing``: the fields the rule needed but the record does not provide.

A field whose output processor fails keeps its original field-level error.
Rules that depend on that field are skipped instead of producing a
misleading business error derived from the missing value, while unrelated
rules are still evaluated::

    loader = OfferLoader()
    loader.add_value("price", "100")
    loader.add_value("promotion_price", "oops")  # float() conversion fails
    loader.add_value("start", "2026-11-10T00:00:00")
    loader.add_value("end", "2026-11-01T00:00:00")
    loader.load_item()
    # itemloaders.ValidationError with two problems:
    #   promotion_price: the original output processor error
    #   [end_after_start] start, end: end time must be later than ...
    # The promotion price rule is skipped, not reported as a business error.

When a rule references a field that is simply absent from the converted
record (including a field whose output processor returns ``None``), the
rule is not run and the problem is reported as a missing dependency, with
the missing field names in ``missing``::

    loader = OfferLoader()
    loader.add_value("price", "100")
    loader.add_value("start", "2026-11-01T00:00:00")
    loader.load_item()
    # ValidationError; each rule reports its missing fields:
    #   [promotion_price_not_above_price] ... (missing: promotion_price)
    #   [end_after_start] ... (missing: end)

:class:`~itemloaders.ValidationError` is a subclass of :class:`ValueError`,
so existing code that catches the value errors historically raised while
loading keeps working. When problems are found, the item is left
unpopulated by the rejected record.

Reference
---------

.. automodule:: itemloaders.validation
    :members:
