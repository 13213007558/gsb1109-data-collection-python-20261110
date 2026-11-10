"""Offline diagnosis: the same collected values are run twice but the second
run is missing an entry.

Three root causes are reproduced against the *real* ItemLoader call path:

  A. a processor mutates its input object in place;
  B. a mutable object shared through the loader ``context`` is mutated;
  C. a one-shot iterator (generator) is consumed before the second run.

Observation rules (do not change the behaviour under diagnosis):

* Identity is recorded with ``id()`` / ``is`` and generator state with
  ``inspect.getgeneratorstate()``. None of these advance an iterator.
* No generator is ever materialised for logging. Lists are re-iterable, so
  ``len()``/``list()`` on lists is safe and used only on lists.
* Every log line is emitted at a value's natural hand-off point (inside the
  processor or around the single pipeline call that the scenario needs).
  Business functions are never re-executed just to add logs.

ItemLoader call path used below (see itemloaders/__init__.py):

  add_value -> get_value (per-call processors, context wrapped at :310)
            -> _add_value (:265) -> arg_to_iter (utils.py:51, Iterator passed
               through as-is at :67) -> _process_input_value (:376)
            -> internal list ``_values`` (:269-270, ``+=`` consumes iterators)
  load_item -> get_output_value (:334) hands the *internal* list itself to the
               output processor (:341-343), with no defensive copy.
"""

from __future__ import annotations

import inspect
import sys
import types
from pathlib import Path

# Allow ``python diagnostics/diagnose_double_run_missing_item.py`` from a
# checkout without an installed itemloaders package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parsel import Selector

from itemloaders import ItemLoader
from itemloaders.processors import Identity, MapCompose

DATA = ["a", "b", "c"]


# --------------------------------------------------------------------------- #
# Non-invasive tracing helpers
# --------------------------------------------------------------------------- #


def label(value: object) -> str:
    """Identity label that never iterates *value*."""
    return f"{type(value).__name__}@{id(value):#x}"


def describe(value: object) -> str:
    """Describe *value* without consuming any iterator it may contain."""
    if isinstance(value, types.GeneratorType):
        return f"{label(value)} state={inspect.getgeneratorstate(value)}"
    if isinstance(value, list):
        return f"{label(value)} len={len(value)} {value!r}"
    return f"{label(value)} {value!r}"


def trace(case: str, stage: str, **fields: object) -> None:
    print(f"[{case}] {stage}")
    for key, value in fields.items():
        print(f"    {key:<12}: {describe(value) if not isinstance(value, str) else value}")


def gen_state(name: str, gen: object) -> str:
    return f"{name} {label(gen)} state={inspect.getgeneratorstate(gen)}"


# ============================================================================ #
# Case A: a processor mutates its input
# ============================================================================ #


def case_a_processor_mutates_input() -> None:
    """A1: an input processor mutates the caller's own list in place.

    ``_add_value`` passes ``arg_to_iter(value)`` straight into the input
    processor (itemloaders/__init__.py:266-267). For a list that is the exact
    object the caller handed to ``add_value``. Two fresh loaders reusing the
    same source therefore see it shrink run after run.
    """
    print("\n=== Case A1: input processor mutates the caller's list ===")

    def popping_input(values: list) -> list:
        trace("A1", "input received", incoming=values)
        dropped = values.pop()  # in-place mutation, same object returned
        trace("A1", "input handed over", outgoing=values, note=f"popped {dropped!r}")
        return values

    class PoppingInputLoader(ItemLoader):
        f_in = staticmethod(popping_input)

    source = list(DATA)
    trace("A1", "caller source before run 1", source=source)
    loader1 = PoppingInputLoader()
    loader1.add_value("f", source)
    run1 = loader1.load_item()
    trace("A1", "caller source after run 1", source=source)

    # The "second run" is a fresh loader over the same, now corrupted source.
    loader2 = PoppingInputLoader()
    loader2.add_value("f", source)
    run2 = loader2.load_item()
    trace("A1", "caller source after run 2", source=source)
    print(f"[A1] run 1 item          : {run1}")
    print(f"[A1] run 2 item          : {run2}  (one entry shorter)")
    assert source == ["a"]
    assert run1["f"] == ["a", "b"]
    assert run2["f"] == ["a"]


