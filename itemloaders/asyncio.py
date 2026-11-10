"""Asynchronous support for Item Loaders.

:class:`AsyncItemLoader` extends :class:`~itemloaders.ItemLoader` with support
for coroutine input processors, e.g. ones that query an internal address
standardization service over the network.

Synchronous and asynchronous input processors can be mixed freely. Values
handled by synchronous processors keep working without an event loop, while
values that need an asynchronous processor are scheduled as concurrent tasks.
The number of in-flight calls is bounded by
:attr:`AsyncItemLoader.concurrent_input_processors`, and the values collected
for each field keep the order in which they were added, no matter the order in
which the asynchronous calls finish.

The synchronous methods never start an event loop themselves: an event loop
must already be running when a value is handed to an asynchronous processor,
and :meth:`~AsyncItemLoader.load_item` is a coroutine.
"""

from __future__ import annotations

import asyncio
import inspect
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

from itemadapter import ItemAdapter

from itemloaders import ItemLoader, _StatsCollector
from itemloaders.common import wrap_loader_context
from itemloaders.utils import arg_to_iter

if TYPE_CHECKING:
    from re import Pattern
    from types import TracebackType

    from parsel import Selector


def _is_async_processor(processor: Any) -> bool:
    """Tell whether *processor* returns an awaitable when called."""
    if inspect.iscoroutinefunction(processor):
        return True
    return callable(processor) and inspect.iscoroutinefunction(processor.__call__)


@dataclass(slots=True)
class _PendingOp:
    """A scheduled input processor call, kept in input order."""

    field_name: str
    task: asyncio.Task[list[Any]]
    generation: int


@dataclass(slots=True)
class _AsyncState:
    """Engine state shared by a loader and its nested loaders."""

    semaphore: asyncio.Semaphore
    pending: deque[_PendingOp] = field(default_factory=deque)
    generations: dict[str, int] = field(default_factory=dict)
    commit_event: asyncio.Event = field(default_factory=asyncio.Event)
    idle_event: asyncio.Event = field(default_factory=asyncio.Event)
    committer: asyncio.Task[None] | None = None
    closed: bool = False
    fatal: BaseException | None = None


async def _process_input_value(
    loader: AsyncItemLoader, field_name: str, value: list[Any]
) -> list[Any]:
    """Run the input processor of *field_name*, sync or async."""
    proc = loader.get_input_processor(field_name)
    wrapped_proc = wrap_loader_context(proc, loader.context)
    try:
        if _is_async_processor(proc):
            result = await wrapped_proc(value)
        else:
            result = wrapped_proc(value)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        raise ValueError(
            f"Error with input processor {proc.__class__.__name__}:"
            f" field={field_name!r} value={value!r} error='{type(e).__name__}: {e!s}'"
        ) from e
    return list(arg_to_iter(result))


async def _run_call(
    loader: AsyncItemLoader,
    state: _AsyncState,
    field_name: str,
    value: list[Any],
) -> list[Any]:
    async with state.semaphore:
        return await _process_input_value(loader, field_name, value)


async def _committer(loader: AsyncItemLoader, state: _AsyncState) -> None:
    """Write processor results into the item, strictly in input order."""
    while True:
        while state.pending:
            op = state.pending[0]
            try:
                result = await op.task
            except asyncio.CancelledError as exc:
                # External cancellation of the committer itself must propagate;
                # a CancelledError raised by the processor poisons the loader.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                state.pending.popleft()
                await _fail(loader, state, exc)
                return
            except BaseException as exc:
                state.pending.popleft()
                await _fail(loader, state, exc)
                return
            state.pending.popleft()
            if op.generation != state.generations.get(op.field_name, 0):
                # A replace_value() scheduled after this call superseded it.
                continue
            if result:
                loader._values.setdefault(op.field_name, []).extend(result)
        state.idle_event.set()
        if state.closed:
            return
        state.commit_event.clear()
        # Re-check after clearing: an enqueue racing with us may have had its
        # wake-up signal cleared, but its operation is now in the queue.
        if state.pending:
            continue
        if state.closed:
            return
        await state.commit_event.wait()


