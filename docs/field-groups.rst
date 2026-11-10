.. currentmodule:: itemloaders

.. _field-groups:

Field groups
============

When several fields are extracted from the same source rows, they have to stay
aligned row by row. Consider product variants, each with a color and a stock
count, scraped from the same table rows::

    rows = [
        {"color": "red", "stock": "3"},
        {"color": "", "stock": "5"},
        {"color": "blue", "stock": None},
    ]

If the two fields are collected separately and each goes through its own
:class:`~itemloaders.processors.MapCompose`, the values dropped by one field
make the two columns shift independently. The second color and the third stock
disappear, but nothing tells the loader that the remaining values no longer
describe the same variants::

    loader.add_value("color", [row["color"] for row in rows], drop_empty)
    loader.add_value("stock", [row["stock"] for row in rows], drop_empty)
    # color: ["red", "blue"]
    # stock: ["3", "5"]            # 5 used to belong to the color-less row

A *field group* keeps the source rows together. Pass the rows to
:meth:`~ItemLoader.add_value` with ``field_name=None``. Each row is a mapping
of field names to values and is cleaned as one record: every field runs its own
input processor, but the record itself is the unit that survives or gets
dropped::

    from itemloaders import ItemLoader
    from itemloaders.processors import MapCompose

    class VariantLoader(ItemLoader):
        color_in = MapCompose(drop_empty, str.strip)
        stock_in = MapCompose(drop_empty, int)

    loader = VariantLoader()
    loader.add_value(None, rows)

The collected records show exactly what happened to each source row. A field
filtered out inside a record is missing from that record only, and a missing
field is never filled with a value taken from another record::

    loader.get_collected_group_records()
    # [
    #     {"color": ["red"], "stock": [3]},
    #     {"stock": [5]},
    #     {"color": ["blue"]},
    # ]

A record from which every field is dropped disappears entirely. In both cases
the relationship between the surviving fields is preserved, and
:meth:`~ItemLoader.load_item` realigns the records back into the output fields
in record order::

    loader.load_item()
    # {"color": ["red", "blue"], "stock": [3, 5]}

A single mapping is accepted as a one-record group::

    loader.add_value(None, {"color": "red", "stock": 3})

One-to-many transforms
----------------------

An input function may return several values, the way
:class:`~itemloaders.processors.MapCompose` normally flattens them. Inside a
field group the extra values stay attached to the record being processed. For
example a color written as ``"red / crimson"`` can be split into two colors
that both remain paired with the stock of that same variant. Inspect
:meth:`~ItemLoader.get_collected_group_records` to see the per-record result;
the output fields themselves stay flat lists of scalar values, as with the
single-field API.

Mixing field groups and single fields
-------------------------------------

The single-field API (:meth:`~ItemLoader.add_value` with an actual field name)
is unchanged. A field must however not be written both ways without an explicit
decision, because the grouped values and the standalone values have no common
row to align on.

By default the loader raises :class:`FieldGroupConflictError` as soon as a
field is used both inside a field group and through the single-field API::

    loader.add_value(None, [{"color": "red", "stock": 3}])
    loader.add_value("color", "blue")   # FieldGroupConflictError

Set :attr:`~ItemLoader.group_conflict_policy` to ``"merge"`` to allow both
sources. Their values are then combined in write order, the grouped values
coming from their records and the standalone values appearing where they were
added::

    loader = VariantLoader(group_conflict_policy="merge")
    loader.add_value(None, [{"color": "red", "stock": 3}])
    loader.add_value("color", "blue")
    loader.load_item()
    # {"color": ["red", "blue"], "stock": [3]}

The policy can also be declared as a :attr:`~ItemLoader.group_conflict_policy`
class attribute, and nested loaders inherit it from their parent.