def case_a_output_processor_mutates_internal_list() -> None:
    """A2: an output processor pops from the internal ``_values`` list.

    ``get_output_value`` passes ``self._values.get(field_name, [])`` itself to
    the output processor (itemloaders/__init__.py:341-343), and ``load_item``
    stores whatever the processor returns back into the item (:330). Returning
    the same list therefore aliases item, internal store and processor input;
    each ``load_item`` shrinks it and retroactively rewrites earlier results.
    """
    print("\n=== Case A2: output processor mutates the internal collected list ===")

    def popping_output(values: list) -> list:
        trace("A2", "output received", incoming=values)
        dropped = values.pop()
        trace("A2", "output handed over", outgoing=values, note=f"popped {dropped!r}")
        return values

    class PoppingLoader(ItemLoader):
        f_out = staticmethod(popping_output)

    loader = PoppingLoader()
    loader.add_value("f", list(DATA))
    internal = loader.get_collected_values("f")
    trace("A2", "internal list before load 1", internal=internal)

    run1 = loader.load_item()
    snapshot_after_load1 = list(internal)  # copy for the timeline; not part of the pipeline
    trace("A2", "internal list after load 1", internal=internal)
    print(f"[A2] item['f'] aliases internal list after load 1: {run1['f'] is internal}")
    run2 = loader.load_item()
    trace("A2", "internal list after load 2", internal=internal)
    print(f"[A2] run 1 item (read after load 2): {run1}")
    print(f"[A2] run 2 item                      : {run2}")
    print(f"[A2] run 1 snapshot taken right after load 1: {snapshot_after_load1}")
    assert snapshot_after_load1 == ["a", "b"]
    assert loader.get_collected_values("f") == ["a"]
    # The item stores the very list the processor returned, so the second load
    # retroactively changes the first run's item too.
    assert run1["f"] is internal is run2["f"]
    assert run1["f"] == ["a"]


# ============================================================================ #
# Case B: a mutable object shared via loader context is mutated
# ============================================================================ #


def case_b_shared_context_mutated() -> None:
    """Two loaders are built with the *same* context object. A stateful
    processor accumulates data in ``loader_context['seen']``; the second run
    filters every value out even though its source data is identical.

    ``wrap_loader_context`` binds the live context mapping with
    ``partial(function, loader_context=context)`` (common.py:30): the
    processor always sees the same mutable object, never a copy.
    """
    print("\n=== Case B: processor mutates a context object shared across runs ===")

    def context_dedupe(value, loader_context):
        seen = loader_context["seen"]
        trace(
            "B",
            "processor call",
            value=repr(value),
            context_id=f"context {label(loader_context)} seen {label(seen)} contents={list(seen)}",
        )
        if value in seen:
            return None  # MapCompose drops None results
        seen.append(value)
        return value

    class DedupeLoader(ItemLoader):
        f_in = MapCompose(context_dedupe)

    shared_seen: list = []
    source1 = list(DATA)
    loader1 = DedupeLoader(seen=shared_seen)
    print(f"[B] loader1.context is caller dict's neighbour; seen object: {label(shared_seen)}")
    loader1.add_value("f", source1)
    run1 = loader1.load_item()

    source2 = list(DATA)  # identical, fresh source
    loader2 = DedupeLoader(seen=shared_seen)  # same context object reused
    loader2.add_value("f", source2)
    run2 = loader2.load_item()

    print(f"[B] run 1 item           : {run1}")
    print(f"[B] run 2 item           : {run2}")
    print(f"[B] both contexts alias one object: "
          f"{loader1.context['seen'] is loader2.context['seen'] is shared_seen}")
    print(f"[B] source lists untouched: {source1} / {source2}")
    assert run1 == {"f": ["a", "b", "c"]}
    assert run2 == {}  # whole field missing on the second run
    assert source1 == source2 == DATA


def case_b_nested_loader_context_aliasing() -> None:
    """``nested_xpath`` builds a new context *dict* but copies values by
    reference (_get_nested_context, itemloaders/__init__.py:192-195). A
    mutation through the child is visible to the parent and to every sibling.
    """
    print("\n=== Case B (nested): shallow-copied context keeps aliased values ===")
    registry: list = []
    root = ItemLoader(selector=Selector(text="<div><span>x</span></div>"), registry=registry)
    child = root.nested_xpath("//span")

    print(f"[B-n] context dicts are distinct objects : {child.context is not root.context}")
    print(f"[B-n] registry value is aliased          : "
          f"{child.context['registry'] is root.context['registry'] is registry}")
    child.context["registry"].append("from-child")
    print(f"[B-n] parent sees child's mutation       : {root.context['registry']}")
    assert root.context["registry"] == ["from-child"]


# ============================================================================ #
# Case C: a one-shot iterator is consumed before the second run
# ============================================================================ #


def case_c_generator_passed_twice() -> None:
    """C1: one generator handed to ``add_value`` twice on the same loader.

    ``arg_to_iter`` returns an Iterator as-is (utils.py:67), then
    ``_values[field_name] += arg_to_iter(processed_value)``
    (itemloaders/__init__.py:270) exhausts it while extending the internal
    list. The second hand-over yields nothing.
    """
    print("\n=== Case C1: the same generator is added twice ===")
    loader = ItemLoader()
    gen = (letter for letter in DATA)

    print(f"[C1] {gen_state('before add 1', gen)}")
    loader.add_value("f", gen)
    print(f"[C1] {gen_state('after add 1 ', gen)}")
    after_first = loader.get_collected_values("f")
    print(f"[C1] collected after add 1: {after_first}")

    loader.add_value("f", gen)  # already exhausted: no error, no values
    print(f"[C1] {gen_state('after add 2 ', gen)}")
    after_second = loader.get_collected_values("f")
    print(f"[C1] collected after add 2: {after_second} (second run added nothing)")
    assert after_first == DATA
    assert after_second == DATA
    assert inspect.getgeneratorstate(gen) == inspect.GEN_CLOSED


