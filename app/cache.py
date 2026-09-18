"""Bounded LRU for validated interpretations, with single-flight de-duplication.

The service runs one event loop in one process, so no locking is needed. Identical notes
arriving concurrently share one in-flight computation instead of N provider calls.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

V = TypeVar("V")


class SingleFlightCache(Generic[V]):
    def __init__(self, max_entries: int) -> None:
        self._max = max(0, max_entries)
        self._data: OrderedDict[str, V] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[V]] = {}
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: str) -> V | None:
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key: str, value: V) -> None:
        if self._max == 0:
            return
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    async def get_or_compute(
        self, key: str, compute: Callable[[], Awaitable[tuple[V, bool]]]
    ) -> tuple[V, bool]:
        """Return (value, was_shared). `compute` yields (value, cacheable)."""
        cached = self.get(key)
        if cached is not None:
            self.hits += 1
            return cached, True
        pending = self._inflight.get(key)
        if pending is not None:
            self.hits += 1
            return await asyncio.shield(pending), True

        self.misses += 1
        future: asyncio.Future[V] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            value, cacheable = await compute()
        except BaseException as error:
            if not future.done():
                future.set_exception(error)
                future.exception()  # mark retrieved: waiters may not exist
            raise
        else:
            if cacheable:
                self.put(key, value)
            future.set_result(value)
            return value, False
        finally:
            del self._inflight[key]
