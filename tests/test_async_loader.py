"""Tests for the asynchronous loader.

The address standardization service is faked: every call blocks on a future
that the test resolves or fails by hand, so completion order, concurrency and
cancellation are fully controllable without any real network service.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from itemloaders import AsyncItemLoader
from itemloaders.processors import Identity, MapCompose, TakeFirst

if TYPE_CHECKING:
    from collections.abc import Callable


class FakeAddressService:
    """Stand-in for the internal address standardization service.

    A call stays in flight until the test calls :meth:`resolve` or
    :meth:`fail` on it, so the completion order is controlled manually.
    """

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.total_calls = 0
        self.calls: dict[int, asyncio.Future[str]] = {}
        self.start_order: list[int] = []
        self.cancelled: list[int] = []

    async def normalize(self, values: list[Any]) -> list[Any]:
        call_id = self.total_calls
        self.total_calls += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self.calls[call_id] = future
        self.start_order.append(call_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            result = await future
        except asyncio.CancelledError:
            self.cancelled.append(call_id)
            raise
        finally:
            self.active -= 1
        return [f"{result}:{value}" for value in values]

    def resolve(self, call_id: int, result: str = "STD") -> None:
        self.calls[call_id].set_result(result)

    def fail(self, call_id: int, exc: BaseException) -> None:
        self.calls[call_id].set_exception(exc)


async def normalize_addresses(
    values: list[Any], loader_context: dict[str, Any]
) -> list[Any]:
    service: FakeAddressService = loader_context["address_service"]
    return await service.normalize(values)


async def raise_cancelled(values: list[Any]) -> list[Any]:
    raise asyncio.CancelledError("processor gave up")


async def wait_until(predicate: Callable[[], bool]) -> None:
    try:
        async with asyncio.timeout(1.0):
            while not predicate():  # noqa: ASYNC110
                await asyncio.sleep(0)
    except TimeoutError as exc:
        raise AssertionError("timed out waiting for condition") from exc


async def wait_started(service: FakeAddressService, call_id: int) -> None:
    await wait_until(lambda: call_id in service.start_order)


class AddressLoader(AsyncItemLoader):
    address_in = normalize_addresses
    city_in = normalize_addresses
    default_output_processor: Callable[..., Any] = Identity()


def make_loader(service: FakeAddressService, limit: int = 8) -> AddressLoader:
    return AddressLoader(address_service=service, concurrent_input_processors=limit)


class TestAsyncItemLoader:
    def test_concurrency_limit(self):
        async def scenario() -> None:
            service = FakeAddressService()
            loader = make_loader(service, limit=2)
            for index in range(5):
                loader.add_value("address", f"a{index}")
            await wait_until(lambda: service.active == 2)
            assert service.max_active == 2

            service.resolve(0)
            for call_id in range(1, 5):
                await wait_started(service, call_id)
                service.resolve(call_id)
            item = await loader.load_item()
            assert service.max_active == 2
            assert item["address"] == [f"STD:a{index}" for index in range(5)]

        asyncio.run(scenario())

    def test_input_order_with_out_of_order_completion(self):
        async def scenario() -> None:
            service = FakeAddressService()
            loader = make_loader(service, limit=4)
            for index in range(4):
                loader.add_value("address", f"a{index}")
            await wait_until(lambda: service.active == 4)
            for call_id in (3, 2, 1, 0):
                service.resolve(call_id, result=f"R{call_id}")
            item = await loader.load_item()
            assert item["address"] == [
                "R0:a0",
                "R1:a1",
                "R2:a2",
                "R3:a3",
            ]

        asyncio.run(scenario())

    def test_order_is_kept_per_field(self):
        async def scenario() -> None:
            service = FakeAddressService()
            loader = make_loader(service, limit=4)
            loader.add_value("address", "a0")
            loader.add_value("city", "c0")
            loader.add_value("address", "a1")
            loader.add_value("city", "c1")
            await wait_until(lambda: service.active == 4)
            for call_id in (3, 2, 1, 0):
                service.resolve(call_id)
            item = await loader.load_item()
            assert item["address"] == ["STD:a0", "STD:a1"]
            assert item["city"] == ["STD:c0", "STD:c1"]

        asyncio.run(scenario())

    def test_mixed_sync_and_async_processors(self):
        async def scenario() -> None:
            service = FakeAddressService()

            class MixedLoader(AsyncItemLoader):
                address_in = normalize_addresses
                name_in = MapCompose(str.upper)  # synchronous field processor

            loader = MixedLoader(address_service=service, limit=2)
            loader.add_value("name", "bob")
            loader.add_value("address", "a0")
            loader.add_value("name", "ann", str.title)  # inline sync processor
            await wait_until(lambda: service.active == 1)
            service.resolve(0)
            item = await loader.load_item()
            # The inline title() runs first, then the field's upper() processor.
            assert item["name"] == ["BOB", "ANN"]
            assert item["address"] == ["STD:a0"]

        asyncio.run(scenario())

    def test_inline_async_processors_are_rejected(self):
        async def coro_processor(value: Any) -> Any:
            return value

        loader = AsyncItemLoader()
        with pytest.raises(RuntimeError, match="Coroutine processors"):
            loader.get_value("x", coro_processor)
        with pytest.raises(RuntimeError, match="Coroutine processors"):
            loader.add_value("name", "x", coro_processor)

    def test_failure_poisons_loader_and_cancels_calls(self):
        async def scenario() -> None:
            service = FakeAddressService()
            loader = make_loader(service, limit=2)
            for index in range(4):
                loader.add_value("address", f"a{index}")
            await wait_until(lambda: service.active == 2)

            # The head call fails: nothing can be committed before it, and the
            # calls queued behind it must be wound down rather than written.
            service.fail(0, RuntimeError("service down"))
            with pytest.raises(ValueError, match="Error with input processor"):
                await loader.load_item()

            await wait_until(lambda: service.active == 0)
            # Call 0 failed; every other call that started must have been
            # cancelled, and none may have written anything.
            assert 1 in service.cancelled
            assert set(service.cancelled) == set(service.start_order) - {0}
            assert "address" not in loader._values

            # No value may reach the item after the failure.
            with pytest.raises(ValueError, match="already failed"):
                loader.add_value("address", "late")
            with pytest.raises(ValueError, match="Error with input processor"):
                await loader.load_item()

        asyncio.run(scenario())

    def test_failure_discards_successful_late_calls(self):
        async def scenario() -> None:
            service = FakeAddressService()
            loader = make_loader(service, limit=3)
            for index in range(3):
                loader.add_value("address", f"a{index}")
            await wait_until(lambda: service.active == 3)
            service.resolve(1)
            service.resolve(2)
            service.fail(0, RuntimeError("boom"))
            with pytest.raises(ValueError, match="Error with input processor"):
                await loader.load_item()
            assert "address" not in loader._values
            assert service.active == 0

        asyncio.run(scenario())

    def test_cancellation_winds_down_started_calls(self):
        async def scenario() -> None:
            service = FakeAddressService()
            loader = make_loader(service, limit=2)
            for index in range(4):
                loader.add_value("address", f"a{index}")
            await wait_until(lambda: service.active == 2)

            with pytest.raises(TimeoutError):
                await asyncio.wait_for(loader.load_item(), timeout=0.05)

            await wait_until(lambda: service.active == 0)
            assert sorted(service.cancelled) == [0, 1]
            assert "address" not in loader._values
            state = loader._async_state
            assert state is not None
            assert state.semaphore._value == 2  # acquired slots are released

            with pytest.raises(RuntimeError, match="closed"):
                loader.add_value("address", "late")
            with pytest.raises(RuntimeError, match="closed"):
                await loader.load_item()

        asyncio.run(scenario())

    def test_failure_keeps_values_committed_before_it(self):
        async def scenario() -> None:
            service = FakeAddressService()
            loader = make_loader(service, limit=3)
            for index in range(3):
                loader.add_value("address", f"a{index}")
            await wait_until(lambda: service.active == 3)

            # The head call completes and is committed in order, then the next
            # call fails: the committed value stays, the rest is wound down.
            service.resolve(0)
            await wait_until(lambda: loader._values.get("address") == ["STD:a0"])
            service.fail(1, RuntimeError("boom"))
            with pytest.raises(ValueError, match="Error with input processor"):
                await loader.load_item()
            assert loader._values["address"] == ["STD:a0"]
            assert service.active == 0

        asyncio.run(scenario())

    def test_close_while_load_item_is_blocked_wakes_it(self):
        async def scenario() -> None:
            service = FakeAddressService()
            loader = make_loader(service, limit=1)
            loader.add_value("address", "a0")
            await wait_until(lambda: service.active == 1)

            async def closer() -> None:
                await asyncio.sleep(0.02)
                await loader.close()

            with pytest.raises(RuntimeError, match="closed"):
                await asyncio.gather(loader.load_item(), closer())
            assert service.active == 0

        asyncio.run(scenario())

    def test_close_without_scheduled_calls(self):
        async def scenario() -> None:
            loader = AsyncItemLoader()
            loader.add_value("name", "kept")
            await loader.close()
            assert loader._values == {"name": ["kept"]}
            with pytest.raises(RuntimeError, match="closed"):
                loader.add_value("name", "late")
            with pytest.raises(RuntimeError, match="closed"):
                await loader.load_item()

        asyncio.run(scenario())

    def test_close_is_idempotent_and_cancels_pending(self):
        async def scenario() -> None:
            service = FakeAddressService()
            loader = make_loader(service, limit=2)
            for index in range(3):
                loader.add_value("address", f"a{index}")
            await wait_until(lambda: service.active == 2)

            await loader.close()
            await loader.close()
            assert service.active == 0
            assert sorted(service.cancelled) == [0, 1]
            with pytest.raises(RuntimeError, match="closed"):
                loader.add_value("address", "late")
            with pytest.raises(RuntimeError, match="closed"):
                await loader.load_item()

        asyncio.run(scenario())

    def test_async_context_manager_closes_on_exception(self):
        async def scenario() -> None:
            service = FakeAddressService()
            loader = make_loader(service, limit=1)
            loader.add_value("address", "a0")
            await wait_until(lambda: service.active == 1)
            with pytest.raises(RuntimeError, match="stop"):
                async with loader:
                    raise RuntimeError("stop")
            assert service.active == 0
            assert service.cancelled == [0]

        asyncio.run(scenario())

    def test_sync_api_does_not_start_event_loop(self):
        service = FakeAddressService()
        loader = make_loader(service, limit=2)
        with pytest.raises(RuntimeError, match="event loop"):
            loader.add_value("address", "a0")

        # Fully synchronous loaders still work without a running loop.
        sync_loader = AsyncItemLoader()
        sync_loader.add_value("name", "x")
        item = asyncio.run(sync_loader.load_item())
        assert item == {"name": ["x"]}

    def test_nested_loaders_share_concurrency_state(self):
        async def scenario() -> None:
            service = FakeAddressService()
            loader = make_loader(service, limit=2)
            child = loader.__class__(
                item=loader.item,
                parent=loader,
                address_service=service,
                concurrent_input_processors=2,
            )
            loader.add_value("address", "a0")
            child.add_value("address", "a1")
            loader.add_value("address", "a2")
            child.add_value("address", "a3")
            await wait_until(lambda: service.active == 2)
            assert service.max_active == 2
            for call_id in range(4):
                await wait_started(service, call_id)
                service.resolve(call_id)
            item = await loader.load_item()
            assert item["address"] == [f"STD:a{index}" for index in range(4)]

        asyncio.run(scenario())

    def test_replace_value_supersedes_inflight_call(self):
        async def scenario() -> None:
            service = FakeAddressService()
            loader = make_loader(service, limit=2)
            loader.add_value("address", "old")
            await wait_until(lambda: service.active == 1)
            loader.replace_value("address", "new")
            await wait_until(lambda: service.active == 2)
            service.resolve(0, result="STALE")
            service.resolve(1, result="FRESH")
            item = await loader.load_item()
            assert item["address"] == ["FRESH:new"]

        asyncio.run(scenario())

    def test_output_processors_run_after_drain(self):
        async def scenario() -> None:
            service = FakeAddressService()

            class SingleLoader(AddressLoader):
                default_output_processor = TakeFirst()

            loader = SingleLoader(
                address_service=service, concurrent_input_processors=2
            )
            loader.add_value("address", "a0")
            loader.add_value("address", "a1")
            await wait_until(lambda: service.active == 2)
            service.resolve(1)
            service.resolve(0)
            item = await loader.load_item()
            assert item["address"] == "STD:a0"

        asyncio.run(scenario())

    def test_load_item_can_be_awaited_more_than_once(self):
        async def scenario() -> None:
            service = FakeAddressService()
            loader = make_loader(service, limit=2)
            loader.add_value("address", "a0")
            await wait_until(lambda: service.active == 1)
            service.resolve(0)
            first = await loader.load_item()
            second = await loader.load_item()
            assert first == second == {"address": ["STD:a0"]}

        asyncio.run(scenario())

    def test_invalid_concurrency_argument(self):
        with pytest.raises(ValueError, match="positive integer"):
            AsyncItemLoader(concurrent_input_processors=0)
        with pytest.raises(ValueError, match="positive integer"):
            AsyncItemLoader(concurrent_input_processors=-1)

    def test_processor_raising_cancelled_error_poisons_loader(self):
        async def scenario() -> None:
            class BrokenLoader(AsyncItemLoader):
                address_in = raise_cancelled

            loader = BrokenLoader(concurrent_input_processors=2)
            loader.add_value("address", "a0")
            loader.add_value("address", "a1")
            with pytest.raises(asyncio.CancelledError):
                await loader.load_item()
            # load_item() treats the CancelledError as a cancellation and winds
            # the loader down, so every later write or load is rejected.
            with pytest.raises(RuntimeError, match="closed"):
                loader.add_value("address", "late")
            with pytest.raises(RuntimeError, match="closed"):
                await loader.load_item()

        asyncio.run(scenario())

    def test_semaphore_is_released_when_processor_raises(self):
        async def scenario() -> None:
            service = FakeAddressService()
            loader = make_loader(service, limit=1)
            loader.add_value("address", "a0")
            await wait_until(lambda: service.active == 1)
            service.fail(0, RuntimeError("boom"))
            with pytest.raises(ValueError, match="Error with input processor"):
                await loader.load_item()
            state = loader._async_state
            assert state is not None
            assert state.semaphore._value == 1

        asyncio.run(scenario())

    def test_chaining_still_returns_loader(self):
        async def scenario() -> None:
            service = FakeAddressService()
            loader = make_loader(service, limit=2)
            assert loader.add_value("address", "a0") is loader
            await loader.close()

        asyncio.run(scenario())
