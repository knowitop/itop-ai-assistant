import asyncio
from typing import Any, Callable, Coroutine, List, TypeVar

T = TypeVar("T")


async def tmap(fn: Callable[..., Coroutine[Any, Any, T]], args: List[Any], workers: int = 10) -> List[T]:
    """
    Async map with concurrency limit.
    Drop-in replacement for the original thread-based tmap.
    """
    semaphore = asyncio.Semaphore(workers)

    async def bounded(arg: Any) -> T:
        async with semaphore:
            return await fn(arg)

    return list(await asyncio.gather(*[bounded(arg) for arg in args]))