async def _fail(
    loader: AsyncItemLoader, state: _AsyncState, exc: BaseException
) -> None:
    """Poison the loader and wind down every call that is still in flight."""
    state.fatal = exc
    remaining = [op.task for op in state.pending if not op.task.done()]
    state.pending.clear()
    for task in remaining:
        task.cancel()
    if remaining:
        await asyncio.gather(*remaining, return_exceptions=True)
    # Wake up a load_item() blocked in _drain() so it re-checks fatal.
    state.idle_event.set()


def _ensure_state(loader: AsyncItemLoader) -> _AsyncState:
    state = loader._async_state
    if state is None:
        state = _AsyncState(
            semaphore=asyncio.Semaphore(loader.concurrent_input_processors)
        )
        state.idle_event.set()
        loader._async_state = state
    return state


def _enqueue(
    loader: AsyncItemLoader,
    field_name: str,
    value: list[Any],
    *,
    replace: bool,
) -> None:
    state = _ensure_state(loader)
    if state.closed:
        raise RuntimeError("Cannot add values to a closed AsyncItemLoader")
    if state.fatal is not None:
        raise ValueError(
            "Cannot add values: an input processor has already failed; "
            "this AsyncItemLoader is unusable"
        ) from state.fatal
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:
        raise RuntimeError(
            "An asynchronous input processor needs a running event loop; "
            "run the loader from async code, the synchronous API does not "
            "start one for you"
        ) from exc
    if replace:
        generation = state.generations.get(field_name, 0) + 1
        state.generations[field_name] = generation
    else:
        generation = state.generations.get(field_name, 0)
    task = loop.create_task(_run_call(loader, state, field_name, value))
    state.idle_event.clear()
    state.pending.append(_PendingOp(field_name, task, generation))
    if state.committer is None or state.committer.done():
        state.committer = loop.create_task(_committer(loader, state))
    state.commit_event.set()


async def _drain(state: _AsyncState) -> None:
    """Wait for every scheduled call to be committed in order."""
    while True:
        if state.closed:
            raise RuntimeError("Cannot load a closed AsyncItemLoader")
        if state.fatal is not None:
            if state.committer is not None and not state.committer.done():
                await state.committer
            raise state.fatal
        if not state.pending:
            return
        await state.idle_event.wait()