def case_c_generator_shared_between_loaders() -> None:
    """C2: the same generator feeds two loader instances. The first loader
    consumes it; the second silently collects an empty field.
    """
    print("\n=== Case C2: one generator shared by two loader instances ===")
    first = ItemLoader()
    second = ItemLoader()
    gen = (letter for letter in DATA)

    first.add_value("f", gen)
    print(f"[C2] {gen_state('after loader 1', gen)}")
    second.add_value("f", gen)
    run1 = first.load_item()
    run2 = second.load_item()
    print(f"[C2] loader 1 item       : {run1}")
    print(f"[C2] loader 2 item       : {run2}  (field present but empty)")
    print("[C2] an exhausted generator is still truthy, so _add_value creates "
          "the field key but '+= ' yields no elements.")
    assert run1 == {"f": ["a", "b", "c"]}
    assert run2 == {"f": []}
    assert second.get_collected_values("f") == []


def case_c_generator_nested_in_list() -> None:
    """C3: a generator nested inside a list is stored as an element verbatim
    (lists are iterated, their elements are not). An iterating output
    processor consumes it on the first ``load_item``; the second run gets an
    empty result from the same stored element.

    Note the second aliasing rule here: ``load_item`` returns ``self.item``
    every time (itemloaders/__init__.py:332), i.e. the same object. The second
    load overwrites the field on the dict the first result points to, so the
    run-1 value is snapshotted immediately instead of being read afterwards.
    """
    print("\n=== Case C3: generator stored as a list element, output consumes it ===")

    class IteratingOutput(ItemLoader):
        f_out = MapCompose(Identity())

    loader = IteratingOutput()
    gen = (letter for letter in ["x", "y"])
    loader.add_value("f", [gen])

    stored = loader.get_collected_values("f")
    print(f"[C3] stored element aliases the generator: {stored[0] is gen}")
    print(f"[C3] {gen_state('before load 1', gen)}")
    item1 = loader.load_item()
    snapshot_run1 = list(item1["f"])  # freeze run-1 output before run 2 rewrites it
    print(f"[C3] {gen_state('after load 1 ', gen)}")
    item2 = loader.load_item()
    print(f"[C3] {gen_state('after load 2 ', gen)}")
    print(f"[C3] both load_item() calls return the same item object: {item1 is item2}")
    print(f"[C3] run 1 output (snapshot at load 1) : {snapshot_run1}")
    print(f"[C3] item after run 2 (both references): {item2}")
    assert snapshot_run1 == ["x", "y"]
    assert item1 is item2
    assert item2["f"] == []


# ============================================================================ #
# Triage guide
# ============================================================================ #


def print_triage_guide() -> None:
    print(
        """
=========================== triage: which case are you in? ===========================
Symptom: same input values, second run shorter. Inspect in this order:

1. Iterator state (case C)?
   - type-check the value handed to add_value / stored in get_collected_values
     against collections.abc.Iterator (a generator qualifies).
   - read inspect.getgeneratorstate(obj): GEN_CREATED before first use,
     GEN_CLOSED after consumption. This read does NOT advance the iterator.
   - A generator appearing as an element of the collected list means the
     output processor will consume it on load_item (case C3).

2. Input object identity (case A)?
   - Record id() of the value the processor receives and id() of the caller's
     source / of loader.get_collected_values(field). Same id + changed content
     after the run means in-place mutation (input proc -> caller's object;
     output proc -> the loader's internal list).

3. Context object identity (case B)?
   - Compare loader.context['key'] by identity across the two runs and across
     parent/nested loaders. Same mutable object whose content grew between runs
     means shared-context state. Nested loaders copy the context dict shallowly,
     so nested values are aliased even though the dicts differ.

Non-goals honoured here: no iterator is consumed for observation, and no
business function is run a second time merely to collect logs.
======================================================================================"""
    )


def main() -> int:
    scenarios = (
        case_a_processor_mutates_input,
        case_a_output_processor_mutates_internal_list,
        case_b_shared_context_mutated,
        case_b_nested_loader_context_aliasing,
        case_c_generator_passed_twice,
        case_c_generator_shared_between_loaders,
        case_c_generator_nested_in_list,
    )
    for scenario in scenarios:
        scenario()
    print_triage_guide()
    print(f"all {len(scenarios)} scenarios reproduced and assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
