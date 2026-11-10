"""
Optional processor contracts for Item Loaders.

A *processor contract* statically declares the shape of data a processor
receives and returns, plus the Loader context keys it needs. Contracts allow
:class:`~itemloaders.ItemLoader` to detect misconfigured processor chains
before any data is loaded, without calling the processors themselves.

Processors that do not declare a contract keep working as before; they are
reported as *unknown* and are simply skipped by the compatibility checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from itertools import pairwise
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


class Shape(Enum):
    """The shape of values a processor receives or returns."""

    #: A single value (e.g. a string), not a sequence.
    SINGLE = "a single value"
    #: A sequence of values (e.g. a list of strings).
    SEQUENCE = "a sequence"
    #: Either a single value or a sequence.
    ANY = "any shape"


def _shapes_compatible(produced: Shape, consumed: Shape) -> bool:
    return produced is Shape.ANY or consumed is Shape.ANY or produced is consumed


@dataclass(frozen=True)
class ProcessorContract:
    """Static declaration of a processor's input/output shapes and context needs.

    :param accepts: shape of the value the processor is called with.
    :param returns: shape of the value the processor may return.
    :param requires_context: Loader context keys the processor needs.
    """

    accepts: Shape = Shape.ANY
    returns: Shape = Shape.ANY
    requires_context: frozenset[str] = frozenset()


#: Sentinel contract of the value streams managed by the loader itself, which
#: always hand a list of values to input and output processors.
_VALUES_CONTRACT = ProcessorContract(accepts=Shape.ANY, returns=Shape.SEQUENCE)


def processor_contract(
    *,
    accepts: Shape = Shape.ANY,
    returns: Shape = Shape.ANY,
    requires_context: Iterable[str] = (),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to attach a :class:`ProcessorContract` to a processor.

    Works on functions, classes and callable instances::

        @processor_contract(accepts=Shape.SINGLE, returns=Shape.SINGLE)
        def clean(value):
            return value.strip()
    """
    contract = ProcessorContract(
        accepts=accepts,
        returns=returns,
        requires_context=frozenset(requires_context),
    )

    def decorator(proc: Callable[..., Any]) -> Callable[..., Any]:
        proc.__processor_contract__ = contract  # type: ignore[attr-defined]
        return proc

    return decorator


def _composite(proc: Any) -> tuple[str | None, tuple[Callable[..., Any], ...]]:
    """Return the composition mode (``chain`` or ``map``) and inner functions
    of composite processors such as Compose and MapCompose."""
    mode = getattr(proc, "_contract_mode", None)
    functions = getattr(proc, "functions", None)
    if mode in ("chain", "map") and functions is not None:
        return mode, tuple(functions)
    return None, ()


def get_processor_contract(proc: Callable[..., Any]) -> ProcessorContract | None:
    """Return the contract declared by *proc*, or ``None`` if it is unknown.

    Contracts of composite processors (Compose, MapCompose) are derived from
    the contracts of their inner functions.
    """
    contract = getattr(proc, "__processor_contract__", None)
    if contract is not None:
        return contract
    mode, functions = _composite(proc)
    if mode is None:
        return None
    known = [c for c in (get_processor_contract(f) for f in functions) if c]
    requires = (
        frozenset().union(*(c.requires_context for c in known))
        if known
        else frozenset()
    )
    if mode == "map":
        # MapCompose normalizes its input with arg_to_iter and always
        # returns a list built from its inner functions.
        return ProcessorContract(
            accepts=Shape.ANY, returns=Shape.SEQUENCE, requires_context=requires
        )
    first = get_processor_contract(functions[0]) if functions else None
    last = get_processor_contract(functions[-1]) if functions else None
    return ProcessorContract(
        accepts=first.accepts if first else Shape.ANY,
        returns=last.returns if last else Shape.ANY,
        requires_context=requires,
    )


class ProcessorContractError(ValueError):
    """Raised when statically declared processor contracts are incompatible."""


@dataclass
class ContractReport:
    """Result of statically checking the processor chains of a loader.

    :ivar issues: definite incompatibilities between adjacent processing stages.
    :ivar unknown: processing stages whose processor declares no contract.
    """

    issues: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no definite incompatibility was found."""
        return not self.issues


def _proc_name(proc: Callable[..., Any]) -> str:
    name = getattr(proc, "__name__", None)
    if name:
        return name
    return type(proc).__name__


def _check_processor(
    label: str,
    proc: Callable[..., Any],
    context_keys: set[str],
    report: ContractReport,
) -> None:
    contract = get_processor_contract(proc)
    if contract is None:
        report.unknown.append(label)
        return
    mode, functions = _composite(proc)
    if mode is None:
        missing = contract.requires_context - context_keys
        if missing:
            missing_keys = ", ".join(sorted(repr(key) for key in missing))
            report.issues.append(
                f"{label} requires loader context keys that are missing: {missing_keys}"
            )
        return
    if mode == "chain":
        stages = [
            (
                f"{label} stage {index} ({_proc_name(func)})",
                get_processor_contract(func),
            )
            for index, func in enumerate(functions, 1)
        ]
        for (label_a, contract_a), (label_b, contract_b) in pairwise(stages):
            if (
                contract_a is not None
                and contract_b is not None
                and not _shapes_compatible(contract_a.returns, contract_b.accepts)
            ):
                report.issues.append(
                    f"{label_a} returns {contract_a.returns.value}, "
                    f"but {label_b} expects {contract_b.accepts.value}"
                )
    else:  # "map": each inner function is applied to single values
        for index, func in enumerate(functions, 1):
            func_contract = get_processor_contract(func)
            if func_contract is not None and not _shapes_compatible(
                Shape.SINGLE, func_contract.accepts
            ):
                report.issues.append(
                    f"{label} stage {index} ({_proc_name(func)}) is applied to "
                    f"single values, but it expects {func_contract.accepts.value}"
                )
    for index, func in enumerate(functions, 1):
        _check_processor(
            f"{label} stage {index} ({_proc_name(func)})", func, context_keys, report
        )


def validate_processor_chain(
    chain_label: str,
    input_processor: Callable[..., Any],
    output_processor: Callable[..., Any],
    context_keys: set[str],
    report: ContractReport,
) -> None:
    """Statically check one effective input/output processor pair.

    Adjacent stages are checked against each other using their declared
    contracts; processors without a contract are recorded as unknown and
    treated as compatible with everything. No processor is ever called.
    """
    input_label = f"{chain_label}: input processor ({_proc_name(input_processor)})"
    output_label = f"{chain_label}: output processor ({_proc_name(output_processor)})"
    stages = [
        ("extracted values", _VALUES_CONTRACT),
        (
            f"input processor ({_proc_name(input_processor)})",
            get_processor_contract(input_processor),
        ),
        ("collected values", _VALUES_CONTRACT),
        (
            f"output processor ({_proc_name(output_processor)})",
            get_processor_contract(output_processor),
        ),
    ]
    for (label_a, contract_a), (label_b, contract_b) in pairwise(stages):
        if (
            contract_a is not None
            and contract_b is not None
            and not _shapes_compatible(contract_a.returns, contract_b.accepts)
        ):
            report.issues.append(
                f"{chain_label}: {label_a} provides {contract_a.returns.value}, "
                f"but {label_b} expects {contract_b.accepts.value}"
            )
    _check_processor(input_label, input_processor, context_keys, report)
    _check_processor(output_label, output_processor, context_keys, report)