class AsyncItemLoader(ItemLoader):
    """An :class:`~itemloaders.ItemLoader` supporting coroutine processors.

    Each field may use a regular synchronous processor or a coroutine
    processor, declared the usual way through the ``_in`` suffix, field
    metadata or :attr:`~ItemLoader.default_input_processor`. Both kinds can be
    mixed on the same loader.

    Values handed to asynchronous processors are processed concurrently, but
    at most :attr:`concurrent_input_processors` calls are in flight at any
    time, and the collected values of every field keep their input order even
    when the calls finish in a different order.

    A processor failure poisons the loader: the failure is re-raised by
    :meth:`load_item`, the remaining calls are cancelled and properly wound
    down, and every further attempt to add or load values raises, so no value
    can silently reach the item after a failure. Cancelling :meth:`load_item`
    has the same effect: started calls are cancelled and awaited, and no late
    result is written afterwards. Use :meth:`close` (or the loader as an async
    context manager) to release a loader without loading it.

    The synchronous methods do not create or start an event loop; they rely on
    a loop already running in the current thread, and :meth:`load_item` must be
    awaited from that loop.

    :param concurrent_input_processors: maximum number of input processor
        calls allowed to be in flight at the same time.
    """

    def __init__(
        self,
        item: Any = None,
        selector: Selector | None = None,
        parent: ItemLoader | None = None,
        stats: _StatsCollector | None = None,
        *,
        concurrent_input_processors: int = 8,
        **context: Any,
    ) -> None:
        if (
            not isinstance(concurrent_input_processors, int)
            or isinstance(concurrent_input_processors, bool)
            or concurrent_input_processors < 1
        ):
            raise ValueError(
                "concurrent_input_processors must be a positive integer, got "
                f"{concurrent_input_processors!r}"
            )
        super().__init__(
            item=item, selector=selector, parent=parent, stats=stats, **context
        )
        self._local_async_state: _AsyncState | None = None
        self.concurrent_input_processors: int = concurrent_input_processors

    @property
    def _async_state(self) -> _AsyncState | None:
        if isinstance(self.parent, AsyncItemLoader):
            return self.parent._async_state
        return self._local_async_state

    @_async_state.setter
    def _async_state(self, state: _AsyncState | None) -> None:
        if isinstance(self.parent, AsyncItemLoader):
            self.parent._async_state = state
        else:
            self._local_async_state = state

    def _field_uses_async_processor(self, field_name: str) -> bool:
        return _is_async_processor(self.get_input_processor(field_name))

    def get_value(
        self,
        value: Any,
        *processors: Any,
        re: str | Pattern[str] | None = None,
        **kw: Any,
    ) -> Any:
        for proc in processors:
            if _is_async_processor(proc):
                raise RuntimeError(
                    "Coroutine processors are only supported as field input "
                    "processors of AsyncItemLoader, not as inline processors"
                )
        return super().get_value(value, *processors, re=re, **kw)

    def _add_value(self, field_name: str, value: Any) -> None:
        value = arg_to_iter(value)
        state = self._async_state
        if state is None and not self._field_uses_async_processor(field_name):
            processed_value = self._process_input_value(field_name, value)
            if processed_value:
                self._values.setdefault(field_name, [])
                self._values[field_name] += arg_to_iter(processed_value)
            return
        _enqueue(self, field_name, list(value), replace=False)

    def _replace_value(self, field_name: str, value: Any) -> None:
        value = arg_to_iter(value)
        state = self._async_state
        if state is None and not self._field_uses_async_processor(field_name):
            self._values.pop(field_name, None)
            processed_value = self._process_input_value(field_name, value)
            if processed_value:
                self._values.setdefault(field_name, [])
                self._values[field_name] += arg_to_iter(processed_value)
            return
        # Values already collected disappear now; the replace marker makes the
        # committer drop values still in flight before writing the new ones.
        self._values.pop(field_name, None)
        _enqueue(self, field_name, list(value), replace=True)

    async def load_item(self) -> Any:
        """Populate and return the item once all scheduled calls finished.

        Waits for every in-flight input processor call, preserving input
        order, then runs the (synchronous) output processors and returns the
        item, like :meth:`ItemLoader.load_item`.

        Raises if a processor failed, the loader was closed, or the wait was
        cancelled.
        """
        state = self._async_state
        if state is None:
            return super().load_item()
        try:
            await _drain(state)
        except asyncio.CancelledError:
            await self.close()
            raise
        adapter = ItemAdapter(self.item)
        for field_name in tuple(self._values):
            value = self.get_output_value(field_name)
            if value is not None:
                adapter[field_name] = value
        return adapter.item

    async def close(self) -> None:
        """Cancel every scheduled call and mark the loader as unusable.

        In-flight processor calls are cancelled and awaited so that their
        cleanup (e.g. releasing network resources) always runs. The method is
        idempotent.
        """
        state = _ensure_state(self)
        if state.closed:
            return
        state.closed = True
        tasks: list[asyncio.Task[Any]] = [
            op.task for op in state.pending if not op.task.done()
        ]
        state.pending.clear()
        state.commit_event.set()
        if state.committer is not None and not state.committer.done():
            tasks.append(state.committer)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Wake up a load_item() blocked in _drain() so it sees the closure.
        state.idle_event.set()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()
