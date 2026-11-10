.. currentmodule:: itemloaders

.. _async-loaders:

Asynchronous Item Loaders
=========================

Some input processors cannot work synchronously, for example ones that query
an internal address standardization service over the network.
:class:`AsyncItemLoader` is an :class:`ItemLoader` variant that accepts
coroutine input processors, so those calls can run concurrently while the
rest of the loader API keeps working the usual way.

Synchronous and coroutine processors can be mixed freely on the same loader.
The synchronous methods never start an event loop themselves: coroutine
processors require an event loop already running in the current thread, and the
item is loaded with ``await loader.load_item()``.

Basic usage
-----------

Declare coroutine processors the same way as regular ones, using the ``_in``
suffix, field metadata or :attr:`~ItemLoader.default_input_processor`::

    import asyncio

    from itemloaders import AsyncItemLoader
    from itemloaders.processors import TakeFirst


    class AddressService:
        async def standardize(self, values):
            # In real code this would call the address service over HTTP.
            await asyncio.sleep(0)
            return [f"standardized: {value}" for value in values]


    service = AddressService()


    class PlaceLoader(AsyncItemLoader):
        default_output_processor = TakeFirst()

        async def address_in(self, values):
            return await service.standardize(values)


    async def main():
        loader = PlaceLoader(concurrent_input_processors=4)
        loader.add_value("name", "The Old Inn")  # synchronous processing
        loader.add_value("address", "1 Main St")
        loader.add_value("address", "2 Oak Ave")
        item = await loader.load_item()
        # {'name': 'The Old Inn',
        #  'address': 'standardized: 1 Main St'}

Only field input processors may be coroutines; inline processors passed to
:meth:`~ItemLoader.add_value` and output processors stay synchronous.

Concurrency and ordering
------------------------

Every value handed to a coroutine processor is scheduled as a task, and at
most :attr:`~AsyncItemLoader.concurrent_input_processors` of those tasks are
in flight at any time, independently of how many values are added::

    loader = PlaceLoader(concurrent_input_processors=4)

The calls may finish in any order, for example when the address service
responds to later requests first. That does not affect the collected data:
the values of every field are always written in the order in which they were
added.

Failures and cancellation
-------------------------

If a processor raises, the loader is poisoned: the exception is re-raised by
:meth:`~AsyncItemLoader.load_item`, the calls still in flight are cancelled
and awaited so their cleanup runs, and every further attempt to add or load
values raises as well. No value can reach the item after a failure.

Cancelling ``load_item()``, for example through :func:`asyncio.timeout` or
:func:`asyncio.wait_for`, has the same effect. Use
:meth:`~AsyncItemLoader.close` to discard a loader without loading it, or use
the loader as an asynchronous context manager, which closes it automatically
even when the ``async with`` block raises::

    try:
        async with PlaceLoader(concurrent_input_processors=4) as loader:
            loader.add_value("address", "1 Main St")
            item = await asyncio.wait_for(loader.load_item(), timeout=5)
    except TimeoutError:
        # Every started call has been cancelled and properly wound down.
        ...

:ref:`Nested loaders <nested-loaders>` created from an :class:`AsyncItemLoader`
share its concurrency limit and its processing queue.
