"""
Whole-record validation rules for Item Loaders.

Unlike input and output processors, which handle one field at a time, the
rules in this module can check relationships between several fields of a
candidate record. They run during :meth:`ItemLoader.load_item`, after the
output processors have converted every field and before the item is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence


class ValidationError(ValueError):
    """Raised by :meth:`ItemLoader.load_item` when one or more validation
    rules, or the field output processors, fail.

    As a subclass of :class:`ValueError`, it stays compatible with code that
    catches the value errors historically raised while loading.

    The :attr:`problems` attribute holds every collected problem, including
    the original field-level errors, so that all independent issues are
    reported together instead of only the first one.
    """

    def __init__(self, problems: Sequence[ValidationProblem]):
        self.problems: list[ValidationProblem] = list(problems)
        messages = [str(problem) for problem in self.problems]
        super().__init__("; ".join(messages))


@dataclass(frozen=True)
class ValidationProblem:
    """A single problem found while loading a record.

    :ivar message: explains what is wrong.
    :ivar fields: the fields involved, so the problem can be located on the
        record. Empty for a problem that is not tied to specific fields.
    :ivar rule: name of the validation rule that reported the problem, or
        ``None`` for problems that do not come from a validation rule (such
        as a field conversion failure).
    :ivar missing: names of the fields the rule needed but that the record
        does not provide. A rule never runs while one of its dependencies is
        missing.
    """

    message: str
    fields: tuple[str, ...] = ()
    rule: str | None = None
    missing: tuple[str, ...] = ()

    def __str__(self) -> str:
        location = ", ".join(self.fields) if self.fields else "<record>"
        text = f"{location}: {self.message}"
        if self.rule is not None:
            text = f"[{self.rule}] {text}"
        if self.missing:
            text += f" (missing: {', '.join(self.missing)})"
        return text


@runtime_checkable
class ValidationRule(Protocol):
    """Interface expected of every validation rule."""

    #: Name used to identify the rule in error reports.
    name: str

    #: Fields whose values the rule needs in order to run.
    fields: tuple[str, ...]

    def validate(self, values: dict[str, Any]) -> str | None:
        """Check the converted field values.

        :param values: mapping of field name to converted value, containing
            at least every name in :attr:`fields`.
        :return: ``None`` when the record satisfies the rule, otherwise a
            human-readable message describing the violation.
        """


class RuleViolation(Exception):
    """Raise from a rule's check function to report a condition failure with
    the given message, instead of returning it."""


class Rule:
    """A validation rule referencing one or more fields of a record.

    :param fields: fields passed to *check*. The rule only runs when every
        one of them is available on the converted record; otherwise a
        missing-dependency problem is reported and *check* is not called.
    :param check: callable receiving the converted values of *fields* as
        keyword arguments. It signals the result by returning:

        * ``None`` when the record satisfies the rule;
        * an error message string when the condition does not hold;
        * ``False`` to report a failure using *message* (or a generic
          fallback) instead of writing the message inline.

        It may also raise :class:`RuleViolation`, whose text is used as the
        error message.
    :param name: name shown in error reports. Defaults to the name of
        *check*.
    :param message: failure message used when *check* returns ``False`` and
        no inline message is available. Defaults to a generic message
        listing the involved fields.

    Rules are typically declared through :func:`rule` and collected in the
    :attr:`ItemLoader.validation_rules` attribute of a loader class.
    """

    fields: tuple[str, ...]
    check: Callable[..., Any]
    name: str
    message: str | None

    def __init__(
        self,
        fields: Iterable[str] | str,
        check: Callable[..., Any],
        *,
        name: str | None = None,
        message: str | None = None,
    ):
        field_names = (fields,) if isinstance(fields, str) else tuple(fields)
        if not field_names:
            raise ValueError("A validation rule must reference at least one field")
        default_name = getattr(check, "__name__", check.__class__.__name__)
        self.fields = field_names
        self.check = check
        self.name = name if name is not None else str(default_name)
        self.message = message

    def validate(self, values: dict[str, Any]) -> str | None:
        try:
            result = self.check(**{name: values[name] for name in self.fields})
        except RuleViolation as violation:
            return str(violation)
        if result is None or result is True:
            return None
        if result is False:
            return self.message or (
                "validation condition not satisfied for fields: "
                f"{', '.join(self.fields)}"
            )
        return str(result)

    def __repr__(self) -> str:
        return f"Rule(name={self.name!r}, fields={self.fields!r})"


def rule(
    fields: Iterable[str] | str,
    *,
    name: str | None = None,
    message: str | None = None,
) -> Callable[[Callable[..., Any]], Rule]:
    """Decorator turning a function into a :class:`Rule`.

    The decorated function receives the converted values of *fields* as
    keyword arguments. It returns ``None`` (or ``True``) when the record
    satisfies the rule, an error message string when it does not, or
    ``False`` to fall back to the *message* argument. It may also raise
    :class:`RuleViolation`.

    Example::

        from itemloaders import rule

        class ProductLoader(ItemLoader):
            @rule(("price", "promotion_price"))
            def promotion_price_not_above_price(price, promotion_price):
                if promotion_price > price:
                    return "promotion price must not be higher than the price"
    """

    def decorator(check: Callable[..., Any]) -> Rule:
        return Rule(fields, check, name=name, message=message)

    return decorator
