"""The single thread all MLX work runs on.

MLX streams are thread-local and the models aren't thread-safe, so STT, LLM and
TTS share one worker thread. Generations are advanced one step per job rather
than run to completion, so the three interleave: TTS can speak the first
sentence while the LLM is still writing the second.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

T = TypeVar("T")

_DONE = object()


class MLXWorker:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")

    async def run(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)

    async def iterate(self, make: Callable[[], Iterator[T]]) -> AsyncIterator[T]:
        """Create an iterator on the worker thread and advance it one item per job.

        Callers should wrap this in `contextlib.aclosing` so the underlying
        generator is closed on the worker thread when they stop early.
        """
        it = await self.run(make)
        try:
            while (item := await self.run(next, it, _DONE)) is not _DONE:
                yield item  # type: ignore[misc]
        finally:
            if close := getattr(it, "close", None):
                await self.run(close)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
